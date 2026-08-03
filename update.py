"""Bring everything up to date: download what is new, extract it, embed it.

    python update.py              the whole thing
    python update.py --no-scrape  local steps only, no downloading
    python update.py --quiet      just the summary

Each step already skips work it has done before, so a normal run handles only the
positions that appeared since the last one -- a couple of minutes rather than an hour.

The three scripts are run as separate processes rather than imported. That keeps each
of them a program in its own right, runnable and debuggable on its own, and means
their output appears here exactly as it would if you ran them by hand.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DSN = os.getenv("DATABASE_URL", "postgresql://positions:positions@localhost:5432/positions")

STEPS = [
    ("download", ["scrape.py", "--update"]),
    ("extract", ["extract.py", "--all"]),
    ("classify", ["extract.py", "--types"]),
    # Recounted each time, because which words are too common to search for is a
    # property of the corpus and the corpus keeps changing.
    ("stopwords", ["extract.py", "--stopwords"]),
    ("embed", ["embed.py", "--all"]),
    ("chunk", ["embed.py", "--chunks"]),
]


def counts():
    """Where the database stands. Returns (positions, embedded, open)."""
    try:
        with psycopg.connect(DSN) as conn:
            return conn.execute(
                "SELECT count(*), count(embedding),"
                "       count(*) FILTER (WHERE closes_at > now()) FROM positions"
            ).fetchone()
    except psycopg.Error as error:
        print(f"could not read the database: {error}")
        return None


def run(name, command, quiet):
    print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
    started = time.time()
    result = subprocess.run(
        [sys.executable, *command],
        cwd=ROOT,
        capture_output=quiet,
        text=True,
    )
    elapsed = time.time() - started

    if result.returncode == 0:
        print(f"\n{name} finished in {elapsed:.0f}s")
    else:
        print(f"\n{name} FAILED (exit {result.returncode}) after {elapsed:.0f}s")
        if quiet and result.stdout:
            print(result.stdout[-2000:])
        if result.stderr:
            print(result.stderr[-2000:])

    return result.returncode == 0, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-scrape", action="store_true",
                        help="skip downloading; extract and embed what is on disk")
    parser.add_argument("--quiet", action="store_true",
                        help="hide each step's output, show only the summary")
    args = parser.parse_args()

    before = counts()
    if before is None:
        sys.exit(1)

    print(f"starting with {before[0]} positions, {before[1]} embedded")

    steps = [s for s in STEPS if not (args.no_scrape and s[0] == "download")]
    failed = []
    total = 0.0

    for name, command in steps:
        # Carry on after a failure rather than stopping. If downloading breaks,
        # extracting and embedding can still finish whatever is already on disk,
        # and a half-done update is better than none.
        ok, elapsed = run(name, command, args.quiet)
        total += elapsed
        if not ok:
            failed.append(name)

    after = counts()
    print(f"\n{'=' * 78}")
    print(f"done in {total:.0f}s")
    if after:
        print(f"  positions   {after[0]}  ({after[0] - before[0]:+d})")
        print(f"  embedded    {after[1]}  ({after[1] - before[1]:+d})")
        print(f"  still open  {after[2]}")
        if after[1] < after[0]:
            print(f"  {after[0] - after[1]} position(s) have no vector yet")

    if failed:
        print(f"\n  FAILED: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
