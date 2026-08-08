<div align="center">

# 🎓 Position Aggregator

**Every academic job advert, in one local database — searchable by meaning, by keyword, or by asking.**

![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Postgres](https://img.shields.io/badge/postgres-16-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-0.8-000000)
![Sources](https://img.shields.io/badge/sources-4-blueviolet)
![Positions](https://img.shields.io/badge/positions-6%2C505-success)
![Chunks](https://img.shields.io/badge/chunks-31%2C012-success)
![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)

</div>

---

## 🔄 Flow

```
  academicpositions  academictransfer  jobs.ac.uk  euraxess
            └───────────────┴────┬───────────┴──────────┘
   ┌────────────────────────────▼────┐
   │   scrape.py     │  sitemap → raw HTML on disk
   ├─────────────────┤
   │   extract.py    │  HTML → rows · types · stopwords     ┐
   ├─────────────────┤                                      ├─ update.py
   │   embed.py      │  text → vectors, whole + chunked     ┘
   └────────┬────────┘
            │
   ┌────────▼────────┐
   │   search.py     │  ① position vectors  ② chunk vectors
   │                 │  ③ full-text  →  RRF  →  ④ rerank
   └────────┬────────┘
            │
      ┌─────┴─────┐
      ▼           ▼
   ask.py      chat.py
  terminal     browser
```

| Stage | Model | Runs |
|---|---|---|
| Embedding | `intfloat/multilingual-e5-base` · 768-dim | local, MPS |
| Reranking | `BAAI/bge-reranker-v2-m3` | local, MPS |
| Answer | `gpt-4o-mini` (any OpenAI-compatible endpoint) | API |

---

## 🌐 Sources

| Board | Positions | Coverage |
|---|---|---|
| `academicpositions` | 2,045 | Europe-wide, research posts |
| `academictransfer` | 913 | Netherlands, research posts |
| `jobsacuk` | 2,136 | UK and international, all university roles |
| `euraxess` | 1,411 | EU-wide — the only one reaching Italy, Spain, Poland, Czechia |

**No site is named anywhere in the code.** `sites.yml` holds everything specific to
one, and the scripts loop over it.

```yaml
  - name: jobsacuk
    sitemap_index: https://www.jobs.ac.uk/sitemapindex.xml
    sitemap_match: sitemap0             # which child sitemap holds the ads
    job_url_contains: /job/             # so ordinary pages are skipped
    id_pattern: '/job/([A-Za-z0-9]+)'   # where the ad's own id sits in the URL
    delay: 2                            # seconds between requests
```

Adding a board is a new block here, then `scrape.py --one` and `extract.py --one` to
check a real page parses before downloading thousands. Boards differ in ways the two
scripts already absorb, each from config rather than a branch in the code:

| Varies | Handled by |
|---|---|
| Ads listed in a sitemap, or only on paginated search pages | `sitemap_index` or `listing_url` |
| Sitemap URLs with or without `https://` | added when missing |
| Ad id numeric (`358334`) or alphanumeric (`DQH648`) | `id_pattern` per board |
| `JobPosting` at the top level, or nested under `mainEntity` | both are checked |
| No `JobPosting` at all — facts in a `<dt>`/`<dd>` list | `fields` maps label → column |
| Advert body in the JSON-LD, the share link, or the page | all read, **longest wins** |
| `jobLocation` a single place, or a list of them | first entry taken |

### On EURAXESS

Two things are worth stating plainly.

Its robots.txt disallows `/jobs/*`, and it is downloaded anyway — the owner's decision,
for one person's job search. The terms: a 3-second delay (the slowest here), an honest
User-Agent, `Retry-After` honoured when the server rate-limits, and no attempt to
defeat any control. A refusal stops the crawl rather than being worked around.

It is also the only board with **no** structured job data, which is why `extract.py`
has a definition-list fallback at all. Its 8,128 adverts are still being collected;
the count above is what has landed so far.

---

## ⚙️ Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Database**

```bash
brew install pgvector

psql -d postgres -c "CREATE ROLE positions LOGIN PASSWORD 'positions';"
createdb -O positions positions
psql -d positions -c "CREATE EXTENSION vector;"
psql -d positions -f schema.sql
```

**API key** — only needed for written answers.

```bash
cp .env.example .env      # paste your key into LLM_API_KEY
```

**An account**, because the web interface will not serve anyone without one.

```bash
python chat.py --add-user nima --admin
```

---

## ▶️ Running it

### Collect and stay current

```bash
python update.py
```

| # | Step | Command |
|---|---|---|
| 1 | download what is new | `scrape.py --update` |
| 2 | HTML → rows | `extract.py --all` |
| 3 | classify phd / postdoc / professor | `extract.py --types` |
| 4 | recount stopwords | `extract.py --stopwords` |
| 5 | one vector per position | `embed.py --all` |
| 6 | one vector per passage | `embed.py --chunks` |

Every step skips work already done. First run ≈ 1 hour, later runs ≈ 2 minutes.

### Search

```bash
python search.py "funded PhD in machine learning"
python search.py "AI or ML PhD position" --type phd --open --rerank --limit 40
python search.py "doktorand maskininlärning"
```

### Ask

```bash
python ask.py "which AI PhD positions in Belgium close soonest?"
python ask.py --models
```

### Browser

```bash
python chat.py            # → http://127.0.0.1:8000
```

Asks you to sign in first. `--add-user <name>` creates an account or resets a
password; `--admin` marks someone as an administrator.

Answer on the left, every retrieved position with its scores on the right — each with
its opening line, which board it came from, and how long is left to apply.

The type dropdown, **open only** and **merge copies** are the same as `--type`,
`--open` and `--no-dedupe` below. Open-only and merging start on; the terminal needs
`--open` explicitly and merges unless told not to.

### Flags

| Flag | Effect |
|---|---|
| `--type phd` | Only PhD posts. Also `postdoc`, `professor`, `lecturer`, `researcher` |
| `--open` | Hide positions whose deadline has passed |
| `--rerank` | Re-score results with the cross-encoder |
| `--limit N` | Results to retrieve. Default `60` |
| `--no-dedupe` | Show every board's copy of a job separately |
| `--no-hybrid` | Disable full-text |
| `--no-chunks` | Disable chunk vectors |

---

## 🔐 Access

Nobody reaches the search page without an account, and accounts are only ever made
from the command line. **There is no registration** — one fewer thing to defend.

```bash
python chat.py --add-user colleague          # prompts twice, echoes nothing
python chat.py --add-user colleague          # run again to reset that password
```

**Passwords** are stored as `scrypt(password, per-user salt)` using `hashlib` from the
standard library. The password itself is never written down, and two people who
choose the same one still store different bytes.

**Sessions** are a random token in an `HttpOnly` cookie; everything real about the
session lives in the `sessions` table. Deleting a row signs that browser out on its
next request, with nothing to do at the browser's end.

**Every endpoint checks**, not only the page — a redirect on `/` would be pointless
if `/api/search` still answered anyone who asked.

```sql
-- who has signed in, and from where
SELECT username, created_at, last_seen, ip FROM sessions ORDER BY last_seen DESC;

-- sign someone out now
DELETE FROM sessions WHERE username = 'colleague';
```

Bound to `127.0.0.1`, so it is reachable from this machine only.

---

## 🔗 The same job on several boards

Four boards overlap, so one job can occupy several of the ten slots the model sees.
Search shows one row and hangs the others off it as **also on …**, with their links
intact. `--no-dedupe` shows every copy.

```
same title  AND  same city  AND  different source  →  the same job
```

All three parts are needed, and each was added because of a case that broke without it:

| Part | Without it |
|---|---|
| source must **differ** | three IT:U ads all titled *"PhD Student (f/m/d)"* merge into one |
| city must **match** | six universities advertising *"Assistant Professor"* — Poznań, Bydgoszcz, Hong Kong, Nottingham — become one |
| employer is **not** compared | the same place is `Umeå universitet` on one board and `Umeå University` on another |

It runs **after** ranking and reranking, so the best-scoring copy is the one kept and
collapsing cannot change which positions were found. Nothing is deleted, and a
position with no city is never collapsed — failing to merge is harmless, merging
wrongly is not.

---

## 🗄️ Database

Three tables. `psql -d positions`

### `positions` — one row per advert

| Column | Type | |
|---|---|---|
| `source`, `source_id` | `text` | **primary key** — board + its own ad id |
| `url` | `text` | canonical link |
| `title`, `employer` | `text` | from JSON-LD |
| `city`, `country`, `street`, `postcode` | `text` | from JSON-LD, verbatim |
| `industry` | `text` | from JSON-LD, not published by every board |
| `summary` | `text` | the ~160-char blurb, from `<meta name="description">` |
| `posted_at`, `closes_at` | `timestamptz` | from JSON-LD |
| `description` | `text` | full advert body, ~6,000 chars |
| `embed_text` | `text` | exactly what was embedded |
| `embedding` | `vector(768)` | HNSW · `vector_cosine_ops` |
| `position_type` | `text[]` | `{phd}`, `{phd,postdoc}` … GIN indexed |
| `tsv` | `tsvector` | generated, `'simple'` config · GIN indexed |
| `html_file`, `first_seen`, `last_seen` | | bookkeeping |
| `closed_at`, `extracted_at`, `embedded_at` | `timestamptz` | |

Two things are stored as each board writes them rather than normalised:

- **`country`** is written three different ways for the Netherlands alone — `NL`,
  `The Netherlands`, `Netherlands` — because each board writes it its own way.
- **Many jobs appear on more than one board** — 363 of them, 753 rows. EURAXESS in
  particular re-lists adverts the national boards already carry. Every copy is kept,
  because they carry different deadlines and one often stays live after the other
  closes. Search collapses them for display instead; see below.

### `position_chunks` — ~4.8 rows per advert

| Column | Type | |
|---|---|---|
| `source`, `source_id`, `chunk_index` | | **primary key** · FK → `positions` `ON DELETE CASCADE` |
| `text` | `text` | 1,200 chars, 200 overlap, title prepended |
| `embedding` | `vector(768)` | HNSW · `vector_cosine_ops` |
| `embedded_at` | `timestamptz` | |

### `users` and `sessions` — who may use the web interface

| `users` | | |
|---|---|---|
| `username` | `text` | **primary key** |
| `password_hash`, `salt` | `bytea` | `scrypt`, never the password |
| `is_admin`, `active` | `boolean` | |
| `created_at`, `last_login` | `timestamptz` | |

| `sessions` | | |
|---|---|---|
| `token` | `text` | **primary key** — the value in the cookie |
| `username` | `text` | FK → `users` `ON DELETE CASCADE` |
| `expires_at`, `created_at`, `last_seen` | `timestamptz` | |
| `ip`, `user_agent` | `text` | where the sign-in came from |

### `stopwords` — recomputed every update

| Column | Type | |
|---|---|---|
| `word` | `text` | **primary key** |
| `ndoc` | `integer` | adverts containing it |
| `share` | `real` | fraction of all adverts (kept above `0.25`) |

Currently 176 words, and it moves every time the corpus does — 241, then 185, then
176 as each board arrived. Which words are too common to search for is a property of
the data, so it is measured on every update rather than written down.

<details>
<summary>Useful queries</summary>

```sql
-- where things stand, per board
SELECT source, count(*) AS total, count(embedding) AS embedded,
       count(*) FILTER (WHERE closes_at > now()) AS still_open
  FROM positions GROUP BY source;

-- open PhD positions by country
SELECT country, count(*) FROM positions
 WHERE 'phd' = ANY(position_type) AND (closes_at IS NULL OR closes_at > now())
 GROUP BY country ORDER BY 2 DESC;

-- the same job on both boards
SELECT lower(trim(title)) AS job, count(*), array_agg(DISTINCT source)
  FROM positions GROUP BY 1
 HAVING count(DISTINCT source) > 1 ORDER BY 2 DESC;

-- the most common words
SELECT word, share FROM stopwords ORDER BY share DESC LIMIT 20;
```

</details>

---

## 📁 Files

| File | |
|---|---|
| `scrape.py` | Sitemap → raw HTML. The only file that touches the internet |
| `extract.py` | HTML → rows. Also `--one`, `--types`, `--stopwords`, `--check` |
| `embed.py` | Rows → vectors. `--all` per position, `--chunks` per passage |
| `search.py` | Retrieval + reranking. Imported, not run alone |
| `ask.py` | Written answer with citations |
| `chat.py` | Web interface and sign-in. Also `--add-user` |
| `templates/` | `chat.html` the search page · `login.html` the sign-in page |
| `update.py` | Runs the six steps in order |
| `sites.yml` | Which boards, and how to read them. The only place a site is named |
| `schema.sql` | The tables. Re-runnable |

`data/raw/<board>/<id>.html` holds the downloaded pages — not committed, and
regenerable from the sitemaps. The folder name is where `extract.py` gets `source`.

---

## 🧹 Development

```bash
ruff check .
ruff check --fix .
```
