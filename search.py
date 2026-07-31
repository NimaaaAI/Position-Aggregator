"""Search the positions by meaning.

    python search.py "funded PhD in machine learning in the Netherlands"
    python search.py "quantum computing" --rerank
    python search.py "robotics" --open --limit 20

Two stages. The vector search compares your question against all 1,951 positions at
once and is effectively instant. The reranker, behind --rerank, then re-reads the
best few properly and reorders them; it is slower and considerably more accurate.

Neither stage sends anything anywhere. Both models run on this machine.
"""

import argparse
import os
import sys
import textwrap
from datetime import datetime, timezone

# Before sentence_transformers pulls in transformers. See embed.py.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import psycopg
from pgvector.psycopg import register_vector

DSN = "postgresql://positions:positions@localhost:5432/positions"

MODEL = "intfloat/multilingual-e5-base"
RERANKER = "BAAI/bge-reranker-v2-m3"

# e5 was trained with these prefixes. embed.py stored the adverts as "passage: ",
# so a question has to be asked as "query: " or the two are not comparable.
QUERY = "query: "

# How many the vector search hands to the reranker. It reads every pair in full, so
# this is the expensive stage and is kept to the plausible candidates.
RERANK_POOL = 50


def device():
    try:
        import torch
        return "mps" if torch.backends.mps.is_available() else "cpu"
    except ImportError:
        return "cpu"


parser = argparse.ArgumentParser()
parser.add_argument("question", help="what you are looking for, in any language")
parser.add_argument("--limit", type=int, default=10, help="how many to show")
parser.add_argument("--open", action="store_true", dest="open_only",
                    help="only positions whose closing date has not passed")
parser.add_argument("--rerank", action="store_true",
                    help="re-read the best candidates with the cross-encoder")
args = parser.parse_args()

from sentence_transformers import SentenceTransformer

print(f"loading {MODEL} on {device()}")
model = SentenceTransformer(MODEL, device=device())
vector = model.encode(
    [QUERY + args.question], normalize_embeddings=True,
    convert_to_numpy=True, show_progress_bar=False,
)[0]

# The reranker only helps if it has more than the final list to choose from.
wanted = max(args.limit, RERANK_POOL) if args.rerank else args.limit

with psycopg.connect(DSN) as conn:
    register_vector(conn)

    closed_clause = ""
    if args.open_only:
        closed_clause = "AND (closes_at IS NULL OR closes_at > now())"

    # <=> is pgvector's cosine distance: 0 is identical, 2 is opposite. Subtracting
    # from 1 turns it into a similarity, which reads more naturally.
    rows = conn.execute(
        f"""
        SELECT source_id, title, employer, city, country, closes_at, url,
               description, 1 - (embedding <=> %s) AS score
          FROM positions
         WHERE embedding IS NOT NULL {closed_clause}
         ORDER BY embedding <=> %s
         LIMIT %s
        """,
        (vector, vector, wanted),
    ).fetchall()

if not rows:
    sys.exit("nothing found")

reranked = None
if args.rerank:
    from sentence_transformers import CrossEncoder

    print(f"loading {RERANKER} on {device()}")
    cross = CrossEncoder(RERANKER, device=device(), max_length=512)

    # The cross-encoder reads the question and the advert together, rather than
    # comparing two vectors made separately. That is why it is better, and why it
    # cannot be run over the whole table.
    pairs = [(args.question, f"{row[1]}. {row[7][:2000]}") for row in rows]
    scores = cross.predict(pairs, show_progress_bar=False)
    reranked = sorted(zip(rows, scores), key=lambda pair: -pair[1])[:args.limit]
    rows = [row for row, _ in reranked]

print(f"\n{len(rows)} result(s) for: {args.question}\n")

for place, row in enumerate(rows, 1):
    source_id, title, employer, city, country, closes, url, description, score = row

    where = ", ".join(part for part in (city, country) if part) or "location not given"
    when = f"closes {closes:%d %b %Y}" if closes else "no closing date"
    if closes and closes < datetime.now(timezone.utc):
        when += "  [CLOSED]"

    if reranked:
        marks = f"{reranked[place - 1][1]:.3f} rerank / {score:.3f} vector"
    else:
        marks = f"{score:.3f}"

    print(f"{place:>2}. [{marks}]  {title}")
    print(f"     {employer} · {where} · {when}")
    print(f"     {url}")
    snippet = " ".join((description or "").split())[:220]
    for line in textwrap.wrap(snippet, width=88):
        print(f"     {line}")
    print()
