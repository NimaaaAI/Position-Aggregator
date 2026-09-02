"""Turn adverts into Qdrant points. The website's builder.

    python website_build.py --one           build one advert, print it, push nothing
    python website_build.py --one --push    build one advert and push it

This is the website half of the project. It shares the local pipeline's parsing
and embedding by importing them -- one extractor, not two -- but it writes to
Qdrant and never touches Postgres. Nothing here runs against the local database,
and nothing in the local pipeline calls this.

--one reads a file already on disk so the shape of a point can be checked without
waiting for a download. The full build fetches from the sitemaps instead; that is
the next step, and it is the only part of this that differs.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC
from pathlib import Path

# Set before importing embed. embed.py does os.environ.setdefault("HF_HUB_OFFLINE",
# "1") at import time, which is right for the Mac -- the model is already cached
# there and a stray download would be a bug. In CI nothing is cached, so the
# builder has to be allowed to fetch. setdefault means whatever is set here wins.
os.environ.setdefault("HF_HUB_OFFLINE", "0")

from dotenv import load_dotenv  # noqa: E402
from fastembed import SparseTextEmbedding  # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

import embed  # noqa: E402
import extract  # noqa: E402

load_dotenv()

ROOT = Path(__file__).parent
URL = os.getenv("QDRANT_URL")
KEY = os.getenv("QDRANT_API_KEY")

POSITIONS = "positions"
CHUNKS = "chunks"

# EURAXESS is excluded from the public deployment: its robots.txt disallows the
# job pages. It stays in the local Postgres, which is not published.
BOARDS = [name for name in extract.SITES if name != "euraxess"]

# One namespace, fixed forever. uuid5 hashes (namespace, name) into a UUID, so
# "jobsacuk:DQH648" always produces the same point id on every machine and every
# run. That is what makes the push idempotent -- a re-run overwrites the point
# rather than adding a second copy -- and it is also how the builder asks Qdrant
# "do you already have this?" without a database of its own to remember in.
#
# Generated once with uuid4 for this project and frozen. The value itself is
# arbitrary and means nothing; what matters is that it never changes. Change it
# and every id in both collections shifts, orphaning everything already loaded.
NAMESPACE = uuid.UUID("ed652f4f-e196-4772-b3e7-137d4751df6f")

# Qdrant computes IDF across the collection (modifier=IDF on the sparse vector),
# so this model only has to produce term frequencies. The website must embed its
# queries with this same model or the two halves score against different vocab.
BM25 = "Qdrant/bm25"

LEVEL = "position"      # courses and other material get their own value later


def point_id(source, source_id):
    return str(uuid.uuid5(NAMESPACE, f"{source}:{source_id}"))


def pid_of(source, source_id):
    """The human-readable key. Payload carries it because a UUID is unreadable,
    and because chunks group on it."""
    return f"{source}:{source_id}"


def when(value):
    """Datetimes go into the payload as RFC-3339 strings, which is what the
    DATETIME payload index expects. Naive values are read as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def keyword_text(row):
    """What BM25 indexes. Deliberately the same fields the local tsvector is
    generated from, so the keyword half of the search means the same thing in
    both worlds."""
    return " ".join(str(row.get(field) or "") for field in
                    ("title", "employer", "city", "country", "description"))


def payload(row, types, code):
    return {
        "pid": pid_of(row["source"], row["source_id"]),
        "level": LEVEL,
        "source": row["source"],
        "source_id": row["source_id"],
        "url": row["url"],
        "title": row["title"],
        "employer": row["employer"],
        "city": row["city"],
        "country": row["country"],
        "country_code": code,
        "position_type": types,
        "posted_at": when(row["posted_at"]),
        "closes_at": when(row["closes_at"]),
        "summary": row["summary"],
        # Kept for the reranker, which scores the query against the advert text.
        # The website shows a snippet and a link out, never the whole body.
        "description": row["description"],
    }


def build(row, model, sparse_model):
    """One extracted row -> (position point, [chunk points])."""
    types = extract.classify(row["title"], row["description"]) or []
    code = extract.iso_country(row["country"]) if row["country"] else None
    pid = pid_of(row["source"], row["source_id"])

    dense = embed.encode(model, [row["embed_text"]])[0]
    sparse = next(sparse_model.embed([keyword_text(row)]))

    position = models.PointStruct(
        id=point_id(row["source"], row["source_id"]),
        vector={
            "dense": dense.tolist(),
            "bm25": models.SparseVector(
                indices=sparse.indices.tolist(), values=sparse.values.tolist()
            ),
        },
        payload=payload(row, types, code),
    )

    texts = embed.chunks_for(row["title"], row["employer"], row["city"],
                             row["country"], row["description"])
    vectors = embed.encode(model, texts) if texts else []
    chunks = [
        models.PointStruct(
            # The chunk's own id has to be stable too, or a re-run duplicates
            # every passage. Same namespace, index appended.
            id=str(uuid.uuid5(NAMESPACE, f"{pid}#{number}")),
            vector={"dense": vector.tolist()},
            payload={
                "pid": pid,
                "text": text,
                # The five scalars that let the chunk half be prefiltered exactly
                # like the position half. Without them a PhD-only query gets
                # postdoc passages back through the chunks.
                "level": LEVEL,
                "source": row["source"],
                "country_code": code,
                "position_type": types,
                "closes_at": when(row["closes_at"]),
            },
        )
        for number, (text, vector) in enumerate(zip(texts, vectors, strict=True))
    ]
    return position, chunks


def one_file():
    """The first advert on disk from a board the website publishes. Only used by
    --one: the real build downloads instead."""
    for name in BOARDS:
        files = sorted((ROOT / "data" / "raw" / name).glob("*.html"))
        for path in files:
            row = extract.read(path)
            if row and row["embed_text"]:
                return path, row
    sys.exit("no readable advert found on disk")


def show(position, chunks):
    print(f"\npoint id  {position.id}")
    print(f"dense     {len(position.vector['dense'])} dims, "
          f"first 4 {[round(v, 4) for v in position.vector['dense'][:4]]}")
    bm25 = position.vector["bm25"]
    print(f"bm25      {len(bm25.indices)} terms")
    print("\npayload")
    for key, value in position.payload.items():
        text = json.dumps(value, ensure_ascii=False, default=str)
        print(f"  {key:14} {text[:100]}")
    print(f"\nchunks    {len(chunks)}")
    for chunk in chunks[:2]:
        print(f"  {chunk.id}  {chunk.payload['text'][:80]}...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--one", action="store_true",
                        help="build a single advert and print it")
    parser.add_argument("--push", action="store_true",
                        help="with --one, also send it to Qdrant")
    args = parser.parse_args()

    if not args.one:
        sys.exit("pick one: --one")
    if args.push and not (URL and KEY):
        sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set in .env")

    path, row = one_file()
    print(f"read {path.parent.name}/{path.name}")

    model = embed.load_model()
    sparse_model = SparseTextEmbedding(BM25)
    position, chunks = build(row, model, sparse_model)
    show(position, chunks)

    if not args.push:
        print("\nnothing pushed -- add --push")
        return

    qdrant = QdrantClient(url=URL, api_key=KEY, timeout=60)
    qdrant.upsert(collection_name=POSITIONS, points=[position])
    if chunks:
        qdrant.upsert(collection_name=CHUNKS, points=chunks)
    print(f"\npushed 1 position and {len(chunks)} chunk(s)")
    for name in (POSITIONS, CHUNKS):
        print(f"  {name}: {qdrant.get_collection(name).points_count:,} points")


if __name__ == "__main__":
    main()
