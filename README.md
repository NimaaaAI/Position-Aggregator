# Position Aggregator

Collects academic job adverts into a local database and makes them searchable by
meaning, by keyword, and by asking a question in plain language.

Everything runs on one machine. Only the final written answer touches an API.

```
academicpositions.com
        ↓  scrape.py     download every advert           ⎫
        ↓  extract.py    HTML → rows, types, stopwords   ⎬ update.py
        ↓  embed.py      text → vectors, whole + chunked ⎭
        ↓
     search.py    three rankings fused, then reranked
        ↓
      ask.py      a written answer with links
        ↓
      chat.py     the same thing in a browser
```

Currently **1,972 positions**, 12,442 chunks, all searchable.

---

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Database** — PostgreSQL with [pgvector](https://github.com/pgvector/pgvector).

```bash
brew install pgvector

psql -d postgres -c "CREATE ROLE positions LOGIN PASSWORD 'positions';"
createdb -O positions positions
psql -d positions -c "CREATE EXTENSION vector;"
psql -d positions -f schema.sql
```

**API key**, only for written answers:

```bash
cp .env.example .env      # paste your key into LLM_API_KEY
```

The endpoint is OpenAI-compatible, so any provider of that shape works.

---

## Running it

**Get the data and keep it current:**

```bash
python update.py
```

Runs six steps, each skipping work already done. The first run takes about an hour;
every one after takes a couple of minutes.

```
1  scrape.py  --update      download what is new
2  extract.py --all         new HTML → rows
3  extract.py --types       work out phd / postdoc / professor
4  extract.py --stopwords   recount which words are too common to search for
5  embed.py   --all         one vector per position
6  embed.py   --chunks      several vectors per position
```

**Search:**

```bash
python search.py "funded PhD in machine learning"
python search.py "AI or ML PhD position" --type phd --rerank --limit 25
python search.py "doktorand maskininlärning"          # any language
```

**Ask a question:**

```bash
python ask.py "which AI PhD positions in Belgium close soonest?"
python ask.py --models
```

**Or use the browser:**

```bash
python chat.py                                        # opens localhost:8000
```

Answer on the left, everything the search found on the right with its scores.
**Search only** runs retrieval with no model, so it costs nothing.

---

## Flags

| Flag | Does |
|---|---|
| `--type phd` | Only PhD posts. Also `postdoc`, `professor`, `lecturer`, `researcher` |
| `--rerank` | Re-read results with the cross-encoder. Slower, much better |
| `--open` | Hide positions whose deadline has passed |
| `--limit N` | How many results. Default 60 |
| `--no-hybrid` | Turn off full-text, for comparison |
| `--no-chunks` | Score by the advert's opening only, for comparison |

---

## How search works

Four signals, and every one exists because something failed without it.

### 1. Position vectors — what the advert is about

One vector per position, built from title + employer + location + the first 1,500
characters. That is all `multilingual-e5-base` reads; feed it more and it silently
ignores the rest.

### 2. Chunk vectors — whether any passage matches

The average advert here is 5,976 characters and **89% are over 3,000**, so the vector
above covers roughly the opening quarter — much of which is employer boilerplate
repeated across every one of their postings.

So each advert is also split into 1,200-character pieces with 200 of overlap, and each
piece embedded separately. A position is then scored by its **best** piece.

That found a position titled only *"PhD Student (f/m/d)"*, opening with
*"#TRANSFORMINGTOMORROW: MAKE A DIFFERENCE WITH US"*, whose actual subject appeared
further down. Invisible before; `chunk 0.896` after.

**Both vectors are used, not one.** Chunks alone compress the scores — almost every
advert has *some* passage loosely matching a broad question — and positions the
opening-level search found comfortably fell below the cutoff. Each finds what the other
misses.

### 3. Full-text — whether an exact string appears

Embeddings capture meaning, which is why they are useless on strings that have none.
Asked for **"ERC Starting Grant"**, vector search returned *Starter Grant Programme
2027* and a wind-farm PhD in the top two, with the real answer fourth. With full-text
it is first. Same for **MSCA**: fourth to first.

Three tiers, strictest first:

| tier | query | matches |
|---|---|---|
| phrase | `erc <-> starting <-> grant` | adjacent, in order |
| all | `erc & starting & grant` | all present anywhere |
| any | `erc \| starting \| grant` | at least one present |

The phrase tier separates a hit from a coincidence: an advert about Aristotle contains
"ERC", "starting date" and "grant" in three different paragraphs and satisfies `all`.

**Stopwords are counted, not listed.** `ts_stat` measures how many adverts contain each
word; anything above 25% goes in a `stopwords` table — "of" 91%, "university" 77%,
"experience" 60%. Without this the `any` tier is useless: *"positions using PyTorch or
TensorFlow"* becomes `positions & using & pytorch & or & tensorflow`, which no advert
satisfies, so 53 adverts naming PyTorch were never found. ORing the same words is
worse — every advert contains "for", so most of the database comes back at the same
noise floor.

Counting rather than listing means it works for Swedish and German queries too, and
updates itself as the corpus grows.

### Fusing the three

Reciprocal rank fusion: each ranking contributes `1/(60 + place)`, so a position found
by two searches beats one found by one. **Ranks, not scores** — a cosine of 0.85 and a
`ts_rank` of 0.09 share no scale and cannot be added.

Results show all of them, with `--` where a search did not find it:

```
#1  IT:U PhD Student     vector --      chunk 0.896   ← only chunks found it
#3  Explainability       vector 0.861   chunk --      ← only position vectors
#10 climate models       vector --      chunk --  text 0.190   ← only full-text
```

### 4. Reranking

`bge-reranker-v2-m3` reads each (question, advert) pair **together**, rather than
comparing two summaries made separately. About 25ms each, so it runs on the survivors:

| | vector | rerank |
|---|---|---|
| AI/ML Biomedical Imaging | 0.868 | **0.935** |
| Ultrasound Imaging (a postdoc) | 0.868 | 0.374 |

Identical vector scores; the reranker separated them.

---

## Filtering by kind of post

`position_type` records whether an advert is a PhD, postdoc, professorship and so on,
read from its title in a dozen languages — `PhD`, `Doktorand`, `Doctoraatsbursaal`,
`Väitöskirjatutkija`. It is an array, because plenty of adverts genuinely offer both.

It is a **filter applied before ranking**, not a boost:

```sql
WHERE 'phd' = ANY(position_type)     -- 617 candidates, not 1,972
```

Ranking cannot supply this. Every AI-related advert scores 0.845–0.887 whatever the
job, because the subject is the whole document and the job type is one word in a title.

Asked for *"AI or ML PhD position"*, the top 40 held **16** PhD positions. With the
filter, **40**. The ranking did not change; the pool did.

`python extract.py --check-types` reports the distribution and checks it against a
hand-written pattern: 45 adverts have both "PhD" and something AI-related in the title,
and all 45 are classified as `phd`.

---

## Collection and extraction

The site's own listing page **cannot** be scraped — it ships empty placeholder cards
and loads jobs with JavaScript. Downloading it gives 17,000 lines of filter sidebar and
no jobs. The sitemap has no such problem.

Raw HTML is saved before anything is parsed. The description extraction was rewritten
three times; each attempt cost seconds instead of an hour.

Every advert carries a [schema.org](https://schema.org/JobPosting) `JobPosting` block —
title, employer, city, country, dates, already structured. Most job boards publish it,
so this should largely carry to the next site.

**The advert text comes from the page's own LinkedIn share link**, whose `summary`
parameter holds the complete advert and nothing else. The site had to separate the
advert from its own navigation in order to share it, so it already did the hard part.

Taking the page text and removing the furniture fails: the body also contains a region
picker naming every European country, and anything missed is silently welded onto the
description — that one alone would have put *"Sverige Norge Danmark Deutschland"* into
all 1,972 rows.

`python extract.py --check` reports coverage, countries, date ranges, and five random
positions with links, so extraction can be verified against the live pages.

---

## The written answer

Only the top ~10 reach the model, at roughly 2,300 tokens — under a tenth of a penny.
All 1,972 adverts would be 4.7 million tokens.

Three things the prompt had to get right:

- **The model never writes a URL.** It once turned `.../abdominal-aortic-aneurysm/`
  into `.../abdominal-aneurysm/` — a confident, broken link. It writes `[3]`; the
  address is filled in from the database afterwards.
- **It must account for every position given.** Told to "be helpful" it returned one of
  ten. Told *"there are 10, mention all 10"*, it covers all ten.
- **Coverage is checked, not assumed.** Anything left out is reported.

A frontier model is not needed. Retrieval and reranking have already chosen the
positions; the model only describes them.

---

## Known limitations

**The 0.25 stopword threshold is the one arbitrary number left.** The proper answer is
IDF — weight each word by `log(total/ndoc)` instead of cutting above a line, so "of"
scores 0.09 and "pytorch" 3.6 and no threshold exists. That is what BM25 does and what
`ts_rank` lacks. Not built.

**337 of 1,972 positions have no type.** Their titles say nothing useful — *"Innovative
Optical Sensing for Precision Storage of Fruit"*. They drop out of filtered searches.
That is the safer failure: a wrong type pollutes results, an absent one merely omits.

**Long adverts have an advantage in chunk search.** A 24-chunk advert has 24 chances at
a high score against a short one's two.

**One source.** Everything is built for more — `sites.yml`, the registry, the database
key — but only one board is wired in.

**No knowledge graph, no agent framework.** The pipeline is linear with no loops or
decisions. LangChain would wrap what already exists; a graph needs relationships
between documents, and job adverts are independent records.

---

## Files

| | |
|---|---|
| `scrape.py` | Sitemap → raw HTML on disk. The only file that touches the internet |
| `extract.py` | HTML → rows. Also `--types`, `--stopwords`, `--check` |
| `embed.py` | Rows → vectors. `--all` per position, `--chunks` per passage |
| `search.py` | Retrieval and reranking. Imported by the two below, never run alone |
| `ask.py` | A written answer with citations |
| `chat.py` | The web interface |
| `update.py` | Runs the six collection steps in order |
| `sites.yml` | Which boards, and how to read them |
| `schema.sql` | The tables. Re-runnable |

**Tables:** `positions` (one row per advert), `position_chunks` (~6.3 rows each),
`stopwords` (241 words, recomputed each update).

Downloaded pages live under `data/`, which is not committed — about 340 MB and
regenerable from the sitemap.

---

## Development

```bash
ruff check .          # before every commit
ruff check --fix .
```

Worth it for the `F` rules alone. It caught three `zip()` calls without `strict=`,
where a length mismatch would have paired rows with the wrong vector — no error, no
warning, wrong search results ever after.

No CI: nothing is deployed and nobody else commits.
