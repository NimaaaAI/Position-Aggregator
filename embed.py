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

# Chunk size in characters. e5 reads about 512 tokens, roughly 1,500-2,000
# characters, so 1,200 leaves room for the title prepended to each piece.
CHUNK_CHARS = 1200

# Chunks overlap so that a sentence split across the boundary still appears whole in
# one of them. Without it, "experience with graph neural networks" cut after "graph"
# is findable in neither piece.
CHUNK_OVERLAP = 200

# e5 was trained with these prefixes and expects them: documents are "passage: ",
# searches are "query: ". Leaving them off measurably worsens the results and
# nothing warns you, so the search side must use "query: " to match.
PASSAGE = "passage: "


def split_text(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Cut text into overlapping pieces, breaking at spaces rather than mid-word."""
    text = " ".join((text or "").split())
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer a space near the end so words survive intact.
            space = text.rfind(" ", start + size - 200, end)
            if space > start:
                end = space
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = end - overlap
    return pieces


def chunks_for(title, employer, city, country, description):
    """The pieces of one advert, each as it will be embedded.

    The title and place are prepended to every piece. A chunk reading "the candidate
    will have experience with PyTorch" says nothing about which job it belongs to, so
    on its own it matches a question about PyTorch and tells the ranking nothing else.
    """
    where = ", ".join(part for part in (city, country) if part)
    header = ". ".join(part for part in (title, employer, where) if part)
    return [f"{header}. {piece}" for piece in split_text(description)]


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
    # The method was renamed in a recent sentence-transformers; accept either so
    # the version installed does not matter.
    dimensions = getattr(
        model, "get_embedding_dimension", model.get_sentence_embedding_dimension
    )()
    print(f"  ready in {time.time() - started:.1f}s, {dimensions} dimensions")
    return model


def encode(model, texts):
    """Vectors for a list of texts, normalised so a dot product is the cosine --
    which is what the vector_cosine_ops indexes expect."""
    return model.encode(
        [PASSAGE + text for text in texts],
        batch_size=BATCH, normalize_embeddings=True,
        show_progress_bar=False, convert_to_numpy=True,
    )


def embed_positions(conn, one=False, force=False):
    """One vector per position, from its embed_text."""
    where = ("embed_text IS NOT NULL" if force
             else "embed_text IS NOT NULL AND embedding IS NULL")
    rows = conn.execute(
        f"SELECT source, source_id, title, embed_text FROM positions"
        f" WHERE {where} ORDER BY source, source_id{' LIMIT 1' if one else ''}"
    ).fetchall()

    total = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
    print(f"\n{total} row(s) in the table, {len(rows)} need embedding")
    if not rows:
        print("nothing to do -- use --force to redo them all")
        return

    model = load_model()
    started = time.time()

    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        vectors = encode(model, [row[3] for row in batch])

        # strict=True: if the model ever returned a different number of vectors than
        # texts given, zip would silently pair rows with the wrong vector, and every
        # one after it too. Better to stop.
        for (source, source_id, title, _), vector in zip(batch, vectors, strict=True):
            conn.execute(
                "UPDATE positions SET embedding = %s, embedded_at = now()"
                " WHERE source = %s AND source_id = %s",
                (vector, source, source_id),
            )
            if one:
                print(f"\n  {title}")
                print(f"  {len(vector)} numbers, first five:")
                print("   ", ", ".join(f"{value:+.4f}" for value in vector[:5]))
                print(f"  length {float((vector ** 2).sum()) ** 0.5:.4f} "
                      f"(1.0 means normalised, as the index expects)")

        conn.commit()
        done = min(start + BATCH, len(rows))
        if not one and done % 250 < BATCH:
            print(f"  ... {done}/{len(rows)}")

    print(f"\n  embedded {len(rows)} row(s) in {time.time() - started:.1f}s")
    with_vector = conn.execute("SELECT count(embedding) FROM positions").fetchone()[0]
    print(f"  {with_vector}/{total} row(s) in the table now have a vector")


def embed_chunks(conn, force=False):
    """Several vectors per position, one per piece of its advert.

    Positions already chunked are skipped, so this is cheap to re-run after an
    update. --force rebuilds everything, which is what to use after changing the
    chunk size or what goes in the header.
    """
    done_already = set()
    if force:
        conn.execute("DELETE FROM position_chunks")
        conn.commit()
    else:
        done_already = {
            (row[0], row[1]) for row in conn.execute(
                "SELECT DISTINCT source, source_id FROM position_chunks"
            ).fetchall()
        }

    rows = [
        row for row in conn.execute(
            "SELECT source, source_id, title, employer, city, country, description"
            "  FROM positions WHERE description IS NOT NULL ORDER BY source, source_id"
        ).fetchall()
        if (row[0], row[1]) not in done_already
    ]

    print(f"\n{len(done_already)} position(s) already chunked, {len(rows)} to do")
    if not rows:
        print("nothing to do -- use --force to rebuild them all")
        return

    model = load_model()
    started = time.time()
    pieces = []          # (source, source_id, index, text)
    for source, source_id, title, employer, city, country, description in rows:
        for index, text in enumerate(
            chunks_for(title, employer, city, country, description)
        ):
            pieces.append((source, source_id, index, text))

    print(f"  {len(pieces)} chunk(s) from {len(rows)} position(s), "
          f"{len(pieces) / max(len(rows), 1):.1f} each on average")

    for start in range(0, len(pieces), BATCH):
        batch = pieces[start:start + BATCH]
        vectors = encode(model, [piece[3] for piece in batch])
        for (source, source_id, index, text), vector in zip(batch, vectors,
                                                            strict=True):
            conn.execute(
                "INSERT INTO position_chunks"
                " (source, source_id, chunk_index, text, embedding)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (source, source_id, chunk_index) DO UPDATE"
                "    SET text = EXCLUDED.text, embedding = EXCLUDED.embedding,"
                "        embedded_at = now()",
                (source, source_id, index, text, vector),
            )
        conn.commit()
        done = min(start + BATCH, len(pieces))
        if done % 1000 < BATCH:
            print(f"  ... {done}/{len(pieces)}")

    stored = conn.execute("SELECT count(*) FROM position_chunks").fetchone()[0]
    print(f"\n  embedded {len(pieces)} chunk(s) in {time.time() - started:.1f}s")
    print(f"  {stored} chunk(s) in the table")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--one", action="store_true", help="embed a single position")
    parser.add_argument("--all", action="store_true",
                        help="embed every position without a vector")
    parser.add_argument("--chunks", action="store_true",
                        help="split adverts into pieces and embed each one")
    parser.add_argument("--force", action="store_true", help="redo work already done")
    args = parser.parse_args()

    if not (args.one or args.all or args.chunks):
        sys.exit("pick one: --one, --all or --chunks")

    with psycopg.connect(DSN) as conn:
        register_vector(conn)   # lets psycopg hand numpy arrays to a vector column
        if args.chunks:
            embed_chunks(conn, force=args.force)
        else:
            embed_positions(conn, one=args.one, force=args.force)


if __name__ == "__main__":
    main()
