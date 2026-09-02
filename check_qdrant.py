"""Check that Qdrant Cloud is reachable and the key works.

    python check_qdrant.py

Creates nothing, writes nothing. Run it whenever the connection looks wrong.
"""

import os
import sys
import time

from dotenv import load_dotenv
from qdrant_client import QdrantClient

load_dotenv()

URL = os.getenv("QDRANT_URL")
KEY = os.getenv("QDRANT_API_KEY")

if not URL or not KEY:
    sys.exit("QDRANT_URL and QDRANT_API_KEY must both be set in .env")


def client():
    # timeout is generous on purpose. A first call over an unreliable link can be
    # slow without being broken, and a short timeout would report that as failure.
    return QdrantClient(url=URL, api_key=KEY, timeout=30)


def main():
    print(f"connecting to {URL}")

    try:
        qdrant = client()
        collections = qdrant.get_collections().collections
    except Exception as error:
        # The class name says more than the message for auth and TLS failures,
        # which often carry an empty or unhelpful string.
        sys.exit(f"\nFAILED: {type(error).__name__}: {error}\n"
                 "  403 or Unauthorized  -> the API key is wrong\n"
                 "  timeout or DNS       -> the network is blocking it, try the VPN")

    print(f"connected. {len(collections)} collection(s)")
    for collection in collections:
        info = qdrant.get_collection(collection.name)
        print(f"  {collection.name}: {info.points_count:,} points, {info.status}")

    # Five round trips, because the first is always the slowest: it pays for the
    # TLS handshake, which every later call on the same connection reuses.
    times = []
    for _ in range(5):
        start = time.perf_counter()
        qdrant.get_collections()
        times.append((time.perf_counter() - start) * 1000)

    print(f"\nround trip: first {times[0]:.0f}ms, "
          f"then {min(times[1:]):.0f}-{max(times[1:]):.0f}ms")
    print("  under 150ms   fine\n"
          "  150-400ms     expected from Ir to us-east-1, harmless\n"
          "  over 1000ms   something is wrong, or the VPN is routing oddly")


if __name__ == "__main__":
    main()
