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

# One of the two places an advert body is published. See read() for why both are
# tried rather than one being chosen per site.
#
# The obvious approach -- take the page text and subtract the furniture -- does not
# work. The body also contains a region picker naming every European country, a
# language picker, three modals and a "Jobs from this employer" list of other
# people's adverts. Subtracting those means guessing every selector, and anything
# missed is silently glued onto the description: the region picker alone would put
# "Sverige Norge Danmark Deutschland" into all 1,886 rows, so a search for jobs in
# Germany would match every one of them.
SHARE_LINK = re.compile(r"linkedin\.com/shareArticle")

# How much of the description goes to the embedding model. e5 reads about 512
# tokens and silently ignores the rest, so more than this is wasted.
EMBED_CHARS = 1500


def json_ld_job(soup):
    """The JobPosting block, if the page has one.

    Sites publish the same block at different depths: academicpositions puts it at
    the top level, academictransfer wraps it in a WebPage as its "mainEntity". So
    each candidate is checked one level down as well -- a question of where the
    block sits, not of which site wrote it.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            for candidate in (item, item.get("mainEntity")):
                if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                    return candidate
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

    # jobLocation is one Place on some boards and a list of them on others; schema.org
    # allows either. A post advertised in several places keeps the first, because the
    # alternative is a row per location and the primary key does not allow that.
    location = job.get("jobLocation") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get("address") or {}
    organisation = job.get("hiringOrganization") or {}

    canonical = soup.find("link", rel="canonical")
    url = canonical["href"] if canonical and canonical.get("href") else ""

    # The advert body. Two places carry it, and each site fills in a different one:
    # academicpositions puts a 165-character blurb in the JSON-LD and the real advert
    # in its LinkedIn share link, academictransfer puts the whole advert in the JSON-LD
    # and no summary in its share link at all.
    #
    # So both are read and the longer wins. That needs no per-site rule, no name in
    # this file, and keeps working if either site changes which field it fills -- the
    # blurb can never beat the advert on length.
    bodies = [job.get("description") or ""]
    share = soup.find("a", href=SHARE_LINK)
    if share and share.get("href"):
        query = urllib.parse.urlparse(share["href"]).query
        bodies.append(urllib.parse.parse_qs(query).get("summary", [""])[0])

    bodies = [re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True)).strip()
              for html in bodies if html]
    description = max(bodies, key=len) if bodies else ""

    # The short blurb, from the tag every site writes for search engines. Taking it
    # from the JSON-LD instead would give the whole advert on sites that put it there.
    blurb = soup.find("meta", attrs={"name": "description"})

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
        # data/raw/<site>/<id>.html -- the folder scrape.py saved it into, which is
        # the site's name in sites.yml. Nothing to pass in and nothing to keep in step.
        "source": path.parent.name,
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
        "summary": (blurb.get("content") if blurb else None) or None,
        "description": description or None,
        "embed_text": embed_text or None,
        "html_file": str(path.relative_to(ROOT)),
    }


# What kind of post it is, read from the title alone. The adverts arrive in a dozen
# languages, so the patterns have to as well -- these are taken from titles actually
# present in the data.
#
# The title only, never the description. Falling back to the description when the
# title said nothing looked helpful and was not: an advert for an Institute Director
# came out as both PhD and postdoc because the director supervises them, a summer
# school likewise, and a Norwegian lecturer post was filed as "student" because its
# advert asks for a master's degree. A description mentions these words for a dozen
# reasons that have nothing to do with what is being advertised.
#
# Leaving a position untyped is the better failure. It then simply does not appear
# in a filtered search, whereas a wrong type actively pollutes one.
POSTDOC = re.compile(
    r"post[-\s]?doc|post[-\s]?doctoral|postdoktor|postdoctoraal|"
    r"post[-\s]?doctorant|chercheur post",
    re.IGNORECASE,
)

# "postdoctoral" must not read as "doctoral". Rather than fight it with lookarounds
# -- "postdoctoral" is safe but "post-doctoral" is not, because the hyphen is a word
# boundary -- postdoc matches are blanked out before this is applied.
PHD = re.compile(
    r"\b(ph\.?\s?d\.?\w*|doctoral|doctorate|pre[-\s]?doctoral|"
    r"doctoraal|doctoraats\w*|promovendus|promovendi|aio|"          # Dutch
    r"doktorand\w*|doktorgrad\w*|stipendiat\w*|"                    # Nordic
    r"promotionsstelle|promovierend\w*|"                            # German
    r"doctorant\w*|"                                                # French
    r"dottorand\w*|dottorato|"                                      # Italian
    r"doctorad\w*|doctores|predoctoral\w*|"                         # Spanish
    r"v[aä]it[oö]skirja\w*|tohtorikoulutettava\w*)\b",              # Finnish
    re.IGNORECASE,
)

PROFESSOR = re.compile(
    r"\b(professor\w*|professur\w*|hoogleraar|professeur\w*|professore|"
    r"catedr[aá]tico\w*|tenure[-\s]track|"
    r"f[oø]rsteamanuensis|amanuensis)\b",          # Norwegian associate professor
    re.IGNORECASE,
)

LECTURER = re.compile(
    r"\b(lecturer\w*|reader|adjunkt\w*|universit[aä]tslektor|universitetslektor|"
    r"h[oø]yskolelektor|h[oø]gskolelektor|lektor\w*|"
    r"ma[iî]tre de conf[eé]rences|docent\w*)\b",
    re.IGNORECASE,
)

# Only consulted when none of the above matched, so "doctoral researcher" stays a
# PhD rather than becoming both a PhD and a researcher.
FALLBACKS = [
    ("researcher", re.compile(
        r"\b(research(er|ers)?|research fellow|research associate|staff scientist|"
        r"scientist\w*|forskare|forsker|onderzoeker|wissenschaftliche\w*|"
        r"chercheur\w*|tutkija|tutkij\w*)\b", re.IGNORECASE)),
    ("engineer", re.compile(
        r"\b(engineer\w*|ingenieur\w*|ingenj[oö]r\w*|developer|programmer|"
        r"programmerare|programmeur|architect)\b", re.IGNORECASE)),
    ("student", re.compile(
        r"\b(intern|internship|undergraduate|studentassistent\w*|"
        r"student assistant|summer school)\b", re.IGNORECASE)),
    ("support", re.compile(
        r"\b(technician|technicus|administrator|coordinator|secretar\w+|"
        r"manager|officer|librarian|analyst|assistent\w*|specialist\w*|"
        r"asiantuntija)\b", re.IGNORECASE)),
]


def classify(title, description=None):
    """Which kinds of post the title advertises. Returns a sorted list, possibly
    empty. `description` is accepted and ignored -- see the note above the patterns."""
    if not title:
        return []

    found = set()

    # Blank out postdoc mentions before looking for PhD, so "post-doctoral" cannot
    # be read as "doctoral". The hyphen is a word boundary, so a plain \b would.
    without_postdoc = POSTDOC.sub(" ", title)

    if POSTDOC.search(title):
        found.add("postdoc")
    if PHD.search(without_postdoc):
        found.add("phd")
    if PROFESSOR.search(title):
        found.add("professor")
    if LECTURER.search(title):
        found.add("lecturer")

    # Only when nothing above matched, so "doctoral researcher" stays a PhD rather
    # than becoming a PhD and a researcher at once.
    if not found:
        for label, pattern in FALLBACKS:
            if pattern.search(title):
                found.add(label)
                break

    return sorted(found)


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


def classify_all(force=False):
    """Fill position_type for every row. Reads the database, writes one column."""
    where = "" if force else " WHERE position_type IS NULL"
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            f"SELECT source, source_id, title, description FROM positions{where}"
        ).fetchall()

        if not rows:
            print("every position already has a type -- use --force to redo them")
            return

        print(f"classifying {len(rows)} position(s)")
        counts, none_found = {}, []
        for source, source_id, title, description in rows:
            types = classify(title, description)
            if not types:
                none_found.append(title)
            for label in types or ["(none)"]:
                counts[label] = counts.get(label, 0) + 1
            conn.execute(
                "UPDATE positions SET position_type = %s "
                " WHERE source = %s AND source_id = %s",
                (types, source, source_id),
            )
        conn.commit()

    print()
    for label in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {label:12} {counts[label]:>5}")
    if none_found:
        print(f"\n  {len(none_found)} could not be placed, for example:")
        for title in none_found[:5]:
            print(f"    {(title or '')[:74]}")


def build_stopwords(share=0.25):
    """Count how many adverts contain each word, and record the common ones.

    A word present in a quarter of the corpus cannot help tell one advert from
    another, whatever language it is in. Removing such words is what lets a
    full-text query OR its terms without matching everything.
    """
    with psycopg.connect(DSN) as conn:
        total = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
        if not total:
            print("no positions yet -- run: python extract.py --all")
            return

        conn.execute("TRUNCATE stopwords")
        conn.execute(
            """
            INSERT INTO stopwords (word, ndoc, share)
            SELECT word, ndoc, ndoc::real / %s
              FROM ts_stat('SELECT tsv FROM positions')
             WHERE ndoc::real / %s >= %s
            """,
            (total, total, share),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT word, share FROM stopwords ORDER BY share DESC"
        ).fetchall()

    print(f"\n{len(rows)} word(s) appear in at least {share:.0%} of "
          f"{total} adverts\n")
    for word, word_share in rows[:40]:
        print(f"  {word_share:5.0%}  {word}")
    if len(rows) > 40:
        print(f"  ... and {len(rows) - 40} more")


def check_types():
    """What the classifier decided, with samples, so it can be judged."""
    with psycopg.connect(DSN) as conn:
        total = conn.execute("SELECT count(*) FROM positions").fetchone()[0]
        typed = conn.execute(
            "SELECT count(*) FROM positions WHERE position_type <> '{}'"
        ).fetchone()[0]
        print(f"\n{typed} of {total} positions have a type\n")

        for label, number in conn.execute(
            "SELECT unnest(position_type) AS t, count(*) FROM positions"
            " GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {label:12} {number:>5}")

        print("\n--- both PhD and postdoc")
        for (title,) in conn.execute(
            "SELECT left(title, 88) FROM positions"
            " WHERE position_type @> ARRAY['phd','postdoc'] LIMIT 5"
        ).fetchall():
            print(f"    {title}")

        # The measurement this column exists for: AI-related PhD positions, counted
        # by a hand-written pattern independent of the classifier.
        #
        # The "post" exclusion is the whole point. A word boundary sits inside
        # "post-doctoral", so a plain \mdoctoral counts postdocs as PhDs -- which is
        # exactly the trap classify() blanks out postdoc mentions to avoid. Written
        # without it, this test reported 53 targets and 4 misses, and all four
        # "misses" were postdocs the classifier had filed correctly.
        phd_like = (
            "title !~* 'post[-\\s]?doc'"
            " AND title ~* '\\m(phd|ph\\.d|doctoral|doctorate|doktorand|doctorant)'"
            " AND title ~* '\\m(ai|ml|artificial intelligence|machine learning|"
            "deep learning|neural|llm|nlp|computer vision|data science)\\M'"
        )
        target = conn.execute(
            f"SELECT count(*) FROM positions WHERE {phd_like}"
        ).fetchone()[0]
        caught = conn.execute(
            f"SELECT count(*) FROM positions"
            f" WHERE 'phd' = ANY(position_type) AND {phd_like}"
        ).fetchone()[0]
        print("\n--- the test case")
        print(f"    {target} AI/ML PhD positions by hand-written pattern")
        print(f"    {caught} of them classified as phd  "
              f"({'all' if caught == target else 'MISSING ' + str(target - caught)})")

        print("\n--- samples per type")
        for label, in conn.execute(
            "SELECT DISTINCT unnest(position_type) FROM positions ORDER BY 1"
        ).fetchall():
            samples = conn.execute(
                "SELECT left(title, 70) FROM positions"
                " WHERE %s = ANY(position_type) ORDER BY random() LIMIT 3",
                (label,),
            ).fetchall()
            print(f"\n  {label}")
            for (title,) in samples:
                print(f"    {title}")


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
        for field, filled in zip(fields, counts, strict=True):
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
parser.add_argument("--types", action="store_true",
                    help="work out what kind of post each one is (phd, postdoc, ...)")
parser.add_argument("--check-types", action="store_true",
                    help="report what the type classifier decided")
parser.add_argument("--stopwords", action="store_true",
                    help="count word frequencies and record the words too common "
                         "to be worth searching for")
parser.add_argument("--stopword-share", type=float, default=0.25,
                    help="a word in at least this fraction of adverts is a stopword")
parser.add_argument("--force", action="store_true",
                    help="with --all or --types, redo work already done")
args = parser.parse_args()

if not (args.one or args.all or args.check or args.types or args.check_types
        or args.stopwords):
    sys.exit("pick one: --one, --all, --types, --stopwords, --check or --check-types")

if args.stopwords:
    build_stopwords(share=args.stopword_share)
    sys.exit(0)

if args.check:
    check()
    sys.exit(0)

if args.types:
    classify_all(force=args.force)
    sys.exit(0)

if args.check_types:
    check_types()
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
        # Only read files that have not been extracted before. A downloaded page
        # never changes, so re-reading it produces the same row. New files arrive
        # from scrape.py --update and are not in this set.
        done = set()
        if not args.force:
            done = {
                row[0] for row in conn.execute(
                    "SELECT source_id FROM positions "
                    " WHERE source = %s AND extracted_at IS NOT NULL",
                    (site["name"],),
                ).fetchall()
            }

        files = [path for path in files if path.stem not in done]
        print(f"  {len(done)} already extracted, {len(files)} to do")
        if not files:
            print("  nothing to do -- use --force to re-read everything")
            continue

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
