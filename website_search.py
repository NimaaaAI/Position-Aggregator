"""Search the website's Qdrant collections.

The website half of search.py. Same shape of answer, same models, same fusion --
but the rows come from Qdrant instead of Postgres, and nothing here imports psycopg
or touches a file. That is the whole reason it is a separate module rather than a
branch inside search.py: the Space has no database, and a module that could reach
for one would eventually try.

Three rankings are fused, exactly as the local version does:

    dense vector over positions   what the advert means
    BM25 sparse over positions    the words actually written in it
    dense vector over chunks      a passage buried inside a long advert

Qdrant can fuse with RRF server-side, but only within one request against one
collection. The chunk ranking lives in a second collection, so the three are fused
here instead -- the same reciprocal rank fusion, on the client.
"""

import os
import sys
from datetime import UTC, datetime

from qdrant_client import QdrantClient, models

POSITIONS = "positions"
CHUNKS = "chunks"

# Repeated from search.py rather than imported, because importing it would drag in
# psycopg and pgvector. Three constants is a cheap price for a server that cannot
# accidentally acquire a database.
MODEL = "intfloat/multilingual-e5-base"
RERANKER = "BAAI/bge-reranker-v2-m3"
QUERY = "query: "
BM25 = "Qdrant/bm25"

# Sixty candidates, all reranked, best handful shown. The cross-encoder reads each
# (question, advert) pair in full, so this number is the latency knob: on the
# Space's 2 vCPU it is the slowest thing in the request by a wide margin.
DEFAULT_LIMIT = 60

_client = None
_encoder = None
_reranker = None
_sparse = None


def client():
    global _client
    if _client is None:
        url, key = os.getenv("QDRANT_URL"), os.getenv("QDRANT_API_KEY")
        if not url or not key:
            sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set")
        _client = QdrantClient(url=url, api_key=key, timeout=30)
    return _client


def encoder():
    """Loaded once. Reloading per question would dominate the response time."""
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        print(f"loading {MODEL}", file=sys.stderr)
        _encoder = SentenceTransformer(MODEL, device="cpu")
    return _encoder


def reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        print(f"loading {RERANKER}", file=sys.stderr)
        _reranker = CrossEncoder(RERANKER, device="cpu", max_length=512)
    return _reranker


def sparse():
    global _sparse
    if _sparse is None:
        from fastembed import SparseTextEmbedding
        _sparse = SparseTextEmbedding(BM25)
    return _sparse


def where(open_only=True, position_type=None, country=None):
    """The filter, applied before ranking.

    Before, not after, and for the same reason search.py puts its type_clause
    inside the SQL: ranking cannot tell a PhD post from a postdoc, so filtering
    afterwards would return ten postdocs to a question asking for PhDs.
    """
    must = []
    if position_type:
        must.append(models.FieldCondition(
            key="position_type", match=models.MatchValue(value=position_type)))
    if country:
        must.append(models.FieldCondition(
            key="country_code", match=models.MatchValue(value=country.upper())))
    if open_only:
        # A missing deadline is unknown, not closed, so those are kept.
        must.append(models.Filter(should=[
            models.FieldCondition(key="closes_at", range=models.DatetimeRange(
                gt=datetime.now(UTC))),
            models.IsNullCondition(is_null=models.PayloadField(key="closes_at")),
        ]))
    return models.Filter(must=must) if must else None


def fuse(rankings, k=60):
    """Reciprocal rank fusion, the same as search.py.

    Each list contributes 1/(k+rank), so something placed well by two searches
    beats something placed well by one. It compares positions rather than scores,
    which matters because a cosine similarity and a BM25 score are not on any
    common scale and cannot simply be added.
    """
    fused = {}
    for ranking in rankings:
        for place, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + place)
    return fused


def as_result(payload):
    return {
        "source": payload.get("source"), "source_id": payload.get("source_id"),
        "title": payload.get("title"), "employer": payload.get("employer"),
        "city": payload.get("city"), "country": payload.get("country"),
        "country_code": payload.get("country_code"),
        "position_type": payload.get("position_type") or [],
        "closes_at": payload.get("closes_at"), "url": payload.get("url"),
        "summary": payload.get("summary"), "description": payload.get("description"),
        "vector_score": None, "chunk_score": None, "text_score": None,
        "rerank_score": None, "fused_score": 0.0,
    }


def collapse(results):
    """One row per job where several boards carry the same one.

    Same rule as search.py: a job is the same job when the title and the city
    match and the source does not. Employer cannot be compared directly -- the
    same institution is written "Umea universitet" on one board and "Umea
    University" on another. Nothing is dropped; the losing copies are attached as
    `also_on` so the other board's link stays one click away.
    """
    best = {}
    for item in results:
        key = ((item["title"] or "").strip().lower(),
               (item["city"] or "").strip().lower())
        if not key[0] or not key[1]:
            key = (id(item),)
        if key not in best:
            best[key] = item
        else:
            best[key].setdefault("also_on", []).append(
                {"source": item["source"], "url": item["url"],
                 "closes_at": item["closes_at"]})
    return list(best.values())


def retrieve(question, limit=10, open_only=True, position_type=None, country=None,
             rerank=True, dedupe=True, pool=DEFAULT_LIMIT):
    """Ranked positions for a question. Returns dicts shaped like search.py's."""
    qdrant = client()
    condition = where(open_only, position_type, country)

    dense = encoder().encode([QUERY + question], normalize_embeddings=True,
                             show_progress_bar=False)[0].tolist()
    # query_embed, not embed: for a query every term weighs 1 and the rarity comes
    # from the collection's IDF, which Qdrant applies server-side. Using embed()
    # here would weight the question by its own term frequencies, which is wrong
    # and, worse, quietly plausible.
    terms = next(sparse().query_embed(question))

    found, orders = {}, []

    for using, vector in (
        ("dense", dense),
        ("bm25", models.SparseVector(indices=terms.indices.tolist(),
                                     values=terms.values.tolist())),
    ):
        hits = qdrant.query_points(
            collection_name=POSITIONS, query=vector, using=using,
            query_filter=condition, limit=pool, with_payload=True,
        ).points
        order = []
        for hit in hits:
            pid = hit.payload["pid"]
            found.setdefault(pid, as_result(hit.payload))
            found[pid]["vector_score" if using == "dense" else "text_score"] = hit.score
            order.append(pid)
        orders.append(order)

    # Grouped by pid so an advert with four strong passages takes one place rather
    # than four. This is what the local version's max() GROUP BY does.
    groups = qdrant.query_points_groups(
        collection_name=CHUNKS, query=dense, using="dense",
        query_filter=condition, group_by="pid", limit=pool, group_size=1,
    ).groups
    chunk_order = []
    for group in groups:
        pid = group.id if isinstance(group.id, str) else str(group.id)
        chunk_order.append(pid)
        if pid in found:
            found[pid]["chunk_score"] = group.hits[0].score
    orders.append(chunk_order)

    # Chunk hits whose advert neither position search returned still need a payload.
    missing = [pid for pid in chunk_order if pid not in found]
    if missing:
        points, _ = qdrant.scroll(
            collection_name=POSITIONS, limit=len(missing), with_payload=True,
            scroll_filter=models.Filter(must=[models.FieldCondition(
                key="pid", match=models.MatchAny(any=missing))]),
        )
        for point in points:
            item = as_result(point.payload)
            item["chunk_score"] = next(
                (g.hits[0].score for g in groups if g.id == point.payload["pid"]), None)
            found[point.payload["pid"]] = item

    for pid, score in fuse(orders).items():
        if pid in found:
            found[pid]["fused_score"] = score

    results = sorted(found.values(), key=lambda item: -item["fused_score"])

    # Collapsed before the list is cut, not after: merging first spends the surplus
    # on replacements, so asking for sixty gives sixty.
    if dedupe:
        results = collapse(results)
    results = results[:pool]

    if rerank and results:
        pairs = [(question, f"{item['title']}. {(item['description'] or '')[:2000]}")
                 for item in results]
        for item, score in zip(results, reranker().predict(
                pairs, show_progress_bar=False), strict=True):
            item["rerank_score"] = float(score)
        results.sort(key=lambda item: -item["rerank_score"])

    return results[:limit]


def browse(limit=100, offset=0, open_only=True, position_type=None, country=None,
           dedupe=True):
    """Everything matching the filters, ordered by deadline, nothing ranked.

    Soonest first, because that is the only ordering that tells you what to do
    next. Positions with no deadline go last: not urgent, unknown.
    """
    qdrant = client()
    condition = where(open_only, position_type, country)
    total = qdrant.count(collection_name=POSITIONS, count_filter=condition,
                         exact=True).count

    points, _ = qdrant.scroll(
        collection_name=POSITIONS, scroll_filter=condition,
        limit=limit + offset, with_payload=True, with_vectors=False,
        order_by=models.OrderBy(key="closes_at", direction=models.Direction.ASC),
    )
    results = [as_result(point.payload) for point in points][offset:]
    if dedupe:
        results = collapse(results)
    return results, total


def stats():
    qdrant = client()
    info = qdrant.get_collection(POSITIONS)
    return {"positions": info.points_count,
            "chunks": qdrant.get_collection(CHUNKS).points_count}
