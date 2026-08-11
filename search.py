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

# How many positions a search returns by default. All of them get reranked, and the
# best handful of those go to the model.
#
# 60 rather than a larger number because reranking is the expensive stage: the
# cross-encoder reads each (question, advert) pair in full at roughly 25ms a pair,
# so 60 costs about 1.5 seconds and 500 would cost twelve.
DEFAULT_LIMIT = 60

# Loaded once and reused. The web interface asks many questions in one process, and
# reloading a model per question would dominate the response time.
_encoder = None
_reranker = None
_chunks_built = None
_stopwords = None


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


COLUMNS = """source, source_id, title, employer, city, country, closes_at, url, description"""

# The same list qualified with the table alias, for queries that join positions to
# position_chunks -- both carry source_id, so an unqualified name is ambiguous.
P_COLUMNS = ", ".join(f"p.{name.strip()}" for name in COLUMNS.split(","))


# Every query selects COLUMNS and then one score, so the score sits just past the
# last column. Derived rather than written out: adding a column to COLUMNS used to
# shift every index silently, which is exactly the kind of error that produces wrong
# results instead of an error.
NAMES = [name.strip() for name in COLUMNS.split(",")]
SCORE = len(NAMES)


def as_result(row, vector_score=None, chunk_score=None, text_score=None):
    # strict=False on purpose: row carries the trailing score, which is read
    # separately and has no name here.
    return {
        **dict(zip(NAMES, row, strict=False)),
        "vector_score": vector_score, "chunk_score": chunk_score,
        "text_score": text_score, "rerank_score": None, "fused_score": 0.0,
    }


def stopwords(conn):
    """Words too common in the corpus to be worth searching for.

    Measured, not listed by hand: `extract.py --stopwords` counts how many adverts
    contain each word and records those above a threshold. A hand-written list would
    be English only, while the adverts here are also Swedish, German, Dutch, French
    and Finnish -- and it would be a guess about which words are common rather than
    a count of which ones are.
    """
    global _stopwords
    if _stopwords is None:
        _stopwords = {
            row[0] for row in conn.execute("SELECT word FROM stopwords").fetchall()
        }
        if not _stopwords:
            print("no stopwords recorded -- run: python extract.py --stopwords",
                  file=sys.stderr)
    return _stopwords


def tsqueries(question, common):
    """Full-text queries for the question, strictest first.

    Stopwords are removed first, then three tiers, strictest first:

        phrase   pytorch <-> tensorflow   the words adjacent, in order
        all      pytorch & tensorflow     all present, anywhere in the advert
        any      pytorch | tensorflow     at least one present

    Removing the stopwords is what makes the last tier usable. Left in, "positions
    using PyTorch or TensorFlow" becomes `positions & using & pytorch & or &
    tensorflow`, which no advert satisfies, so full-text returned nothing at all for
    56 positions that name PyTorch. ORing the same words was worse: every advert
    contains "for" and "position", so most of the database came back with every
    score at the same noise floor and gender studies was promoted into a machine
    learning search.

    Neither failure was the tier's fault. It was the words.

    The phrase tier separates a real hit from a coincidence: a posting about
    Aristotle contains "ERC", "starting date" and "grant" in three different
    paragraphs and satisfies "all", but only genuine ERC Starting Grant positions
    have the words together.

    Only word characters survive the split, so nothing reaching to_tsquery can upset
    its parser.
    """
    words = [
        word for word in re.findall(r"[^\W_]+", question.lower(), flags=re.UNICODE)
        if word not in common
    ]
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


def has_chunks(conn):
    """Whether the chunk table has been built. Cached: it cannot change mid-process."""
    global _chunks_built
    if _chunks_built is None:
        _chunks_built = bool(conn.execute(
            "SELECT EXISTS (SELECT 1 FROM position_chunks LIMIT 1)"
        ).fetchone()[0])
    return _chunks_built


def browse(limit=100, offset=0, open_only=False, position_type=None,
           country=None, dedupe=True):
    """Every position matching the filters, with no question asked.

    Retrieval answers "which of these best matches what I typed". This answers
    "what is there", which is a different question and a better one when you are
    choosing where to apply: the ranked search returns its best sixty, and sixty
    is not the same as all eighty-six.

    Ordered by deadline, soonest first, because that is the only ordering that
    tells you what to do next. Positions with no deadline go last -- they are not
    urgent, they are unknown.

    Returns (rows, total) so the page can say how many more there are.
    """
    where = ["true"]
    if open_only:
        where.append("(closes_at IS NULL OR closes_at > now())")
    if position_type:
        where.append("%(type)s = ANY(position_type)")
    if country:
        where.append("country_code = %(country)s")
    clause = " AND ".join(where)

    params = {"type": position_type, "country": country,
              "limit": limit, "offset": offset}

    with psycopg.connect(DSN) as conn:
        total = conn.execute(
            f"SELECT count(*) FROM positions WHERE {clause}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT {COLUMNS} FROM positions"
            f" WHERE {clause}"
            "  ORDER BY closes_at ASC NULLS LAST, posted_at DESC NULLS LAST"
            "  LIMIT %(limit)s OFFSET %(offset)s",
            params,
        ).fetchall()

    # as_result leaves every score None, which is right: nothing was scored.
    results = [as_result(row) for row in rows]
    return (collapse(results) if dedupe else results), total


def collapse(results):
    """One row per job, where several boards carry the same one.

    The boards overlap heavily -- EURAXESS re-lists adverts the national sites
    already have -- and every copy spends one of the ten slots the model sees.

    A job is the same job when the title and the city match and the source does
    not. All three parts are needed, and each was put there by a case that broke
    without it:

      - source must differ, or the three IT:U ads all titled "PhD Student (f/m/d)"
        collapse into one, losing two real positions;
      - city must match, or six different universities advertising "Assistant
        Professor" -- Poznan, Bydgoszcz, Hong Kong, Nottingham -- become one;
      - employer cannot be compared directly, because the same institution is
        written "Umea universitet" on one board and "Umea University" on another.

    Nothing is deleted. The copies that lose are attached to the winner as
    `also_on` so the other board's link and deadline stay one click away.

    The survivor is whichever copy the fused ranking put first, since this runs
    before the shortlist is cut. Which copy wins barely matters -- the two carry
    near-identical text and score within a whisker of each other -- while having
    enough distinct positions left to fill the shortlist matters a great deal.
    """
    best = {}
    for item in results:
        key = ((item["title"] or "").strip().lower(),
               (item["city"] or "").strip().lower())
        # A position with no city cannot be checked against the rule, so it is
        # given a key of its own and never collapsed.
        if not key[0] or not key[1]:
            key = (id(item),)
        if key not in best:
            best[key] = item
        else:
            best[key].setdefault("also_on", []).append(
                {"source": item["source"], "url": item["url"],
                 "closes_at": item["closes_at"]})
    return list(best.values())


def retrieve(question, limit=DEFAULT_LIMIT, open_only=False, rerank=False,
             hybrid=True, position_type=None, chunked=True, dedupe=True,
             country=None):
    """Find the positions that best answer `question`.

    Returns `limit` dicts, best first. Two searches run: vector similarity, which
    understands meaning, and full-text, which matches literal strings, and their
    results are combined by rank fusion. With rerank=True the cross-encoder then
    re-reads all of them and reorders.

    position_type ("phd", "postdoc", ...) restricts the search before any ranking
    happens. That is the point of it: ranking cannot tell a PhD post from a postdoc,
    because every advert on a subject scores about the same whatever the job, so the
    only way to ask for one is to exclude the others up front.

    Each result carries every score it earned, so the stages can be compared.
    """
    vector = encoder().encode(
        [QUERY + question], normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False,
    )[0]

    wanted = int(limit)
    open_clause = "AND (closes_at IS NULL OR closes_at > now())" if open_only else ""
    # Applied inside the SQL rather than to the results, so the shortlist is drawn
    # from PhD positions only rather than filtered down to whichever happened to
    # survive a general ranking.
    type_clause = "AND %(type)s = ANY(position_type)" if position_type else ""
    # On the code, never on the text: three boards write the Netherlands three
    # different ways, so matching the spelling would hide most of a country.
    if country:
        type_clause += " AND country_code = %(country)s"
    params = {"vector": vector, "wanted": wanted, "type": position_type,
              "country": country}

    found = {}
    vector_order, chunk_order, text_order = [], [], []

    with psycopg.connect(DSN) as conn:
        register_vector(conn)

        # Three rankings are gathered and fused. They fail in different places, and
        # a position found by two of them outranks one found by only one:
        #
        #   position vectors  what the advert is about, from its opening
        #   chunk vectors     whether any passage matches, wherever it sits
        #   full-text         whether an exact string appears anywhere
        #
        # Neither vector ranking is sufficient alone. The position-level one sees
        # only the first ~1,500 characters, which for many employers is mostly
        # shared boilerplate -- it missed a position titled "PhD Student (f/m/d)"
        # whose subject was described further down. The chunk-level one compresses
        # the scores, because almost every advert has some passage loosely matching
        # a broad question, and positions the first query found comfortably fell
        # below the cutoff.

        # <=> is pgvector's cosine distance: 0 is identical, 2 is opposite.
        # Subtracting from 1 turns it into a similarity, which reads more naturally.
        for row in conn.execute(
            f"""
            SELECT {COLUMNS}, 1 - (embedding <=> %(vector)s) AS score
              FROM positions
             WHERE embedding IS NOT NULL {open_clause} {type_clause}
             ORDER BY embedding <=> %(vector)s
             LIMIT %(wanted)s
            """,
            params,
        ).fetchall():
            # Keyed on (source, source_id), because two boards number their ads
            # independently and both can publish an ad 250476.
            found[row[:2]] = as_result(row, vector_score=float(row[SCORE]))
            vector_order.append(row[:2])

        if chunked and has_chunks(conn):
            # Every chunk is scored and each position keeps its best. No shortlist
            # of chunks first: taking the top N and collapsing them looks like the
            # same thing and is not, because one 24-chunk advert can occupy 24 of
            # those slots. At 12,442 chunks a full scan is about a tenth of a
            # second, so the shortcut buys nothing and costs results.
            for row in conn.execute(
                f"""
                SELECT {P_COLUMNS},
                       max(1 - (c.embedding <=> %(vector)s)) AS score
                  FROM position_chunks c
                  JOIN positions p
                    ON p.source = c.source AND p.source_id = c.source_id
                 WHERE c.embedding IS NOT NULL {open_clause} {type_clause}
                 GROUP BY {P_COLUMNS}
                 ORDER BY score DESC
                 LIMIT %(wanted)s
                """,
                params,
            ).fetchall():
                if row[:2] in found:
                    found[row[:2]]["chunk_score"] = float(row[SCORE])
                else:
                    found[row[:2]] = as_result(row, chunk_score=float(row[SCORE]))
                chunk_order.append(row[:2])

        if hybrid:
            # Strict query first, then loose to top up. Anything already found by
            # the stricter query keeps its higher place: text_order is built in
            # order, and a position is only added the first time it appears.
            for query in tsqueries(question, stopwords(conn)):
                if len(text_order) >= wanted:
                    break
                for row in conn.execute(
                    f"""
                    SELECT {COLUMNS},
                           ts_rank(tsv, to_tsquery('simple', %(q)s)) AS score
                      FROM positions
                     WHERE tsv @@ to_tsquery('simple', %(q)s)
                           {open_clause} {type_clause}
                     ORDER BY score DESC
                     LIMIT %(remaining)s
                    """,
                    {**params, "q": query, "remaining": wanted - len(text_order)},
                ).fetchall():
                    if row[:2] in text_order:
                        continue
                    if row[:2] in found:
                        found[row[:2]]["text_score"] = float(row[SCORE])
                    else:
                        found[row[:2]] = as_result(row, text_score=float(row[SCORE]))
                    text_order.append(row[:2])

    fused = fuse([vector_order, chunk_order, text_order])
    for key, score in fused.items():
        found[key]["fused_score"] = score

    results = sorted(found.values(), key=lambda item: -item["fused_score"])

    # Collapsed before the list is cut, not after.
    #
    # Three searches each return `wanted`, so this pool holds well over `wanted`
    # distinct positions. Cutting first and merging afterwards throws the surplus
    # away and then discovers it was needed: ask for 60, merge nine pairs, see 51.
    # Merging first spends the surplus on replacements instead, and asking for 60
    # gives 60. Only when the whole pool collapses below `wanted` is there a
    # shortfall, and then there genuinely is nothing more to show.
    if dedupe:
        results = collapse(results)
    results = results[:wanted]

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
        mark("chunk", item["chunk_score"]),
        mark("text", item["text_score"]),
    ]))

    lines = [
        f"[{marks}]  {item['title']}",
        f"{item['employer']} · {where} · {when}",
        item["url"],
    ]
    if item.get("also_on"):
        lines.append("also on " + ", ".join(c["source"] for c in item["also_on"]))
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
    parser.add_argument("--no-chunks", action="store_true",
                        help="score each position by its opening only, as before "
                             "chunking. For comparison")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="show every board's copy of the same job separately")
    parser.add_argument("--type", dest="position_type",
                        choices=["phd", "postdoc", "professor", "lecturer",
                                 "researcher", "engineer", "student", "support"],
                        help="only this kind of post. Applied before ranking")
    parser.add_argument("--country", type=lambda code: code.upper(),
                        help="ISO country code, e.g. NL, GB, IT. Applied before "
                             "ranking, and matches however the board spelled it")
    args = parser.parse_args()

    results = retrieve(
        args.question, limit=args.limit, open_only=args.open_only,
        rerank=args.rerank, hybrid=not args.no_hybrid,
        position_type=args.position_type, chunked=not args.no_chunks,
        dedupe=not args.no_dedupe, country=args.country,
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
