"""The public website. FastAPI over Qdrant, served from a Hugging Face Space.

    uvicorn website_app:app --port 7860

Read-only and stateless. There are no accounts, no sessions, no admin page and no
writable database: everything chat.py keeps in Postgres is simply absent here, and
absent is the only way to be sure it cannot leak.

Searching is free and uncapped -- it costs a Qdrant query and some CPU, both of
which are already paid for. Writing an answer calls a paid API, so that has a
daily ceiling.

The prompt and its helpers are copied from ask.py rather than imported. ask.py
does `from search import retrieve`, which pulls in psycopg and pgvector; importing
it here would give the Space a Postgres driver it has no database for.
"""

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import website_search as search

# A no-op on the Space, where the settings arrive as real environment variables
# from the Space's secrets. Here so the same file can be run locally against a
# .env while it is being worked on.
load_dotenv()

ROOT = Path(__file__).parent

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.gapgpt.app/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "180"))

# How many written answers the whole site may produce in a day. Everyone shares
# it: there are no accounts, so there is no per-person anything.
#
# Held in memory, which means it resets when the Space restarts or wakes from
# sleep. That is a deliberate limit, not an oversight. The wall that actually
# guarantees the bill is the monthly cap on the provider account; this only stops
# one visitor spending a month's budget in an afternoon.
DAILY_ANSWERS = int(os.getenv("DAILY_ANSWERS", "100"))
_spent = {"day": None, "count": 0}
_countries = None

CONTEXT_CHARS = 600
CITED = re.compile(r"\[(\d+)\]")

SYSTEM = """You help someone find academic jobs.

You are given positions already selected from a database by a search system, strongest
first. Use only those. Never invent a position or a deadline.

Account for every position you are given. If you are handed ten, your answer mentions
all ten, each exactly once, by its number. This is not a preference -- a position you
leave out is one the reader will never see, and they cannot judge what they are not
shown.

Order them by how well they fit, best first, and group them if that helps to read:
a close match, a partial one, something adjacent, and at the end the ones that do not
really fit.

Every entry gives, in this order:

    [n] the title
        employer, city, country
        closes on the date given, or say no closing date was given
        one or two lines on what it is and how it relates to the question

The employer, the place and the closing date are the facts someone needs in order to
act, so they belong in every entry, including the poor fits. For a poor fit, say
plainly that it is a poor fit and why -- one line is enough. Never silently omit it.

Someone asking about AI wants to hear about every position involving AI, in any field,
and will decide for themselves whether AI in robotics or AI in biology suits them.
Someone asking about medical imaging wants the imaging work that has no AI in it too.

You are describing what the search found. Deciding what is worth their time is their
job, not yours.

Refer to positions by their number, like [3]. Quote the closing date when it matters.
"""

app = FastAPI(title="Position Aggregator")


def pretty(code):
    """ISO code -> the name people read. pycountry is already a dependency for
    extract.py, so this costs nothing new."""
    try:
        import pycountry
        found = pycountry.countries.get(alpha_2=code)
        return found.name if found else code
    except Exception:
        return code


def budget():
    """(spent, allowed) for today, rolling over at UTC midnight."""
    today = datetime.now(UTC).date().isoformat()
    if _spent["day"] != today:
        _spent["day"], _spent["count"] = today, 0
    return _spent["count"], DAILY_ANSWERS


def day(value):
    """A payload date is an RFC-3339 string, not a datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def as_context(results):
    """The positions as the model sees them: numbered, with the facts it must not
    invent, and enough of the advert to describe what the work is."""
    blocks = []
    for number, item in enumerate(results, 1):
        where = ", ".join(p for p in (item["city"], item["country"]) if p) or "not given"
        closes = day(item["closes_at"])
        blocks.append("\n".join([
            f"[{number}] {item['title']}",
            f"employer: {item['employer']}",
            f"location: {where}",
            f"closes: {closes:%d %B %Y}" if closes else "closes: not given",
            f"url: {item['url']}",
            f"advert: {' '.join((item['description'] or '').split())[:CONTEXT_CHARS]}",
        ]))
    return "\n\n".join(blocks)


def with_urls(text, results):
    """Put the real URL next to each citation the model made.

    A model does not copy text, it regenerates it token by token, so a long slug
    comes back subtly altered -- a confident, broken link that looks exactly like
    a working one. The model supplies the reference, the database the address.
    """
    seen = set()

    def replace(match):
        number = int(match.group(1))
        if not 1 <= number <= len(results) or number in seen:
            return match.group(0)
        seen.add(number)
        return f"{match.group(0)} {results[number - 1]['url']}"

    return CITED.sub(replace, text)


def public(item):
    """What a visitor is allowed to see.

    The advert body is stored so the reranker can score against it, but it is
    someone else's copyright. What goes out is a snippet and the link, which is
    what sending the reader to the source means.
    """
    body = " ".join((item.get("description") or "").split())
    return {
        "source": item["source"], "title": item["title"],
        "employer": item["employer"], "city": item["city"],
        "country": item["country"], "country_code": item["country_code"],
        "position_type": item["position_type"], "closes_at": item["closes_at"],
        "url": item["url"], "snippet": (item.get("summary") or body)[:300],
        "closed": bool(day(item["closes_at"])
                       and day(item["closes_at"]) < datetime.now(UTC)),
        "rerank_score": item.get("rerank_score"),
        "fused_score": item.get("fused_score"),
        "also_on": item.get("also_on", []),
    }


class Search(BaseModel):
    question: str
    show: int = 10
    open_only: bool = True
    position_type: str | None = None
    country: str | None = None
    rerank: bool = True
    dedupe: bool = True


class Browse(BaseModel):
    limit: int = 60
    offset: int = 0
    open_only: bool = True
    position_type: str | None = None
    country: str | None = None
    dedupe: bool = True


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "templates" / "website.html").read_text(encoding="utf-8")


@app.get("/api/stats")
def api_stats():
    spent, allowed = budget()
    return JSONResponse({**search.stats(),
                         "answers_used": spent, "answers_allowed": allowed})


@app.get("/api/countries")
def api_countries():
    """Countries with something open in them.

    Counted with Qdrant's facet API rather than by reading every payload: the
    question is "how many points per value of one field", which is exactly what a
    keyword payload index already knows. Cached for the life of the process --
    the collection only changes when the nightly build runs, and a Space that has
    been up since before it is a stale dropdown, not a wrong answer.
    """
    global _countries
    if _countries is None:
        hits = search.client().facet(
            collection_name=search.POSITIONS, key="country_code",
            facet_filter=search.where(open_only=True), limit=200,
        ).hits
        _countries = [{"code": h.value, "name": pretty(h.value), "count": h.count}
                      for h in hits if h.value]
    return JSONResponse({"countries": _countries})


@app.post("/api/search")
def api_search(body: Search):
    results = search.retrieve(
        body.question, limit=min(body.show, 50), open_only=body.open_only,
        position_type=body.position_type, country=body.country, rerank=body.rerank,
        dedupe=body.dedupe,
    )
    return JSONResponse({"positions": [public(item) for item in results]})


@app.post("/api/browse")
def api_browse(body: Browse):
    results, total = search.browse(
        limit=min(body.limit, 200), offset=body.offset, open_only=body.open_only,
        position_type=body.position_type, country=body.country, dedupe=body.dedupe,
    )
    return JSONResponse({"positions": [public(item) for item in results],
                         "total": total})


@app.post("/api/ask")
def api_ask(body: Search, request: Request):
    spent, allowed = budget()
    if spent >= allowed:
        return JSONResponse(
            {"error": f"the {allowed} written answers for today are used up. "
                      f"Search still works, and resets at midnight UTC."},
            status_code=429)
    if not API_KEY:
        return JSONResponse({"error": "no model key configured"}, status_code=503)

    results = search.retrieve(
        body.question, limit=min(body.show, 10), open_only=body.open_only,
        position_type=body.position_type, country=body.country, rerank=True,
    )
    if not results:
        return JSONResponse({"answer": "Nothing matched that.", "positions": []})

    from openai import OpenAI
    try:
        response = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=TIMEOUT,
                          max_retries=3).chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content":
                       f"Question: {body.question}\n\n"
                       f"There are {len(results)} positions below, numbered "
                       f"[1] to [{len(results)}].\n"
                       f"Your answer must mention all {len(results)} of them, "
                       f"each exactly once.\n\n{as_context(results)}"}],
            # Zero, not low. There is no writing to be done here, only a fixed
            # list to be described, and the same question should give the same
            # answer twice.
            temperature=0,
        )
    except Exception as error:
        print(f"model call failed: {error}", file=sys.stderr)
        return JSONResponse({"error": "the model did not answer"}, status_code=502)

    # Counted only once the call succeeded. A failed call cost nothing and should
    # not spend someone else's share of the day.
    _spent["count"] += 1
    text = response.choices[0].message.content or ""
    return JSONResponse({"answer": with_urls(text, results),
                         "positions": [public(item) for item in results],
                         "answers_left": allowed - _spent["count"]})
