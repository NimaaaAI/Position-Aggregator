# Position Aggregator

Downloads academic job ads from job boards, stores them locally, and makes them
searchable by meaning rather than keyword.

Nothing is filtered while collecting. The job is to get every ad onto disk exactly as the
server sent it; extracting, cleaning and searching happen afterwards over the saved
files. A mistake in parsing then costs a re-read of local files instead of another hour
of downloading.

## The pipeline

```
sitemap  →  scrape.py    →  data/raw/<site>/<id>.html      done
                               ↓
             extract.py  →  positions table                next
                               ↓
             embed.py    →  embedding column
                               ↓
             search.py   →  ask questions
```

## Status

Downloading works: 1,886 ads from academicpositions.com, with a nightly update. The
database exists and is empty.

Not built yet: extraction, embeddings, search.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests pyyaml beautifulsoup4
```

## Database

PostgreSQL with [pgvector](https://github.com/pgvector/pgvector), which adds a vector
column type so embeddings live alongside the job data.

```bash
brew install pgvector

psql -d postgres -c "CREATE ROLE positions LOGIN PASSWORD 'positions';"
createdb -O positions positions
psql -d positions -c "CREATE EXTENSION vector;"
psql -d positions -f schema.sql
```

`brew install` puts pgvector on the machine. `CREATE EXTENSION` switches it on inside one
database — other databases on the same server are unaffected.

Check it:

```bash
psql -d positions -P pager=off -c "\d positions"
```

You want `embedding | vector(768)` among the columns.

### The table

One table, `positions`, holding every ad and its embedding.
Full definition in [`schema.sql`](schema.sql). Four decisions worth knowing:

- **Key is `(source, source_id)`** — re-running the extractor updates rows instead of
  duplicating them, and a second job board can reuse the same ad numbers safely.
- **`embedding` is `vector(768)`** — 768 is fixed by `multilingual-e5-base`, the model
  that produces the vectors.
- **`embed_text` is stored** as well as the full description. It is the ~2,000 characters
  actually given to the model, kept so you can see what was compared when a result looks
  wrong, and so re-embedding can skip anything unchanged.
- **`closed_at` is set** when an ad leaves the sitemap, rather than deleting the row.

## Downloading

```bash
python scrape.py --list      # read the sitemap, save the URL list, print the count
python scrape.py --one       # download one ad, to check it before the rest
python scrape.py --all       # download everything not already downloaded
python scrape.py --update    # the nightly job: refresh, diff, download what is new
```

The first full download is about 1,890 pages at 2 seconds each — roughly an hour. It
resumes: each ad is saved as `data/raw/<site>/<id>.html` and anything already on disk is
skipped, so Ctrl-C and re-run costs at most the page in flight.

After that, `--update` is the only command needed. About two minutes.

## Configuration

Everything site-specific is in `sites.yml`. Adding a board is one more block.

```yaml
sites:
  - name: academicpositions
    sitemap_index: https://academicpositions.com/sitemap.xml
    sitemap_match: jobs          # which sitemap in the index holds the ads
    job_url_contains: /ad/       # what an ad URL looks like
    delay: 2                     # seconds between requests
```

## Why the sitemap

The site's own listing page cannot be scraped. It ships empty placeholder cards and loads
the jobs afterwards with JavaScript, so downloading it gives 17,000 lines of filter
sidebar and no jobs.

The sitemap has no such problem:

```
/sitemap.xml                     16 sitemaps listed
  └── .../jobs-0-xml             1,889 ad URLs
        └── /ad/<employer>/<year>/<title>/<id>
```

Individual ad pages are ordinary server-rendered HTML, and each carries a `JobPosting`
block in JSON-LD — title, employer, city, country, posted date and closing date, already
structured. The full description sits in the page body beside it.

## What downloading produces

| Path | What it is |
|---|---|
| `data/<site>_urls.txt` | Every ad URL from the latest sitemap |
| `data/<site>_urls_prev.txt` | The previous run's list, so a diff can be checked by hand |
| `data/<site>_closed.txt` | Ads that left the sitemap, with the date. These have closed |
| `data/<site>_dead.txt` | URLs the sitemap lists but the server 404s, so they are not retried |
| `data/raw/<site>/<id>.html` | One file per ad, exactly as served |

None of it is committed — see `.gitignore`. It is all regenerable from the sitemap.

## The nightly update

`--update` re-reads the sitemap and compares it to the saved list:

- **New URLs** are downloaded.
- **URLs that vanished** are recorded as closed. No download needed.
- **Everything else** is skipped.

A board like this adds a few dozen ads a day, so a nightly run is a couple of minutes.

A few URLs in any sitemap are already deleted and answer 404. Those are recorded and not
tried again, so the "to go" count reaches zero instead of retrying them forever.

## Next

**Extraction** — read each saved HTML file, pull out the JSON-LD and the description
text, write one row per position.

**Then embedding and search.** One vector per position. A search narrows 1,886 to about
50, a reranker narrows that to 8, and only those 8 are shown to a language model. That is
what keeps the context small enough to be cheap.
