"""Turn adverts into Qdrant points. The website's builder.

    python website_build.py --one           build one advert, print it, push nothing
    python website_build.py --one --push    build one advert and push it
    python website_build.py --all --dry-run what a build would add and remove
    python website_build.py --all           do it

This is the website half of the project. It shares the local pipeline's parsing
and embedding by importing them -- one extractor, not two -- but it writes to
Qdrant and never touches Postgres. Nothing here runs against the local database,
and nothing in the local pipeline calls this.

--one reads a file already on disk so the shape of a point can be checked without
waiting for a download. It is a development tool and nothing the website runs.
--all keeps nothing on disk at all: it downloads, parses and discards.

There is no record of the previous run anywhere, and none is needed. The sitemap
says what exists now, Qdrant says what existed last night, and the difference
between the two is the whole job:

    in the sitemap, not in Qdrant     ->  download it
    in Qdrant, not in the sitemap     ->  the board pulled it, delete it
    closes_at already past            ->  delete it

That works only because the point id is a hash of source and source_id, so the
builder can compute the id of a sitemap URL and look for it without downloading
anything. It also means an advert that quietly vanishes from its board cannot
linger as a dead link, which is a fault the local pipeline has to be told about.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
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
import scrape  # noqa: E402

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


# Points go up in batches: one request per advert would spend the whole run on
# round trips, and one request for 4,900 would be a single failure away from
# starting over.
BATCH = 64


def held(qdrant, source):
    """What Qdrant already has for one board: {source_id: closes_at}.

    This is the builder's memory. There is no extracted_at column out here, so
    the collection itself answers "have I done this one?".
    """
    found = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=POSITIONS,
            scroll_filter=models.Filter(must=[models.FieldCondition(
                key="source", match=models.MatchValue(value=source))]),
            limit=1000, offset=offset,
            with_payload=["source_id", "closes_at"], with_vectors=False,
        )
        for point in points:
            found[point.payload["source_id"]] = point.payload.get("closes_at")
        if offset is None:
            return found


def plan(site, qdrant):
    """(urls to fetch, source_ids to delete) for one board."""
    name = site["name"]
    urls = scrape.sitemap_urls(site, site.get("delay", 2))
    listed = {scrape.ad_id(url, site["id_pattern"]): url for url in urls}
    listed.pop(None, None)

    have = held(qdrant, name)
    now = datetime.now(UTC).isoformat()

    todo = [url for source_id, url in sorted(listed.items()) if source_id not in have]
    gone = [i for i in have if i not in listed]
    # A deadline that has passed is not a job any more. Checked against the stored
    # payload rather than re-reading the advert, which is the point of keeping
    # closes_at in the payload at all.
    expired = [i for i, closes in have.items()
               if i in listed and closes and closes <= now]
    return todo, gone, expired


def fetch(url, site, board_dir):
    """Download one advert and parse it. Returns a row, or None.

    extract.read() takes a path and works out the board from the folder name, so
    the page is written to a temp file named the way it would be on the Mac and
    deleted with the directory. Doing it this way keeps one extractor rather than
    a second one that parses strings.
    """
    source_id = scrape.ad_id(url, site["id_pattern"])
    if not source_id:
        return None
    response = scrape.get(url)
    if response.status_code != 200:
        return None
    path = board_dir / f"{source_id}.html"
    path.write_text(response.text, encoding="utf-8")
    try:
        return extract.read(path)
    finally:
        path.unlink(missing_ok=True)


def live(row):
    """Only positions still open are published."""
    closes = row.get("closes_at")
    if closes is None:
        return True
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=UTC)
    return closes > datetime.now(UTC)


def send(qdrant, positions, chunks):
    if positions:
        qdrant.upsert(collection_name=POSITIONS, points=positions)
    if chunks:
        qdrant.upsert(collection_name=CHUNKS, points=chunks)


def remove(qdrant, source, source_ids):
    """Delete positions and every chunk cut from them."""
    if not source_ids:
        return
    ids = [point_id(source, i) for i in source_ids]
    pids = [pid_of(source, i) for i in source_ids]
    qdrant.delete(collection_name=POSITIONS,
                  points_selector=models.PointIdsList(points=ids))
    qdrant.delete(collection_name=CHUNKS, points_selector=models.FilterSelector(
        filter=models.Filter(must=[models.FieldCondition(
            key="pid", match=models.MatchAny(any=pids))])))


def indexing(qdrant, threshold):
    """HNSW building off during a bulk load, on again afterwards.

    Rebuilding the graph on every batch is most of the cost of a large load. The
    threshold is restored in a finally block: a collection left at 0 would answer
    every search by brute force, silently and for ever.
    """
    for name in (POSITIONS, CHUNKS):
        qdrant.update_collection(
            collection_name=name,
            optimizers_config=models.OptimizersConfigDiff(
                indexing_threshold=threshold),
        )


def build_all(dry_run=False):
    qdrant = QdrantClient(url=URL, api_key=KEY, timeout=120)

    work = {}
    for name in BOARDS:
        site = extract.SITES[name]
        print(f"\n=== {name}")
        todo, gone, expired = plan(site, qdrant)
        work[name] = (site, todo, gone, expired)
        print(f"  {len(todo)} to fetch, {len(gone)} gone, {len(expired)} expired")

    print("\n" + "=" * 60)
    total = sum(len(t) for _, t, _, _ in work.values())
    print(f"{total} advert(s) to fetch, "
          f"{sum(len(g) + len(e) for _, _, g, e in work.values())} point(s) to delete")

    if dry_run:
        print("\n--dry-run, nothing written")
        return

    model = embed.load_model()
    sparse_model = SparseTextEmbedding(BM25)

    indexing(qdrant, 0)
    try:
        for name, (site, todo, gone, expired) in work.items():
            print(f"\n=== {name}")
            remove(qdrant, name, gone + expired)
            if gone or expired:
                print(f"  removed {len(gone) + len(expired)} point(s)")

            written = skipped = closed = 0
            positions, chunks = [], []
            # dir=ROOT because extract.read() records the file as a path relative
            # to the project directory, and /tmp is not under it. The directory
            # and everything in it goes when the block ends.
            with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
                board_dir = Path(tmp) / name
                board_dir.mkdir()
                for number, url in enumerate(todo, 1):
                    row = fetch(url, site, board_dir)
                    if row is None or not row.get("embed_text"):
                        skipped += 1
                    elif not live(row):
                        closed += 1
                    else:
                        position, cut = build(row, model, sparse_model)
                        positions.append(position)
                        chunks.extend(cut)
                        written += 1
                    if len(positions) >= BATCH:
                        send(qdrant, positions, chunks)
                        positions, chunks = [], []
                        print(f"  ... {number}/{len(todo)}")
                    time.sleep(site.get("delay", 2))
                send(qdrant, positions, chunks)

            print(f"  {written} written, {skipped} unreadable, {closed} already closed")
    finally:
        indexing(qdrant, 10000)

    print()
    for name in (POSITIONS, CHUNKS):
        info = qdrant.get_collection(name)
        print(f"{name}: {info.points_count:,} points, status {info.status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--one", action="store_true",
                        help="build a single advert and print it")
    parser.add_argument("--push", action="store_true",
                        help="with --one, also send it to Qdrant")
    parser.add_argument("--all", action="store_true",
                        help="fetch every board and bring Qdrant up to date")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --all, report what would change and write nothing")
    args = parser.parse_args()

    if not (args.one or args.all):
        sys.exit("pick one: --one or --all")
    if (args.all or args.push) and not (URL and KEY):
        sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set in .env")

    if args.all:
        build_all(dry_run=args.dry_run)
        return

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
