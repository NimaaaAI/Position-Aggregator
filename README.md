# Position Aggregator

Downloads academic job ads from job boards and keeps a local copy of every one.

Nothing is filtered during collection. The job is to get the pages onto disk exactly as
the server sent them; extracting, cleaning and searching happen afterwards, over the
saved files. That way a mistake in parsing costs a re-read of local files rather than
another download.

## Status

Collection works. 1,886 ads from academicpositions.com are downloaded, the nightly
update is in place, and the database is ready to receive them.

Not built yet: extraction (HTML into rows), embeddings, search.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests pyyaml beautifulsoup4
```

## Database

PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension, which
adds a vector column type so embeddings can live alongside the job data.

```bash
psql -d postgres -c "CREATE ROLE positions LOGIN PASSWORD 'positions';"
createdb -O positions positions
psql -d positions -c "CREATE EXTENSION vector;"
psql -d positions -f schema.sql
```

Check it worked:

```bash
psql -d positions -P pager=off -c "\d positions"
```

You want to see `embedding | vector(768)` among the columns and
`positions_embedding_idx hnsw` among the indexes.

### If CREATE EXTENSION fails

`brew install pgvector` builds against **one** PostgreSQL version — currently 17 and 18.
If your running server is 16, the extension is installed but invisible to it, and
`pg_available_extensions` returns nothing for `vector`.

Check which server is actually live, since the `psql` client version can differ from it:

```bash
psql -d postgres -c "SHOW server_version;"
psql -d postgres -c "SHOW data_directory;"
```

If it is 16, build pgvector against 16 explicitly. `PG_CONFIG` is what points the build
at the right installation:

```bash
git clone --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector
make         PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
make install PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
```

No restart needed afterwards; a newly installed extension is visible immediately.

Two PostgreSQL versions installed at once is a common cause of confusion here. Only one
can hold port 5432 — the other fails to start with *"Address already in use"*, which
looks like a broken install but is only a port conflict.

### The table

One table, `positions`, holding every ad and its embedding. Key points:

- **Primary key is `(source, source_id)`** — re-running the extractor updates rows rather
  than duplicating them, and a second job board can reuse the same ad numbers without
  colliding.
- **`embedding` is `vector(768)`** — 768 is fixed by `multilingual-e5-base`, the model
  that produces the vectors.
- **`embed_text`** is stored as well as the full `description`. It is the ~2,000-character
  string actually handed to the model, kept so you can see exactly what was compared when
  a search result looks wrong, and so re-embedding can skip anything unchanged.
- **`closed_at`** is set when an ad drops out of the sitemap, rather than deleting the
  row. The history is worth keeping, and you can choose whether a search includes closed
  positions.

Full definition in [`schema.sql`](schema.sql).

## Running it

```bash
python scrape.py --list      # read the sitemap, save the URL list, print the count
python scrape.py --one       # download one ad, to check it before the rest
python scrape.py --all       # download everything not already downloaded
python scrape.py --update    # the nightly job: refresh, diff, download what is new
```

The first full download is about 1,890 pages at 2 seconds each — roughly an hour. It is
resumable: each ad is saved as `data/raw/<site>/<id>.html` and anything already on disk
is skipped, so Ctrl-C and re-run costs you at most the page in flight.

After that, `--update` is the only command you need. It takes about two minutes.

## Configuration

Everything site-specific lives in `sites.yml`. Adding a second board is one more block.

```yaml
sites:
  - name: academicpositions
    sitemap_index: https://academicpositions.com/sitemap.xml
    sitemap_match: jobs          # which sitemap in the index holds the ads
    job_url_contains: /ad/       # what an ad URL looks like
    delay: 2                     # seconds between requests
```

## How it finds the ads

The site's listing page at `/find-jobs` cannot be scraped. It ships ten empty
`placeholder-content` skeleton cards and loads the real jobs afterwards over a Livewire
AJAX call, which `requests` never makes. Downloading that page gives 17,000 lines of
filter sidebar and zero jobs.

The route that works is the sitemap:

```
/sitemap.xml                     16 sitemaps listed
  └── .../jobs-0-xml             1,889 ad URLs
        └── /ad/<employer>/<year>/<title>/<id>
```

Individual ad pages *are* server-rendered — 0 placeholder bars, about 16,000 characters
of real advert text — and each one carries a `JobPosting` block in JSON-LD:

```json
{"@type":"JobPosting",
 "title":"Assistant/Associate Professor ...",
 "datePosted":"2026-06-26T14:17:46+02:00",
 "validThrough":"2026-08-16T23:59:59+02:00",
 "jobLocation":{"address":{"addressLocality":"Aalborg","addressCountry":"Denmark"}},
 "hiringOrganization":{"name":"Aalborg University (AAU)"}}
```

Title, employer, city, country, posted date and closing date, already structured. The
full description sits in the page body alongside it.

## What each run produces

| Path | What it is |
|---|---|
| `data/<site>_urls.txt` | Every ad URL from the latest sitemap |
| `data/<site>_urls_prev.txt` | The previous run's list, so a diff can be checked by hand |
| `data/<site>_closed.txt` | Ads that dropped out of the sitemap, with the date. These have closed |
| `data/<site>_dead.txt` | URLs the sitemap lists but the server 404s, so they are not retried nightly |
| `data/raw/<site>/<id>.html` | One file per ad, exactly as served |

None of this is committed — see `.gitignore`. It is all regenerable from the sitemap.

## How the nightly update works

`--update` always re-reads the sitemap, then compares it to the saved list:

- **New URLs** are downloaded.
- **URLs that vanished** are recorded in `_closed.txt`. The ad has closed. Its HTML stays
  on disk; we keep history rather than deleting it.
- **Everything else** is skipped.

A board like this adds a few dozen ads a day, so a nightly run is roughly 40 downloads.

A few URLs in any sitemap are already deleted and answer 404. Those are recorded in
`_dead.txt` and not tried again, otherwise they would be retried every night forever and
the "to go" count would never reach zero.

## The pipeline

```
sitemap  →  scrape.py   →  data/raw/<site>/<id>.html      done
                              ↓
            extract.py  →  positions table                next
                              ↓
            embed.py    →  embedding column
                              ↓
            search.py   →  ask questions
```

Each stage after the download runs entirely over local files and the database. Getting a
field wrong costs a re-run of seconds, not another hour of downloading — which is the
whole reason the raw HTML is kept.

## Next

**Extraction.** Read each saved HTML file, pull out the JSON-LD block and the description
text, and write one row per position.

Each ad page carries a `JobPosting` block in JSON-LD, so title, employer, city, country,
posted date and closing date come out structured rather than scraped:

```json
{"@type":"JobPosting",
 "title":"Assistant/Associate Professor ...",
 "datePosted":"2026-06-26T14:17:46+02:00",
 "validThrough":"2026-08-16T23:59:59+02:00",
 "jobLocation":{"address":{"addressLocality":"Aalborg","addressCountry":"Denmark"}},
 "hiringOrganization":{"name":"Aalborg University (AAU)"}}
```

The full ~16,000-character description sits in the page body alongside it, mixed in with
the cookie banner and a region picker listing every European country. Separating the
advert from that furniture is the part that needs checking against real output rather
than assuming.

**Then embedding and search.** One vector per position, built from title, employer,
location and the opening of the description. Retrieval narrows 1,886 to about 50, a
reranker narrows that to 8, and only those 8 are ever shown to a language model — which
is what keeps the context small enough to be cheap.
