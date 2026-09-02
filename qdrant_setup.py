"""Create the two Qdrant collections the website reads from. Run once.

    python qdrant_setup.py            create what is missing, leave the rest alone
    python qdrant_setup.py --drop     delete both and rebuild them empty

Run once against the cluster, from anywhere holding the credentials -- a terminal
now, the nightly workflow later. The website never calls it, and nothing here
reads a database: creating the collections and filling them are separate jobs.

Creating a collection is cheap; the data is what costs. --drop exists because the
payload will change a few times before this is settled -- but refilling means the
builder scrapes all four boards from nothing, which is hours, not minutes.
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

load_dotenv()

URL = os.getenv("QDRANT_URL")
KEY = os.getenv("QDRANT_API_KEY")

DIM = 768                      # intfloat/multilingual-e5-base
POSITIONS = "positions"
CHUNKS = "chunks"

# Fields that get filtered on before ranking. An unindexed payload field is still
# filterable, but Qdrant falls back to scanning it, and these filters are applied
# to every search this thing will ever run.
#
# Both collections carry the same fields on purpose. The chunk half of the search
# has to be prefiltered by type and country exactly like the position half, or a
# PhD-only query gets postdoc passages back through the chunks. That is the same
# reason search.py applies its type_clause inside the SQL rather than after it:
# ranking cannot tell a PhD post from a postdoc.
FILTERS = {
    "country_code": models.PayloadSchemaType.KEYWORD,   # ISO alpha-2
    "position_type": models.PayloadSchemaType.KEYWORD,  # array; keyword handles it
    "source": models.PayloadSchemaType.KEYWORD,
    "level": models.PayloadSchemaType.KEYWORD,          # "position" now, courses later
    "closes_at": models.PayloadSchemaType.DATETIME,     # RFC-3339 strings in payload
    "pid": models.PayloadSchemaType.KEYWORD,            # group_by key on chunks
}


def client():
    return QdrantClient(url=URL, api_key=KEY, timeout=60)


def dense():
    return models.VectorParams(size=DIM, distance=models.Distance.COSINE)


def hnsw():
    # m=16 is the default and right for a collection this size. ef_construct is
    # raised from 100 because building the index is a one-off cost of seconds
    # here, and recall is worth more than build time at 24k points.
    return models.HnswConfigDiff(m=16, ef_construct=200)


def create(qdrant, name, sparse):
    qdrant.create_collection(
        collection_name=name,
        vectors_config={"dense": dense()},
        # Only `positions` carries BM25. Matching a literal string against a
        # 1,200-char fragment scores noise; the keyword half of the search
        # belongs at advert level.
        #
        # modifier=IDF is what makes it BM25 rather than raw term counts. Term
        # rarity is a property of the whole collection, so Qdrant has to compute
        # it server-side; without this the sparse vector scores nothing useful.
        sparse_vectors_config=(
            {"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)}
            if sparse else None
        ),
        hnsw_config=hnsw(),
        # 13 MB of advert text belongs on disk, not in the 1 GB of RAM that the
        # free tier gives the vectors. Reversible later with update_collection if
        # reranking turns out to be latency-bound on payload reads.
        on_disk_payload=True,
    )
    print(f"created {name}")

    for field, schema in FILTERS.items():
        qdrant.create_payload_index(
            collection_name=name, field_name=field, field_schema=schema,
        )
    print(f"  {len(FILTERS)} payload indexes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--drop", action="store_true",
                        help="delete both collections first. Destroys the data")
    args = parser.parse_args()

    if not URL or not KEY:
        sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set in .env")

    qdrant = client()
    existing = {c.name for c in qdrant.get_collections().collections}

    if args.drop:
        for name in (POSITIONS, CHUNKS):
            if name in existing:
                points = qdrant.get_collection(name).points_count
                # Asked rather than assumed, because --drop on the wrong terminal
                # is a full re-scrape: there is no copy to reload from.
                answer = input(f"delete {name} with {points:,} points? [y/N] ")
                if answer.strip().lower() != "y":
                    sys.exit("stopped")
                qdrant.delete_collection(name)
                print(f"deleted {name}")
        existing = {c.name for c in qdrant.get_collections().collections}

    for name, sparse in ((POSITIONS, True), (CHUNKS, False)):
        if name in existing:
            print(f"{name} already exists, left alone")
            continue
        create(qdrant, name, sparse)

    print()
    for name in (POSITIONS, CHUNKS):
        info = qdrant.get_collection(name)
        print(f"{name}: {info.points_count:,} points, status {info.status}")


if __name__ == "__main__":
    main()
