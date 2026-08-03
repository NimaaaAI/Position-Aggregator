# Position Aggregator

Collects academic job adverts into a local database and makes them searchable by
meaning, by keyword, and by asking a question in plain language.

Everything runs on one machine. Only the final written answer touches an API.

```
academicpositions.com
        ↓  scrape.py     download every advert          ⎫
        ↓  extract.py    HTML → database rows           ⎬ update.py
        ↓  embed.py      text → 768 numbers             ⎭
        ↓
     search.py    vector + full-text, then reranked
        ↓
      ask.py      a written answer with links
        ↓
      chat.py     the same thing in a browser
```

Currently **1,972 positions**, all searchable.

---

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Database** — PostgreSQL with [pgvector](https://github.com/pgvector/pgvector), which
adds a vector column type so embeddings live beside the job data.

```bash
brew install pgvector

psql -d postgres -c "CREATE ROLE positions LOGIN PASSWORD 'positions';"
createdb -O positions positions
psql -d positions -c "CREATE EXTENSION vector;"
psql -d positions -f schema.sql
```

**API key**, only needed for written answers:

```bash
cp .env.example .env      # paste your key into LLM_API_KEY
```

The endpoint is OpenAI-compatible, so any provider of that shape works.

---

## Running it

**Get the data, and keep it current:**

```bash
python update.py
```

Downloads what is new, extracts it, embeds it. Each step skips work already done, so the
first run takes about an hour and every one after takes a couple of minutes.

```
done in 75s
  positions   1972  (+21)
  embedded    1972  (+21)
  still open  1687
```

**Search from the terminal:**

```bash
python search.py "funded PhD in machine learning"
python search.py "AI or ML PhD position" --type phd --rerank --limit 25
python search.py "doktorand maskininlärning"          # any language
```

**Ask a question and get a written answer:**

```bash
python ask.py "which AI PhD positions in Belgium close soonest?"
python ask.py --models                                # what your key can reach
```

**Or use the browser:**

```bash
python chat.py                                        # opens localhost:8000
```

Answer on the left, everything the search found on the right with its scores.
**Search only** runs retrieval with no model, so it costs nothing.

---

## Useful flags

| Flag | Does |
|---|---|
| `--type phd` | Only PhD posts. Also `postdoc`, `professor`, `lecturer`, `researcher` |
| `--rerank` | Re-read results with the cross-encoder. Slower, much better |
| `--open` | Hide positions whose deadline has passed |
| `--limit N` | How many results |
| `--no-hybrid` | Vector search only, for comparison |

---

## Adding another job board

One block in `sites.yml`, one module in the same shape as the existing one.

```yaml
sites:
  - name: academicpositions
    sitemap_index: https://academicpositions.com/sitemap.xml
    sitemap_match: jobs          # which sitemap in the index holds the ads
    job_url_contains: /ad/       # what an ad URL looks like
    delay: 2                     # seconds between requests
```

---

## How it works

### Collection

The site's own listing page **cannot** be scraped — it ships empty placeholder cards and
loads the jobs with JavaScript afterwards. Downloading it gives 17,000 lines of filter
sidebar and no jobs.

The sitemap has no such problem, and lists every advert:

```
/sitemap.xml  →  .../jobs-0-xml  →  /ad/<employer>/<year>/<title>/<id>
```

Raw HTML is saved to disk before anything is parsed. Re-reading local files costs seconds;
re-downloading costs an hour.

### Extraction

Every advert carries a [schema.org](https://schema.org/JobPosting) `JobPosting` block —
title, employer, city, country, dates, already structured. Most job boards publish it, so
this should largely carry to the next site.

The advert text comes from the page's **own LinkedIn share link**, whose `summary`
parameter holds the complete advert and nothing else. The obvious alternative — take the
page text and remove the furniture — fails: the body also contains a region picker naming
every European country, and anything missed gets welded onto the description.

`python extract.py --check` reports coverage, countries, date ranges, and five random
positions with their links, so the extraction can be verified against the live pages.

### Search

Two searches run and their rankings are combined.

**Vector search** understands meaning, which is exactly why it is useless on strings that
have none. Asked for *"ERC Starting Grant"* it returned *Starter Grant Programme 2027* and
a wind-farm PhD in the top two, with the real answer fourth.

**Full-text search** matches literal strings — grant codes, acronyms, scheme names. With it
added, the right position is first.

It runs two queries, strict first: the words adjacent and in order, then all present
anywhere. There is deliberately no third tier ORing them: every advert contains "for" and
"position", so a question phrased as a sentence would match the whole database at the same
noise floor. Full-text abstains when it has nothing exact to say.

### Reranking

The cross-encoder re-reads each (question, advert) pair in full, rather than comparing two
summaries made separately. It costs ~25ms per position and is worth it:

| | vector | rerank |
|---|---|---|
| AI/ML Biomedical Imaging | 0.868 | **0.935** |
| Ultrasound Imaging (a postdoc) | 0.868 | 0.374 |

Identical vector scores; the reranker separated them.

### Position type

`position_type` records what kind of job each advert is, read from its title in a dozen
languages. It is a **filter applied before ranking**, not a boost:

```sql
WHERE 'phd' = ANY(position_type)     -- 617 candidates, not 1,972
```

This matters because ranking cannot supply it. Every AI-related advert scores between 0.845
and 0.887 whatever the job, since the subject is the whole document and the job type is one
word in a title.

Asked for *"AI or ML PhD position"*, the top 40 contained **16** PhD positions. With the
filter, **40**. The ranking did not change — the pool did.

### The written answer

Only the top ~10 positions reach the model, at roughly 2,300 tokens — under a tenth of a
penny. All 1,972 adverts would be 4.7 million tokens.

Three things the prompt had to get right:

- **The model never writes a URL.** It once turned `.../abdominal-aortic-aneurysm/` into
  `.../abdominal-aneurysm/` — a confident, broken link. It writes `[3]`; the address is
  filled in from the database afterwards.
- **It must account for every position given.** Told to "be helpful" it returned one of
  ten. Told *"there are 10, mention all 10"*, it covers all ten.
- **Coverage is checked, not assumed.** Anything left out is reported.

A frontier model is not needed. Retrieval and reranking have already chosen the positions;
the model only describes them.

---

## Known limitations

**Only the first ~1,500 characters of each advert are embedded**, so something stated
halfway down a long advert is invisible to *meaning-based* search. Full-text already indexes
the whole description, so it is only that half which is affected.

**337 of 1,972 positions have no type.** Their titles say nothing useful — *"Innovative
Optical Sensing for Precision Storage of Fruit"*. They drop out of filtered searches. That
is the safer failure: a wrong type would pollute results, an absent one merely omits.

**One source.** Everything is built for more — `sites.yml`, the registry, the database key
— but only one board is wired in.

**No knowledge graph, no agent framework.** The pipeline is four linear steps with no loops
or decisions. LangChain would wrap what already exists; a graph needs relationships between
documents, and job adverts are independent records.

---

## Files

| | |
|---|---|
| `scrape.py` | Sitemap → raw HTML on disk |
| `extract.py` | HTML → database rows. Also `--types` and `--check` |
| `embed.py` | Rows → 768-number vectors |
| `search.py` | Hybrid retrieval and reranking. Imported by the two below |
| `ask.py` | A written answer with citations |
| `chat.py` | The web interface |
| `update.py` | Runs the first three in order |
| `sites.yml` | Which boards, and how to read them |
| `schema.sql` | The table. Re-runnable |

Downloaded pages and URL lists live under `data/`, which is not committed — about 340 MB,
and regenerable from the sitemap.

---

## Development

```bash
ruff check .          # before every commit
ruff check --fix .
```

Worth it for the `F` rules alone. It caught three `zip()` calls without `strict=`, where a
length mismatch would have paired rows with the wrong vector — no error, no warning, wrong
search results ever after.

No CI: nothing is deployed and nobody else commits.
