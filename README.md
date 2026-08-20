<div align="center">

# 🎓 Position Aggregator

**Every academic job advert, in one local database — searchable by meaning, by keyword, or by asking.**

![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Postgres](https://img.shields.io/badge/postgres-16-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-0.8-000000)
![Sources](https://img.shields.io/badge/sources-5-blueviolet)
![Positions](https://img.shields.io/badge/positions-14%2C743-success)
![Chunks](https://img.shields.io/badge/chunks-61%2C785-success)
![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)

</div>

---

## 🔄 Flow

```
   five job boards — see Sources
            │
   ┌────────▼────────┐
   │   scrape.py     │  sitemaps and listings → raw HTML on disk
   ├─────────────────┤
   │   extract.py    │  HTML → rows · types · countries · stopwords  ┐
   ├─────────────────┤                                               ├─ update.py
   │   embed.py      │  text → vectors, whole and chunked            ┘
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

| Board | Positions | PhD share | Coverage |
|---|---|---|---|
| `euraxess` | 8,127 | **25%** | EU-wide. The only one reaching Italy, Spain, Poland, Czechia |
| `jobsacuk` | 2,565 | 8% | UK and international, every university role including admin |
| `academicpositions` | 2,132 | — | Europe-wide, research posts |
| `academictransfer` | 964 | — | Netherlands, research posts |
| `naturecareers` | 955 | 6% | Global — the only one reaching the US in any number |

The PhD share is worth knowing before adding a sixth. EURAXESS is a researcher portal
and a quarter of it is doctoral; jobs.ac.uk and Nature Careers are general boards where
most adverts are faculty, admin or recruitment drives. All of it is kept — the type
filter decides what you see — but 900 adverts do not mean 900 candidates.

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
| A board too slow to update every time | `--skip` / `--site` on the download |
| Sitemap URLs with or without `https://` | added when missing |
| Ad id numeric (`358334`) or alphanumeric (`DQH648`) | `id_pattern` per board |
| `JobPosting` at the top level, or nested under `mainEntity` | both are checked |
| No `JobPosting` at all — facts in a `<dt>`/`<dd>` list | `fields` maps label → column |
| `JobPosting` present but incomplete | the same `fields` list fills the blanks |
| A whole address under one label — `Hangzhou, Zhejiang, China` | the city is the part before the first comma |
| Advert body in the JSON-LD, the share link, or the page | all read, **longest wins** |
| `jobLocation` a single place, or a list of them | first entry taken |

### On EURAXESS

Two things are worth stating plainly.

Its robots.txt disallows `/jobs/*`, and it is downloaded anyway — the owner's decision,
for one person's job search. The terms: a 3-second delay (the slowest here), an honest
User-Agent, `Retry-After` honoured when the server rate-limits, and no attempt to
defeat any control. A refusal stops the crawl rather than being worked around.

It is also the only board with **no** structured job data, which is why `extract.py`
has a definition-list fallback at all — its facts live in a `<dt>`/`<dd>` list, and
`fields` in `sites.yml` says which label holds which column.

It is also the slow one to update, which is why `update.py` can leave it out. See
[Collect and stay current](#collect-and-stay-current).

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
| 4 | each board's country spelling → ISO code | `extract.py --countries` |
| 5 | recount stopwords | `extract.py --stopwords` |
| 6 | one vector per position | `embed.py --all` |
| 7 | one vector per passage | `embed.py --chunks` |

Every step skips work already done.

**Three of the four boards are quick; EURAXESS is not.** The others publish a sitemap,
so finding out what is new means reading one file. EURAXESS publishes none, so its
URLs can only be found by walking 830 listing pages — most of an hour, with rate-limit
pauses, whether anything changed or not.

```bash
python update.py --skip euraxess     # the three fast boards, ~2 minutes
python update.py                     # all four, allow an evening
python update.py --site euraxess     # only the slow one
python update.py --no-scrape         # download nothing, process what is on disk
```

`--skip` and `--site` apply **only to downloading**. Pages already on disk for a
skipped board are still extracted, embedded and chunked, so a partial run never leaves
the database out of step with the files. A name matching no board stops the run rather
than silently skipping nothing.

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
python chat.py            # → http://127.0.0.1:8001
```

Asks you to sign in first. `/register` requests an account, `/admin` approves them.
`--add-user <name> --admin` makes the first administrator from the command line.

Answer on the left, every retrieved position with its scores on the right — each with
its opening line, which board it came from, and how long is left to apply.

The type dropdown, **open only** and **merge copies** are the same as `--type`,
`--open` and `--no-dedupe` below. Open-only and merging start on; the terminal needs
`--open` explicitly and merges unless told not to.

### Flags

| Flag | Effect |
|---|---|
| `--type phd` | Only PhD posts. Also `postdoc`, `professor`, `lecturer`, `researcher` |
| `--country IT` | Only that country, by ISO code. Applied before ranking |
| `--open` | Hide positions whose deadline has passed |
| `--rerank` | Re-score results with the cross-encoder |
| `--limit N` | Results to retrieve. Default `60` |
| `--no-dedupe` | Show every board's copy of a job separately |
| `--no-hybrid` | Disable full-text |
| `--no-chunks` | Disable chunk vectors |

---

## 🔐 Access

```
/register   choose a username, email and password   →  pending
/admin      an administrator approves               →  active
/login      sign in                                 →  the search page
```

Anyone may fill in the form; **nobody may use the result until an administrator
approves it**. An unapproved account is a queue entry, not a way in. The first
administrator is made from the command line, because there is nobody to approve them:

```bash
python chat.py --add-user nima --admin       # prompts twice, echoes nothing
```

**Passwords** are `scrypt(password, per-user salt)` from `hashlib` — standard library,
no dependency. The password itself is never stored, and two people who choose the same
one still store different bytes.

**Sessions** are a random token in an `HttpOnly` cookie; everything real about the
session lives in the `sessions` table, so deleting a row signs that browser out on its
next request. That also means restarting the server signs nobody out.

**Every endpoint checks** — a redirect on `/` would be pointless if `/api/search` still
answered anyone who asked.

### The admin page

`/admin`, for administrators only.

| | |
|---|---|
| **People** | state, asks today, editable daily limit, lifetime asks and tokens |
| **Signed in now** | live sessions with IP and browser |
| **Recent activity** | the last 60 questions — who, what, results, tokens, time |

Approve · Disable · Sign out · Remove. **Disabling deletes that person's sessions**,
or a disabled account keeps working until its cookie happens to expire. You cannot
disable or remove yourself.

### The daily limit

Only the written answer costs anything, so only `Ask` is capped — **50 a day per
person** by default, editable per user in the panel. Searching is free and stays
unlimited, so hitting the limit degrades the service rather than stopping it.

---

## 🌍 Putting it online

Two terminals. The first serves, the second exposes.

```bash
python chat.py                                  # 127.0.0.1:8001
ssh -R 80:127.0.0.1:8001 nokey@localhost.run    # prints an https://….lhr.life URL
```

That is the whole thing — no port opened on the router, no public IP, no certificate
to renew. The models stay on this machine; only HTTP crosses the tunnel.

**Use `127.0.0.1`, not `localhost`**, in the `-R` argument. `localhost` can resolve to
IPv6 `::1` first, and a server bound to `0.0.0.0` is IPv4 only — the tunnel then knocks
on a door nobody is behind and reports `connect_to localhost port 8001: failed`.

To reach it from another device on the same wifi instead, no tunnel is needed:

```bash
python chat.py --host 0.0.0.0        # then http://<this-mac's-LAN-IP>:8001
```

The free tunnel gives a new URL each time and dies with the terminal. A stable name
needs an account and an SSH key.

<details>
<summary>Why not Cloudflare Tunnel</summary>

It was the first choice, and it does not work from this connection. Two faults, in
order:

**Its DNS lookups go nowhere.** `cloudflared` is written in Go, and Go must use its own
resolver for SRV records — `getaddrinfo` cannot return them. That resolver reads
`/etc/resolv.conf`, which on macOS is a placeholder containing no nameserver, so every
lookup goes to localhost and is refused. `GODEBUG=netdns=cgo` does not help, for the
same reason. Appending `nameserver 8.8.4.4` to that file fixes it and changes nothing
else — macOS itself does not read it — but the file is regenerated on every network
change, so the fix does not survive a reboot.

**Then port 7844 is blocked.** With DNS finally working, the pre-checks still failed:

```
DNS Resolution    PASS
UDP Connectivity  FAIL   QUIC connection failed
TCP Connectivity  FAIL   HTTP/2 connection is blocked or unreachable
```

Cloudflare Tunnel speaks only on 7844. `localhost.run` uses SSH on port 22 and `ngrok`
uses 443 — ports that carry ordinary traffic and are therefore open almost everywhere.
That is the whole reason they work here and Cloudflare does not.

</details>

---

## 🌐 One country, three spellings

Filtering by country cannot use the country. The boards disagree:

```
NL                909      academictransfer
The Netherlands   170      academicpositions
Netherlands        71      euraxess
```

One country, three menu entries, and whichever you picked would hide the other two
thirds of it. So `country_code` holds the ISO 3166 alpha-2 code and everything filters
on that, while `country` keeps whatever the board actually published, for display.

The mapping comes from **`pycountry`** — the ISO standard as a package — rather than a
list kept in this repository. A hand-written list would cover the four boards we happen
to have and fail silently on the fifth; this way `Deutschland`, `Suomi` and `Czech
Republic` all land correctly without anyone editing anything.

```bash
python extract.py --countries      # prints every mapping it makes
```

It prints rather than working quietly because its last resort is a fuzzy match, and a
fuzzy match can be wrong. Anything it cannot resolve is left `NULL` and reported, so it
simply never appears in the filter — failing to offer a country is harmless, offering
the wrong one is not. `'Europe'` is the current example: two adverts, not a country,
left alone.

Some boards publish the code already — Nature Careers writes `US`, `DE`, `CN` — and
those pass straight through. The work is only for the ones writing prose.

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
| `country_code` | `text` | ISO 3166 alpha-2, resolved from `country` |
| `tsv` | `tsvector` | generated, `'simple'` config · GIN indexed |
| `html_file`, `first_seen`, `last_seen` | | bookkeeping |
| `closed_at`, `extracted_at`, `embedded_at` | `timestamptz` | |

Two things are stored as each board writes them rather than normalised:

- **`country`** is written three different ways for the Netherlands alone — `NL`,
  `The Netherlands`, `Netherlands` — because each board writes it its own way. Kept
  verbatim for display; `country_code` is what anything filters on.
- **Many jobs appear on more than one board** — 363 of them, 753 rows. EURAXESS in
  particular re-lists adverts the national boards already carry. Every copy is kept,
  because they carry different deadlines and one often stays live after the other
  closes. Search collapses them for display instead; see below.

### `position_chunks` — ~4.2 rows per advert

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
| `email` | `text` | given at registration |
| `is_admin` | `boolean` | may reach `/admin` |
| `active` | `boolean` | **false until approved** — the pending flag and the off switch |
| `daily_ask_limit` | `integer` | written answers per day. Default 50 |
| `created_at`, `last_login` | `timestamptz` | |

| `sessions` | | |
|---|---|---|
| `token` | `text` | **primary key** — the value in the cookie |
| `username` | `text` | FK → `users` `ON DELETE CASCADE` |
| `expires_at`, `created_at`, `last_seen` | `timestamptz` | |
| `ip`, `user_agent` | `text` | where the sign-in came from |

### `activity` — one row per question

| Column | Type | |
|---|---|---|
| `username` | `text` | FK → `users` `ON DELETE CASCADE` |
| `at` | `timestamptz` | |
| `endpoint` | `text` | `search` costs nothing, `ask` calls a model |
| `question` | `text` | kept, so the panel can show what someone was doing |
| `results`, `ms` | `integer` | |
| `model`, `prompt_tokens`, `completion_tokens` | | `ask` only |

This is what the daily limit counts and what the admin page reports.

### `stopwords` — recomputed every update

| Column | Type | |
|---|---|---|
| `word` | `text` | **primary key** |
| `ndoc` | `integer` | adverts containing it |
| `share` | `real` | fraction of all adverts (kept above `0.25`) |

Currently 141 words, and it moves every time the corpus does — 241, 185, 176, then 141
as each board arrived. Which words are too common to search for is a property of the
data, so it is measured on every update rather than written down.

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
| `templates/` | `chat.html` search · `login.html` sign in · `register.html` request access · `admin.html` the panel |
| `update.py` | Runs the seven steps in order |
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
