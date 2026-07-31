"""Read the downloaded ad pages and write one row per position into Postgres.

Works entirely over the files already on disk. Nothing is downloaded, so a mistake
here costs a re-run of seconds rather than another hour of fetching.

    python extract.py --one    read one file, print what came out, write nothing
    python extract.py --all    read every file and fill the positions table
"""

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import psycopg
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DSN = "postgresql://positions:positions@localhost:5432/positions"

# The advert body is taken from the page's own LinkedIn share link, whose "summary"
# parameter holds the complete advert as HTML and nothing else.
#
# The obvious approach -- take the page text and subtract the furniture -- does not
# work here. The body also contains a region picker naming every European country,
# a language picker, three modals and a "Jobs from this employer" list of other
# people's adverts. Subtracting those means guessing every selector, and anything
# missed is silently glued onto the description: the region picker alone would put
# "Sverige Norge Danmark Deutschland" into all 1,886 rows, so a search for jobs in
# Germany would match every one of them.
SHARE_LINK = re.compile(r"linkedin\.com/shareArticle")

# How much of the description goes to the embedding model. e5 reads about 512
# tokens and silently ignores the rest, so more than this is wasted.
EMBED_CHARS = 1500


def json_ld_job(soup):
    """The JobPosting block, if the page has one."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def when(value):
    """"2026-06-26T14:17:46+02:00" -> datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read(path):
    """One saved HTML file -> a dict matching the positions table, or None."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")

    job = json_ld_job(soup)
    if job is None:
        return None

    address = (job.get("jobLocation") or {}).get("address") or {}
    organisation = job.get("hiringOrganization") or {}

    canonical = soup.find("link", rel="canonical")
    url = canonical["href"] if canonical and canonical.get("href") else ""

    # The advert body, from the share link's "summary" parameter. See SHARE_LINK.
    description = ""
    share = soup.find("a", href=SHARE_LINK)
    if share and share.get("href"):
        query = urllib.parse.urlparse(share["href"]).query
        body_html = urllib.parse.parse_qs(query).get("summary", [""])[0]
        if body_html:
            body = BeautifulSoup(body_html, "html.parser")
            description = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()

    title = job.get("title") or ""
    employer = organisation.get("name") or ""
    city = address.get("addressLocality") or ""
    country = address.get("addressCountry") or ""

    # What the embedding model actually sees: the facts first, because they are the
    # strongest signal, then as much of the advert as the model will read.
    embed_text = ". ".join(part for part in [
        title, employer, ", ".join(p for p in [city, country] if p),
    ] if part)
    embed_text = f"{embed_text}. {description[:EMBED_CHARS]}".strip()

    return {
        "source": "academicpositions",
        "source_id": path.stem,
        "url": url,
        "title": title or None,
        "employer": employer or None,
        "city": city or None,
        "country": country or None,
        "street": address.get("streetAddress") or None,
        "postcode": address.get("postalCode") or None,
        "industry": job.get("industry") or None,
        "posted_at": when(job.get("datePosted")),
        "closes_at": when(job.get("validThrough")),
        "summary": job.get("description") or None,
        "description": description or None,
        "embed_text": embed_text or None,
        "html_file": str(path.relative_to(ROOT)),
    }


UPSERT = """
INSERT INTO positions (
    source, source_id, url, title, employer, city, country, street, postcode,
    industry, posted_at, closes_at, summary, description, embed_text, html_file,
    extracted_at
) VALUES (
    %(source)s, %(source_id)s, %(url)s, %(title)s, %(employer)s, %(city)s,
    %(country)s, %(street)s, %(postcode)s, %(industry)s, %(posted_at)s,
    %(closes_at)s, %(summary)s, %(description)s, %(embed_text)s, %(html_file)s,
    now()
)
ON CONFLICT (source, source_id) DO UPDATE SET
    url = EXCLUDED.url, title = EXCLUDED.title, employer = EXCLUDED.employer,
    city = EXCLUDED.city, country = EXCLUDED.country, street = EXCLUDED.street,
    postcode = EXCLUDED.postcode, industry = EXCLUDED.industry,
    posted_at = EXCLUDED.posted_at, closes_at = EXCLUDED.closes_at,
    summary = EXCLUDED.summary, description = EXCLUDED.description,
    embed_text = EXCLUDED.embed_text, html_file = EXCLUDED.html_file,
    last_seen = now(), extracted_at = now(),
    -- If the text we embed has changed, the stored vector no longer describes
    -- this ad. Clearing it makes the embedder redo just those rows.
    embedding = CASE
        WHEN positions.embed_text IS DISTINCT FROM EXCLUDED.embed_text THEN NULL
        ELSE positions.embedding
    END,
    embedded_at = CASE
        WHEN positions.embed_text IS DISTINCT FROM EXCLUDED.embed_text THEN NULL
        ELSE positions.embedded_at
    END
"""

def day(value, fmt="%Y-%m-%d"):
    """Format a date, or say so when there isn't one. Plenty of ads give no
    closing date at all, and a missing date should read as missing rather than
    stop the report."""
    return value.strftime(fmt) if value else "(none)"


def check():
    """Report what actually landed in the table, so it can be judged rather than
    taken on trust. Reads only; writes nothing."""
    with psycopg.connect(DSN) as conn:
        total = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
        if not total:
            print("the table is empty -- run: python extract.py --all")
            return

        print(f"\n=== {total} row(s) in the table")

        print("\n--- coverage: how many rows actually got each field")
        fields = ["title", "employer", "city", "country", "street", "postcode",
                  "industry", "posted_at", "closes_at", "summary", "description",
                  "embed_text", "embedding"]
        counts = conn.execute(
            "SELECT " + ", ".join(f"count({f})" for f in fields) + " FROM positions"
        ).fetchone()
        for field, filled in zip(fields, counts):
            flag = "" if filled == total else "   <-- missing some"
            print(f"  {field:12} {filled:>5} / {total}  {100 * filled // total:>3}%{flag}")

        print("\n--- countries (nonsense here means the parsing is wrong)")
        for country, number in conn.execute(
            "SELECT coalesce(country, '(none)'), count(*) FROM positions "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
        ).fetchall():
            print(f"  {number:>5}  {country}")

        print("\n--- advert length in characters")
        low, mid, high = conn.execute(
            "SELECT min(length(description)),"
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY length(description)),"
            "       max(length(description)) FROM positions WHERE description <> ''"
        ).fetchone()
        print(f"  shortest {low:,}   typical {int(mid):,}   longest {high:,}")
        empty = conn.execute(
            "SELECT count(*) FROM positions WHERE description IS NULL OR description = ''"
        ).fetchone()[0]
        print(f"  {empty} row(s) with no advert text at all")

        print("\n--- dates")
        posted_lo, posted_hi, closes_lo, closes_hi = conn.execute(
            "SELECT min(posted_at), max(posted_at), min(closes_at), max(closes_at) "
            "FROM positions"
        ).fetchone()
        print(f"  posted between {day(posted_lo)} and {day(posted_hi)}")
        print(f"  closes between {day(closes_lo)} and {day(closes_hi)}")

        still_open, no_date, absurd = conn.execute(
            "SELECT count(*) FILTER (WHERE closes_at > now()),"
            "       count(*) FILTER (WHERE closes_at IS NULL),"
            "       count(*) FILTER (WHERE closes_at > now() + interval '5 years')"
            "  FROM positions"
        ).fetchone()
        print(f"  {still_open} not yet past their closing date, "
              f"{total - still_open - no_date} already closed, "
              f"{no_date} with no closing date given")
        if absurd:
            # The employer typed the year wrong. Their data, not ours -- but it
            # would sort to the top of anything ordered by deadline.
            print(f"  {absurd} with a closing date more than 5 years away, "
                  f"almost certainly a typo on the site")

        # The part that actually proves it: open these links and compare.
        print("\n--- five at random. Open each link and check it against the page.")
        for row in conn.execute(
            "SELECT url, title, employer, city, country, closes_at, "
            "       left(description, 200) FROM positions ORDER BY random() LIMIT 5"
        ).fetchall():
            url, title, employer, city, country, closes, opening = row
            print(f"\n  {url}")
            print(f"     title     {title}")
            print(f"     employer  {employer}")
            print(f"     where     {city}, {country}")
            print(f"     closes    {day(closes, '%d %B %Y')}")
            print(f"     starts    {opening}...")


parser = argparse.ArgumentParser()
parser.add_argument("--one", action="store_true", help="read one file and print it")
parser.add_argument("--all", action="store_true", help="read every file into the table")
parser.add_argument("--check", action="store_true",
                    help="report what is in the table so it can be verified")
args = parser.parse_args()

if not (args.one or args.all or args.check):
    sys.exit("pick one: --one, --all or --check")

if args.check:
    check()
    sys.exit(0)

sites = yaml.safe_load((ROOT / "sites.yml").read_text(encoding="utf-8"))["sites"]

for site in sites:
    files = sorted((ROOT / "data" / "raw" / site["name"]).glob("*.html"))
    print(f"\n=== {site['name']}: {len(files)} file(s) on disk")

    if args.one:
        row = read(files[0])
        if row is None:
            sys.exit(f"no JobPosting block found in {files[0].name}")
        print(f"\nread {files[0].name}, nothing written\n")
        for key, value in row.items():
            text = str(value)
            if key in ("description", "embed_text", "summary"):
                print(f"  {key} ({len(text)} chars):")
                print(f"      {text[:700]}...")
            else:
                print(f"  {key:12} {text[:110]}")
        continue

    written = skipped = no_body = 0
    with psycopg.connect(DSN) as conn:
        for number, path in enumerate(files, 1):
            row = read(path)
            if row is None:
                skipped += 1
                continue
            # An ad with no advert text is not much use, and if this count is high
            # the share link is not the reliable source it appears to be.
            if not row["description"]:
                no_body += 1
            conn.execute(UPSERT, row)
            written += 1
            if number % 250 == 0:
                conn.commit()
                print(f"  ... {number}/{len(files)}")
        conn.commit()

    print(f"\n  {written} row(s) written")
    print(f"  {skipped} skipped, no JobPosting block")
    print(f"  {no_body} written with an empty description")
