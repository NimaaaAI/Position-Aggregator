# Position Aggregator

Downloads academic job ads from job boards and keeps a local copy of every one.

Nothing is filtered during collection. The job is to get the pages onto disk exactly as
the server sent them; extracting, cleaning and searching happen afterwards, over the
saved files. That way a mistake in parsing costs a re-read of local files rather than
another download.

## Status

Collection works. 1,886 ads from academicpositions.com are downloadable and the nightly
update is in place.

Not built yet: extraction (HTML into structured rows), storage, embeddings, search.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests pyyaml beautifulsoup4
```

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

## Next

Extraction: read each saved HTML file, pull the JSON-LD block and the description text
out of it, and write one clean row per position. That runs entirely over local files, so
it can be re-run and corrected without touching the network.
