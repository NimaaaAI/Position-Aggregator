"""Look at what is in Qdrant, from the terminal.

    python qdrant_show.py                       counts, per board, indexing state
    python qdrant_show.py --sample 5            five adverts, briefly
    python qdrant_show.py --pid jobsacuk:DQH648 one point in full, with its chunks
    python qdrant_show.py --type phd            restrict to a kind of post
    python qdrant_show.py --country DE          restrict to a country
    python qdrant_show.py --open                only ones still taking applications

Reads and prints. Nothing here writes, and no model is loaded -- every question it
answers is a payload question, so it costs one network call and no compute. It is
the counterpart to check_qdrant.py, which only proves the cluster is reachable.
"""

import argparse
import os
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

load_dotenv()

URL = os.getenv("QDRANT_URL")
KEY = os.getenv("QDRANT_API_KEY")

POSITIONS = "positions"
CHUNKS = "chunks"

# What a finished build should hold, so the summary can show progress rather than
# a number with nothing to compare it against.
BOARDS = ("academicpositions", "academictransfer", "jobsacuk", "naturecareers")


def client():
    return QdrantClient(url=URL, api_key=KEY, timeout=60)


def where(args):
    """The filter the flags add up to, or None when nothing was asked for."""
    must = []
    if args.type:
        must.append(models.FieldCondition(
            key="position_type", match=models.MatchValue(value=args.type)))
    if args.country:
        must.append(models.FieldCondition(
            key="country_code", match=models.MatchValue(value=args.country.upper())))
    if args.source:
        must.append(models.FieldCondition(
            key="source", match=models.MatchValue(value=args.source)))
    if args.open:
        # Null closes_at means no deadline given, which is not the same as closed.
        # is_null keeps those, so "open" means "not known to have shut".
        must.append(models.Filter(should=[
            models.FieldCondition(key="closes_at", range=models.DatetimeRange(
                gt=datetime.now(UTC))),
            models.IsNullCondition(is_null=models.PayloadField(key="closes_at")),
        ]))
    return models.Filter(must=must) if must else None


def count(qdrant, collection, condition=None):
    return qdrant.count(collection_name=collection,
                        count_filter=condition, exact=True).count


def summary(qdrant, condition):
    for name in (POSITIONS, CHUNKS):
        info = qdrant.get_collection(name)
        # indexing_threshold 0 means a bulk load switched HNSW off and has not
        # switched it back. Searches still answer, by brute force.
        building = " (indexing OFF -- a load is running or was interrupted)" \
            if info.config.optimizer_config.indexing_threshold == 0 else ""
        print(f"{name:10} {info.points_count:>7,} points   {info.status}{building}")

    print("\nper board")
    total = 0
    for board in BOARDS:
        n = count(qdrant, POSITIONS, models.Filter(must=[models.FieldCondition(
            key="source", match=models.MatchValue(value=board))]))
        total += n
        print(f"  {board:20} {n:>6,}")
    print(f"  {'':20} {total:>6,}")

    if condition:
        print(f"\nmatching your filter: {count(qdrant, POSITIONS, condition):,}")


def brief(point):
    p = point.payload
    closes = (p.get("closes_at") or "")[:10] or "no deadline"
    types = ",".join(p.get("position_type") or []) or "-"
    print(f"\n  {p['pid']}")
    print(f"    {(p.get('title') or '')[:72]}")
    print(f"    {(p.get('employer') or '')[:44]} · {p.get('city') or '?'}, "
          f"{p.get('country_code') or '??'}")
    print(f"    closes {closes} · {types}")
    print(f"    {p.get('url')}")


def sample(qdrant, condition, many):
    points, _ = qdrant.scroll(collection_name=POSITIONS, scroll_filter=condition,
                              limit=many, with_payload=True, with_vectors=False)
    if not points:
        print("nothing matched")
        return
    for point in points:
        brief(point)


def one(qdrant, pid):
    points, _ = qdrant.scroll(
        collection_name=POSITIONS,
        scroll_filter=models.Filter(must=[models.FieldCondition(
            key="pid", match=models.MatchValue(value=pid))]),
        limit=1, with_payload=True, with_vectors=True,
    )
    if not points:
        sys.exit(f"no point with pid {pid!r}")
    point = points[0]

    print(f"id      {point.id}")
    vectors = point.vector or {}
    if "dense" in vectors:
        print(f"dense   {len(vectors['dense'])} dims")
    if "bm25" in vectors:
        print(f"bm25    {len(vectors['bm25'].indices)} terms")

    print("\npayload")
    for key, value in point.payload.items():
        text = str(value).replace("\n", " ")
        print(f"  {key:14} {text[:110]}")

    chunks, _ = qdrant.scroll(
        collection_name=CHUNKS,
        scroll_filter=models.Filter(must=[models.FieldCondition(
            key="pid", match=models.MatchValue(value=pid))]),
        limit=20, with_payload=True, with_vectors=False,
    )
    print(f"\nchunks  {len(chunks)}")
    for chunk in chunks:
        print(f"  {chunk.payload['text'][:96]}...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, metavar="N",
                        help="show N adverts")
    parser.add_argument("--pid", help="show one point in full, by its pid")
    parser.add_argument("--type", help="phd, postdoc, professor, ...")
    parser.add_argument("--country", help="ISO alpha-2, e.g. DE")
    parser.add_argument("--source", choices=BOARDS, help="one board only")
    parser.add_argument("--open", action="store_true",
                        help="only ones still taking applications")
    args = parser.parse_args()

    if not URL or not KEY:
        sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set in .env")

    qdrant = client()
    condition = where(args)

    if args.pid:
        one(qdrant, args.pid)
    elif args.sample:
        sample(qdrant, condition, args.sample)
    else:
        summary(qdrant, condition)


if __name__ == "__main__":
    main()
