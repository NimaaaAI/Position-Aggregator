# Position Aggregator

Downloads academic job ads from job boards, stores them locally, and makes them
searchable by meaning rather than keyword.

Nothing is filtered while collecting. The job is to get every ad onto disk exactly as the
server sent it; extracting, cleaning and searching happen afterwards over the saved
files. A mistake in parsing then costs a re-read of local files instead of another hour
of downloading.

## The pipeline

```
sitemap  →  scrape.py    →  data/raw/<site>/<id>.html   ⎫
                               ↓                        ⎬  update.py
             extract.py  →  positions table             ⎪  runs these three
                               ↓                        ⎭
             embed.py    →  embedding column
                               ↓
             search.py   →  vector + full-text, reranked
                               ↓
             ask.py      →  a written answer
                               ↓
             chat.py     →  the same thing in a browser
```

## Status

Working end to end. 1,976 ads downloaded from academicpositions.com, 1,972 extracted,
embedded and searchable, with a web interface over the top. The four missing carry no
`JobPosting` block.

Not automated yet — `python update.py` is run by hand. A `launchd` job calling it nightly
is the remaining step.

## Keeping it up to date

One command:

```bash
python update.py              # download what is new, extract it, embed it
python update.py --no-scrape  # local steps only, after changing a parser
python update.py --quiet      # summary only, for a scheduler
```

A real run took 75 seconds: 21 new positions and 123 closures found in the sitemap, 21
pages downloaded, 21 extracted, 21 embedded.

```
done in 75s
  positions   1972  (+21)
  embedded    1972  (+21)
  still open  1687
```

Every step skips work it has already done, so a normal run handles only what has appeared
since the last one. The before-and-after counts are the check: if `embedded` lags
`positions`, something went wrong in the last step and it says so.

`update.py` runs the three scripts as separate processes rather than importing them. Each
stays a program in its own right, runnable and debuggable alone, and their output appears
exactly as it would by hand. A failing step does not stop the others — if downloading
breaks, extracting and embedding still finish whatever is on disk, and the summary names
what failed.

The individual steps, if you want to run one on its own:

```bash
python scrape.py  --update    # what is new, what has closed, download the new
python extract.py --all       # read only the newly downloaded files
python embed.py   --all       # embed only the rows without a vector
python extract.py --check     # report, to confirm it all landed
```

Ads that leave the sitemap have closed. Their HTML stays on disk and their row stays in
the table — the closing date already says they are past, and it is useful to be able to
see a position you missed. This accumulates: after a few runs there were 1,976 files on
disk against 1,786 ads in the sitemap, the difference being everything that has closed
since collection started.

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

## Extracting

Reads the saved HTML files and writes one row per position into the database.

```bash
python extract.py --one      # read one file, print what came out, write nothing
python extract.py --all      # read every file not yet extracted
python extract.py --check    # report what is in the table
python extract.py --all --force   # re-read everything, after changing the parser
```

Like the downloader, `--all` skips work already done — it only reads files with no row
in the table yet, so a nightly run handles just the new ads.

Nothing here touches the network. Fixing a parsing mistake costs a re-run of seconds.

### Where the fields come from

Every ad page carries a `JobPosting` block in [schema.org](https://schema.org/JobPosting)
JSON-LD — a standard search engines read, embedded in a `<script>` tag and invisible on
the page. Title, employer, city, country, posted date and closing date come straight out
of it, so nothing has to be guessed from markup. Most job boards publish the same block,
so this should largely carry over to the next site.

The advert text is taken from the page's **own LinkedIn share link**, whose `summary`
parameter holds the complete advert as HTML and nothing else.

That is deliberate. The obvious approach — take the page text and remove the furniture —
does not work: the body also holds a region picker naming every European country, a
language picker, three modals and a list of other people's adverts. Removing those means
guessing every selector, and anything missed is silently welded onto the description. The
region picker alone would have put *"Sverige Norge Danmark Deutschland"* into all 1,882
rows, so a search for jobs in Germany would match every one of them. Taking only what the
site itself packaged up for sharing avoids the whole problem.

### Checking it

`--check` exists so the extraction can be judged rather than trusted. It reports how many
rows got each field, the countries and advert lengths it found, the date ranges, and then
prints five positions at random with their links — open those and compare against the
live page.

## Embedding

Turns each position's `embed_text` into 768 numbers, so positions can be found by
meaning rather than by matching words.

```bash
pip install sentence-transformers pgvector

python embed.py --one          # embed one row and show the result
python embed.py --all          # embed every row without a vector
python embed.py --all --force  # redo everything
```

About a minute for 1,900 positions on the GPU, seconds for a nightly batch.

The model is `intfloat/multilingual-e5-base`, running locally — nothing is sent anywhere
and there is no API cost. Multilingual matters here: the adverts arrive in Swedish,
German, French, Dutch and Norwegian, and a question in English has to find them.

Three details:

- **e5 needs prefixes.** Documents are encoded as `passage: …` and searches as
  `query: …`. That is how the model was trained; leaving them off quietly makes results
  worse and nothing warns you. The search side must use `query: ` to match.
- **Vectors are normalised**, so a dot product is the cosine similarity — which is what
  the `vector_cosine_ops` index on the column expects.
- **HuggingFace is forced offline.** The model is already cached locally, but the library
  otherwise calls out to check for updates on every run, which is slow on a poor
  connection and fails outright without one.

Only rows with no vector are touched. Since `extract.py` clears the vector whenever
`embed_text` changes, re-extracting automatically queues exactly those rows for
re-embedding.

## Checking the database directly

```bash
psql -d positions -c "SELECT count(*), count(embedding) FROM positions;"

# must return no rows: proves the upsert cannot duplicate
psql -d positions -c "SELECT source, source_id, count(*) FROM positions
                      GROUP BY 1,2 HAVING count(*) > 1;"

# one row, all columns, long text cut short
psql -d positions -P pager=off -x -c "
SELECT source_id, url, title, employer, city, country, posted_at, closes_at,
       left(description, 300) AS description,
       substring(embedding::text, 1, 70) AS embedding
FROM positions LIMIT 2;"
```

`-x` prints each row as a vertical block, which is the only readable way to look at a
table this wide.

## Searching

```bash
python search.py "funded PhD in machine learning"
python search.py "PhD in artificial intelligence for medical imaging" --rerank
python search.py "quantum computing" --open --limit 20
python search.py "doktorand maskininlärning"        # any language works
python search.py "ERC Starting Grant" --no-hybrid   # vector only, for comparison
```

`--open` hides positions whose closing date has passed; without it they appear marked
`[CLOSED]`.

### Two searches at once

Every question runs through both a vector search and Postgres full-text search, and the
two rankings are combined by reciprocal rank fusion.

They fail in opposite directions. Embeddings capture meaning, which is exactly why they
are useless on strings that have none — a grant code, an acronym, a scheme name. Asked
for **"ERC Starting Grant"**, vector search alone returned *Starter Grant Programme 2027*
and a wind-farm PhD in the top two, with the real answer fourth. It understood "grant
funding for research" and had no way to know that ERC is an institution and *Starting*
distinguishes it from *Consolidator*.

With full-text added, the correct position is first. Same for **MSCA**: fourth to first.

The text side runs two queries, strict first:

| tier | query | matches |
|---|---|---|
| phrase | `erc <-> starting <-> grant` | the words adjacent, in order |
| all | `erc & starting & grant` | all present, anywhere in the advert |

The phrase tier is what separates a real hit from a coincidence: an advert about
Aristotle contains "ERC", "starting date" and "grant" in three different paragraphs and so
satisfies `all`, but only genuine ERC Starting Grant posts have the words together.

**There is deliberately no third tier ORing the words.** It sounds like useful breadth and
is not. Asked *"I am looking for an AI or ML PhD position, show me all of them"*, it
becomes `i | am | looking | for | … | position | …`, and every advert ever written contains
"for" and "position". Full-text then returned most of the database with every score at the
same 0.054 noise floor, and rank fusion promoted gender studies and post-colonial
literature into a machine learning search.

So full-text abstains when it has nothing exact to say. That is the division of labour: it
matches literal strings, the vector search handles meaning, and a question phrased as a
sentence is a job for the latter.

Fusion compares *positions* rather than scores, which matters because a cosine similarity
of 0.85 and a `ts_rank` of 0.09 are not on any common scale and cannot simply be added.

`--no-hybrid` turns the text side off, for comparing the two.

### Results that look wrong and are not

Searching *"ERC Starting Grant"* returns a postdoc on **Aristotle's De Anima**. That looks
like a failure until you read the advert:

> *"…embedded in several projects: the ERC Starting Grant FitMA at KU Leuven, the ERC
> Advanced Grant TIDA at Universität Tübingen…"*

It is funded by an ERC Starting Grant. Someone searching that phrase wants positions
funded by one, and the subject is beside the point. Likewise a *"MSCA"* search returns
adverts about error-correcting codes and composite materials — both MSCA-funded, both
naming the scheme only in the small print.

This is the thing full-text does that embeddings structurally cannot: find a funding
scheme mentioned in a paragraph that has nothing to do with the work itself. Such results
were dismissed as noise three times during development and were correct every time.

### The numbers

```
1,951 positions
   ↓   vector + full-text, fused by rank        milliseconds
  60   ← adjustable, the "retrieve" box
   ↓   reranker reads every one of them         ~1.5s
  60   reordered, all shown in the list
   ↓   the best of them
  10   ← the "to model" box                     ~2,300 tokens
```

Reranking is the only expensive stage, at roughly 25ms a position, and it covers
everything retrieved. Raising 60 to 200 is fine; it costs about five seconds.

### A limitation worth knowing

Ranking cannot distinguish a PhD post from a postdoc. Counting by hand, the database holds
**52 positions whose title says both "PhD" and something AI-related**. Asked for exactly
that, the search surfaced **7 of them in the top 40**.

The reason is visible in the scores: every result sits between 0.827 and 0.846 cosine
similarity. The embedding model considers several hundred adverts about equally "AI-ish",
because they are, and the job type is one word in a title against hundreds in a
description. Retrieving more helps a little; it does not fix the ranking signal.

The fix would be to extract `position_type` into a column and filter on it before ranking,
so PhD positions compete only with other PhD positions. Not built.

### Two more stages, and why they matter

The vector search compares your question against all 1,951 positions at once and is
effectively instant. The reranker, behind `--rerank`, then re-reads the best 50
properly and reorders them.

The difference is easy to see. For *"PhD in artificial intelligence for medical
imaging"*:

| Position | vector | rerank |
|---|---|---|
| Doctoral researcher in AI / ML Biomedical Imaging | 0.868 | **0.935** |
| PhD in Multi-Aperture Ultrasound Imaging | 0.862 | 0.555 |
| Postdoc in Computational Ultrasound Imaging | 0.868 | 0.374 |
| PhD Student (f/m/d), no subject given | 0.855 | 0.240 |

The first and third had **the same** vector score. The reranker separated them, because
it reads the question and the advert together rather than comparing two summaries made
independently — so it can notice that one is a postdoc about ultrasound hardware and the
other is a PhD about AI.

Notice the spread. Vector scores sit in a narrow 0.82–0.88 band and barely distinguish
anything; rerank scores run from 0.935 down to 0.087. The vector stage is for narrowing
1,951 to 50 cheaply. The reranker is for deciding which of those 50 actually answer the
question.

Both models run on this machine. Nothing is sent anywhere.

## Linting

```bash
pip install ruff
ruff check .          # find problems
ruff check --fix .    # fix what can be fixed safely
```

Worth having for the `F` rules alone — undefined names and unused imports. Two real bugs
during this project were exactly that: a constant referenced after being deleted, and a
helper used before it was written.

It also caught three `zip()` calls without `strict=`. That one matters: in `embed.py`, if
the model ever returned fewer vectors than texts, `zip` would stop at the shorter one and
pair every row after that point with the wrong vector — no error, no warning, quietly
wrong search results ever after. `strict=True` makes it crash instead.

There is no CI. Nothing is deployed and nobody else commits, so `ruff check .` before a
commit does the same job in a second.

## Not used, and why

**No knowledge graph.** Graphs earn their keep when an answer requires following
relationships between documents. Job adverts are independent, self-contained records —
there is nothing to traverse between them, so a graph would be machinery without a
payoff.

**No hybrid search yet.** Vector search is weak at exact strings: grant codes, acronyms
like MSCA or ERC, project names. Combining it with Postgres's built-in full-text search
would fix that and needs no new dependency. Worth adding when a real query fails rather
than in advance.

## Asking a language model

```bash
pip install openai python-dotenv
cp .env.example .env      # then paste your key into LLM_API_KEY

python ask.py "PhD in AI for medical imaging"
python ask.py "quantum computing" --show 60 --context 20
python ask.py --models    # what the key can reach, grouped by family
```

Two separate numbers:

- **`--show`** — how many positions are printed for you. Default 40.
- **`--context`** — how many the model is given to write about. Default 10.

Nothing is hidden. The model describes the head of a list you can read in full, and the
positions it was shown are marked with `*`.

### Three things that had to be got right

**The model never writes a URL.** It once turned
`.../abdominal-aortic-aneurysm/251224` into `.../abdominal-aneurysm/251224` — a
confident, broken link indistinguishable from a working one. A model does not copy text,
it regenerates it token by token, and long slugs are where that fails. So it writes `[3]`
and the URL is pasted in afterwards from the database row.

**It must account for every position it is given.** Told to "be generous", it returned
one position out of ten. Told there are *"10 positions, numbered [1] to [10], mention all
10"*, it covers all ten — close matches first, poor fits at the end with a line saying
why. The reader decides what is worth their time; that is not the model's job.

Curiously this made answers *cheaper*: 619 output tokens covering ten positions, against
693 covering six, because per-position paragraphs collapsed into one-liners.

**Coverage is verified, not assumed.** After each answer the citations are counted, and
anything left out is reported:

```
the model left out 2 of 10 positions it was given: [4], [9]
```

Everything else here can be checked — extraction against the live page, vector counts in
SQL, rerank scores against your own judgement. A silently dropped position was the one
failure that looked identical to success.

### What the model adds

Two of the ten results were titled only *"PhD Student (f/m/d)"*. The model read the
adverts and reported that they concern personalised technical medicine for Parkinson's
disease. Filtering on a job board cannot do that.

### Cost

About **3,000 tokens a question** — 2,400 in, 600 out — which is under a tenth of a penny
on `gpt-4o-mini`, roughly 1,300 questions per dollar.

`temperature` is 0: the same question gives the same answer. There is no writing to be
done, only a fixed list to describe.

Reasoning models are a poor fit here. `gpt-5-nano` spent 1,400–2,000 output tokens on
answers *shorter* than the 619 `gpt-4o-mini` needed, because it was thinking about a
ranking that had already been decided before it saw anything.

The endpoint is OpenAI-compatible, so any provider of that shape works — set
`LLM_BASE_URL` and `LLM_API_KEY` in `.env`. Nothing else in the project needs an API key;
retrieval and reranking are local.

`--models` groups what is available and marks models that cannot answer a question at
all. That distinction is not always obvious from the name: `gpt-4o-mini-tts` is
text-to-speech, not a smaller `gpt-4o-mini`.

### A strong model is not needed here

By the time the model is asked anything, retrieval and the reranker have already chosen
which eight positions matter. All that is left is summarising them and quoting the links.
`gpt-4o-mini` and similar are entirely sufficient, and cost a fraction of a frontier
model.

The work that decides answer quality — the embeddings and the reranker — happens on this
machine for free.

## The web interface

```bash
pip install fastapi uvicorn
python chat.py            # opens http://127.0.0.1:8000
```

Binds to localhost only. There is no login because there is nobody else, and nothing is
reachable from the network.

The answer is on the left, everything the search found on the right — with both scores
per position and the ones given to the model highlighted. Two buttons:

- **Search only** — retrieval with no model. No API key needed, no cost. This is how to
  watch the search work on its own.
- **Ask** — the full pipeline.

Timings and token counts appear under each answer, so the cost of a question is never a
mystery.

`chat.py` is a thin wrapper: retrieval is `search.retrieve()`, the answer is
`ask.answer()`. The browser and the terminal run the same code and cannot drift apart.

### Citations are links

The model writes `[3]`; the page turns that into a link using the URL from the database
row. The model never handles an address, so it cannot mistype one — and the prose stays
readable instead of carrying 120-character URLs.

On the terminal `ask.py` still pastes the URL inline, because there is nowhere else to
put it.

### Three stages, each catching what the last one missed

Asked for *"an AI or ML PhD position in Europe"*, the reranker put **Staff Scientist –
Embedded AI** first, scoring 0.9128 — the highest of all forty. The model moved it down,
saying it is a research post rather than a PhD, and kept it visible rather than dropping
it.

| Stage | Judges | Cost |
|---|---|---|
| Vector search | roughly on topic | free, milliseconds |
| Reranker | genuinely on topic | free, ~1 second |
| Language model | whether it is the *kind* of thing asked for | ~$0.0008 |

The reranker matched the subject perfectly and missed the job type. Nothing before the
model could have caught that, and nothing after it would have been cheap enough to.

## Next

**Better retrieval.** The pipeline works end to end; the remaining gains are in what gets
found, not in what happens afterwards. Known weaknesses:

- **Position type is not extracted.** PhD, postdoc, professor and staff scientist are all
  just text, which is why 7 of 52 AI/ML PhD positions reached the top 40. A
  `position_type` column, filtered before ranking, would fix it — and the 7-of-52 count
  is the test to measure it against.
- **Only the opening of each advert is embedded.** `embed_text` holds the first ~1,500
  characters, so anything stated further down is invisible to *semantic* search. Full-text
  already indexes the whole description, so this only affects meaning-based matching.
  Splitting long adverts into several vectors would fix it.
