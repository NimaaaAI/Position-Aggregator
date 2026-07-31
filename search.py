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


def retrieve(question, limit=10, open_only=False, rerank=False, pool=RERANK_POOL):
    """Find the positions that best answer `question`.

    Returns a list of dicts, best first. With rerank=True the vector search fetches
    `pool` candidates and the cross-encoder reorders them, keeping the best `limit`.
    Each result carries both scores so the two stages can be compared.
    """
    vector = encoder().encode(
        [QUERY + question], normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=False,
    )[0]

    # The reranker can only improve on what it is given, so fetch more than we show.
    wanted = max(limit, pool) if rerank else limit
    open_clause = "AND (closes_at IS NULL OR closes_at > now())" if open_only else ""

    with psycopg.connect(DSN) as conn:
        register_vector(conn)
        # <=> is pgvector's cosine distance: 0 is identical, 2 is opposite.
        # Subtracting from 1 turns it into a similarity, which reads more naturally.
        rows = conn.execute(
            f"""
            SELECT source_id, title, employer, city, country, closes_at, url,
                   description, 1 - (embedding <=> %s) AS score
              FROM positions
             WHERE embedding IS NOT NULL {open_clause}
             ORDER BY embedding <=> %s
             LIMIT %s
            """,
            (vector, vector, wanted),
        ).fetchall()

    results = [
        {
            "source_id": row[0], "title": row[1], "employer": row[2],
            "city": row[3], "country": row[4], "closes_at": row[5], "url": row[6],
            "description": row[7], "vector_score": float(row[8]), "rerank_score": None,
        }
        for row in rows
    ]

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

    if item["rerank_score"] is not None:
        marks = f"{item['rerank_score']:.3f} rerank / {item['vector_score']:.3f} vector"
    else:
        marks = f"{item['vector_score']:.3f}"

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
    args = parser.parse_args()

    results = retrieve(
        args.question, limit=args.limit,
        open_only=args.open_only, rerank=args.rerank,
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
