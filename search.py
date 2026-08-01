"""Search the positions by meaning.

    python search.py "funded PhD in machine learning in the Netherlands"
    python search.py "quantum computing" --rerank
    python search.py "robotics" --open --limit 20

Two stages. The vector search compares your question against every position at once
and is effectively instant. The reranker, behind --rerank, then re-reads the best
candidates properly and reorders them; it is slower and considerably more accurate.

Neither stage sends anything anywhere. Both models run on this machine.

retrieve() is also the search used by ask.py and the web interface, so that a change
to the query or the ranking happens in one place rather than three.
"""

import argparse
import os
import re
import sys
import textwrap
from datetime import UTC, datetime

# Before sentence_transformers pulls in transformers. See embed.py.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import psycopg
from pgvector.psycopg import register_vector

DSN = os.getenv("DATABASE_URL", "postgresql://positions:positions@localhost:5432/positions")

MODEL = "intfloat/multilingual-e5-base"
RERANKER = "BAAI/bge-reranker-v2-m3"

# e5 was trained with these prefixes. embed.py stored the adverts as "passage: ",
# so a question has to be asked as "query: " or the two are not comparable.
QUERY = "query: "

# How many the vector search hands to the reranker. It reads every pair in full, so
# this is the expensive stage and is kept to the plausible candidates.
RERANK_POOL = 40

# Loaded once and reused. The web interface asks many questions in one process, and
# reloading a model per question would dominate the response time.
_encoder = None
_reranker = None


def device():
    try:
        import torch
        return "mps" if torch.backends.mps.is_available() else "cpu"
    except ImportError:
        return "cpu"


def encoder():
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        print(f"loading {MODEL} on {device()}", file=sys.stderr)
        _encoder = SentenceTransformer(MODEL, device=device())
    return _encoder


def reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        print(f"loading {RERANKER} on {device()}", file=sys.stderr)
        _reranker = CrossEncoder(RERANKER, device=device(), max_length=512)
    return _reranker


COLUMNS = """source_id, title, employer, city, country, closes_at, url, description"""


def as_result(row, vector_score=None, text_score=None):
    return {
        "source_id": row[0], "title": row[1], "employer": row[2], "city": row[3],
        "country": row[4], "closes_at": row[5], "url": row[6], "description": row[7],
        "vector_score": vector_score, "text_score": text_score,
        "rerank_score": None, "fused_score": 0.0,
    }


def tsqueries(question):
    """Full-text queries for the question, strictest first.

    Three tiers, each looser than the last, and the results are taken in that order:

        phrase   erc <-> starting <-> grant   the words adjacent, in order
        all      erc & starting & grant       all present, anywhere
        any      erc | starting | grant       at least one present

    Every tier earns its place. "any" alone is far too generous: Postgres's ts_rank
    has no notion of "erc" being rare and "grant" being boilerplate, so an advert
    mentioning a grant in its funding paragraph outranks the actual ERC Starting
    Grant post. "all" fixes most of that but still cannot tell three words scattered
    through a long advert from the phrase itself -- which is how a posting about
    Aristotle, containing "ERC", "starting date" and "grant" in separate paragraphs,
    ranked third for "ERC Starting Grant". Only the phrase tier separates those.

    "all" and "any" remain as fallbacks, because a whole question rarely appears
    word for word anywhere.

    Only word characters survive the split, so nothing reaching to_tsquery can upset
    its parser.
    """
    words = re.findall(r"[^\W_]+", question.lower(), flags=re.UNICODE)
    if not words:
        return []
    if len(words) == 1:
        return [words[0]]
    return [" <-> ".join(words), " & ".join(words), " | ".join(words)]


def fuse(rankings, k=60):
    """Reciprocal rank fusion: combine several rankings into one.

    Each list contributes 1/(k+rank) per item, so something placed well by both
    searches beats something placed well by only one. It compares *positions* rather
    than scores, which matters because a cosine similarity of 0.85 and a ts_rank of
    0.09 are not on any common scale and cannot simply be added.

    k=60 is the value from the original paper; it damps the difference between the
    top few places so that rank 1 does not overwhelm everything else.
    """
    fused = {}
    for ranking in rankings:
        for place, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + place)
    return fused


def retrieve(question, limit=10, open_only=False, rerank=False, pool=RERANK_POOL,
             hybrid=True):
    """Find the positions that best answer `question`.

    Returns a list of dicts, best first. Two searches run: vector similarity, which
    understands meaning, and full-text, which matches literal strings. Their results
    are combined by rank fusion. With rerank=True the cross-encoder then reorders the
    top `pool` candidates and the best `limit` are kept.

    Each result carries every score it earned, so the stages can be compared.
    """
    vector = encoder().encode(
        [QUERY + question], normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False,
    )[0]

    # The reranker can only improve on what it is given, so fetch more than we show.
    wanted = max(limit, pool) if rerank else limit
    open_clause = "AND (closes_at IS NULL OR closes_at > now())" if open_only else ""

    found = {}
    vector_order, text_order = [], []

    with psycopg.connect(DSN) as conn:
        register_vector(conn)

        # <=> is pgvector's cosine distance: 0 is identical, 2 is opposite.
        # Subtracting from 1 turns it into a similarity, which reads more naturally.
        for row in conn.execute(
            f"""
            SELECT {COLUMNS}, 1 - (embedding <=> %s) AS score
              FROM positions
             WHERE embedding IS NOT NULL {open_clause}
             ORDER BY embedding <=> %s
             LIMIT %s
            """,
            (vector, vector, wanted),
        ).fetchall():
            found[row[0]] = as_result(row, vector_score=float(row[8]))
            vector_order.append(row[0])

        if hybrid:
            # Strict query first, then loose to top up. Anything already found by
            # the stricter query keeps its higher place: text_order is built in
            # order, and a position is only added the first time it appears.
            for query in tsqueries(question):
                if len(text_order) >= wanted:
                    break
                for row in conn.execute(
                    f"""
                    SELECT {COLUMNS}, ts_rank(tsv, to_tsquery('simple', %s)) AS score
                      FROM positions
                     WHERE tsv @@ to_tsquery('simple', %s) {open_clause}
                     ORDER BY score DESC
                     LIMIT %s
                    """,
                    (query, query, wanted - len(text_order)),
                ).fetchall():
                    if row[0] in text_order:
                        continue
                    if row[0] in found:
                        found[row[0]]["text_score"] = float(row[8])
                    else:
                        found[row[0]] = as_result(row, text_score=float(row[8]))
                    text_order.append(row[0])

    fused = fuse([vector_order, text_order])
    for key, score in fused.items():
        found[key]["fused_score"] = score

    results = sorted(found.values(), key=lambda item: -item["fused_score"])[:wanted]

    if rerank and results:
        # The cross-encoder reads the question and the advert together, rather than
        # comparing two vectors made separately. That is why it is better, and why
        # it cannot be run over the whole table.
        pairs = [
            (question, f"{item['title']}. {(item['description'] or '')[:2000]}")
            for item in results
        ]
        scores = reranker().predict(pairs, show_progress_bar=False)
        for item, score in zip(results, scores, strict=True):
            item["rerank_score"] = float(score)
        results.sort(key=lambda item: -item["rerank_score"])

    return results[:limit]


def describe(item):
    """One result as the lines printed on the terminal."""
    where = ", ".join(p for p in (item["city"], item["country"]) if p) or "location not given"
    closes = item["closes_at"]
    when = f"closes {closes:%d %b %Y}" if closes else "no closing date"
    if closes and closes < datetime.now(UTC):
        when += "  [CLOSED]"

    # A position can be found by one search and not the other, so any of these may
    # be absent. "--" says the search did not return it, which is itself worth
    # seeing: it shows which of the two found a result the other one missed.
    def mark(label, value, places=3):
        return f"{label} {value:.{places}f}" if value is not None else f"{label} --"

    marks = " / ".join(filter(None, [
        mark("rerank", item["rerank_score"]) if item["rerank_score"] is not None else None,
        mark("vector", item["vector_score"]),
        mark("text", item["text_score"]),
    ]))

    lines = [
        f"[{marks}]  {item['title']}",
        f"{item['employer']} · {where} · {when}",
        item["url"],
    ]
    snippet = " ".join((item["description"] or "").split())[:220]
    lines += textwrap.wrap(snippet, width=88)
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question", help="what you are looking for, in any language")
    parser.add_argument("--limit", type=int, default=10, help="how many to show")
    parser.add_argument("--open", action="store_true", dest="open_only",
                        help="only positions whose closing date has not passed")
    parser.add_argument("--rerank", action="store_true",
                        help="re-read the best candidates with the cross-encoder")
    parser.add_argument("--no-hybrid", action="store_true",
                        help="vector search only, without full-text. For comparison")
    args = parser.parse_args()

    results = retrieve(
        args.question, limit=args.limit, open_only=args.open_only,
        rerank=args.rerank, hybrid=not args.no_hybrid,
    )
    if not results:
        sys.exit("nothing found")

    print(f"\n{len(results)} result(s) for: {args.question}\n")
    for place, item in enumerate(results, 1):
        first, *rest = describe(item)
        print(f"{place:>2}. {first}")
        for line in rest:
            print(f"     {line}")
        print()


# Only when run directly. Importing this file gives retrieve() without running a
# search or demanding command-line arguments.
if __name__ == "__main__":
    main()
