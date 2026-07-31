"""Turn each position's embed_text into a vector, so it can be searched by meaning.

    python embed.py --one     embed a single row and show the result
    python embed.py --all     embed every row that has no vector yet
    python embed.py --all --force   re-embed everything

Only rows with no vector are touched. extract.py clears the vector whenever
embed_text changes, so a re-extraction automatically queues just those rows.
"""

import argparse
import os
import sys
import time

# Must be set before sentence_transformers imports transformers. The model is
# already in ~/.cache/huggingface; without this the library still calls out to
# check for updates, which is dead time on a slow link and an outright failure
# when the connection is down.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import psycopg
from pgvector.psycopg import register_vector

DSN = "postgresql://positions:positions@localhost:5432/positions"

MODEL = "intfloat/multilingual-e5-base"
BATCH = 32

# e5 was trained with these prefixes and expects them: documents are "passage: ",
# searches are "query: ". Leaving them off measurably worsens the results and
# nothing warns you, so the search side must use "query: " to match.
PASSAGE = "passage: "


def load_model():
    from sentence_transformers import SentenceTransformer

    try:
        import torch
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    print(f"loading {MODEL} on {device}")
    started = time.time()
    model = SentenceTransformer(MODEL, device=device)
    print(f"  ready in {time.time() - started:.1f}s, "
          f"{model.get_sentence_embedding_dimension()} dimensions")
    return model


parser = argparse.ArgumentParser()
parser.add_argument("--one", action="store_true", help="embed a single row")
parser.add_argument("--all", action="store_true", help="embed everything missing one")
parser.add_argument("--force", action="store_true", help="re-embed rows that have one")
args = parser.parse_args()

if not (args.one or args.all):
    sys.exit("pick one: --one or --all")

with psycopg.connect(DSN) as conn:
    register_vector(conn)   # lets psycopg hand numpy arrays to a vector column

    where = "embed_text IS NOT NULL" if args.force else \
            "embed_text IS NOT NULL AND embedding IS NULL"
    limit = " LIMIT 1" if args.one else ""

    rows = conn.execute(
        f"SELECT source, source_id, title, embed_text FROM positions "
        f" WHERE {where} ORDER BY source, source_id{limit}"
    ).fetchall()

    total = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
    print(f"\n{total} row(s) in the table, {len(rows)} need embedding")
    if not rows:
        print("nothing to do -- use --force to redo them all")
        sys.exit(0)

    model = load_model()
    started = time.time()
    done = 0

    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        vectors = model.encode(
            [PASSAGE + row[3] for row in batch],
            batch_size=BATCH,
            # Normalised so that a dot product is the cosine similarity, which is
            # what the vector_cosine_ops index on this column expects.
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )

        for (source, source_id, title, _), vector in zip(batch, vectors):
            conn.execute(
                "UPDATE positions SET embedding = %s, embedded_at = now() "
                " WHERE source = %s AND source_id = %s",
                (vector, source, source_id),
            )

            if args.one:
                print(f"\n  {title}")
                print(f"  {len(vector)} numbers, first five:")
                print("   ", ", ".join(f"{value:+.4f}" for value in vector[:5]))
                print(f"  length {float((vector ** 2).sum()) ** 0.5:.4f} "
                      f"(1.0 means normalised, as the index expects)")

        conn.commit()
        done += len(batch)
        if not args.one and done % 250 < BATCH:
            print(f"  ... {done}/{len(rows)}")

    elapsed = time.time() - started
    print(f"\n  embedded {done} row(s) in {elapsed:.1f}s")

    with_vector = conn.execute(
        "SELECT count(embedding) FROM positions"
    ).fetchone()[0]
    print(f"  {with_vector}/{total} row(s) in the table now have a vector")
