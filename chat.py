"""A local web interface over the same pipeline the command line uses.

    python chat.py            then open http://127.0.0.1:8000

Binds to localhost only. This is one person's tool on one machine: there is no
login because there is nobody else, and nothing is reachable from the network.

Everything here is a thin wrapper. Retrieval is search.retrieve(), the answer is
ask.answer(), and the URLs are pasted in by ask.with_urls() -- the same code the
terminal runs, so the two cannot drift apart.
"""

import argparse
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from threading import Timer

import psycopg
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import ask
import search

ROOT = Path(__file__).parent
PAGE = ROOT / "templates" / "chat.html"

app = FastAPI(title="Position search", docs_url=None, redoc_url=None)


class Question(BaseModel):
    question: str
    model: str = ask.MODEL
    # How many the search returns and the reranker re-reads. All of these appear in
    # the list; the best `context` of them are given to the model.
    show: int = search.DEFAULT_LIMIT
    context: int = 10
    open_only: bool = False
    rerank: bool = True
    hybrid: bool = True
    # "phd", "postdoc", ... or None for any. Applied before ranking.
    position_type: str | None = None


def as_json(item):
    """One position, shaped for the browser. Dates become strings; nothing else
    survives JSON."""
    closes = item["closes_at"]
    return {
        "source_id": item["source_id"],
        "title": item["title"],
        "employer": item["employer"],
        "city": item["city"],
        "country": item["country"],
        "url": item["url"],
        "closes": closes.strftime("%d %b %Y") if closes else None,
        "closed": bool(closes and closes < datetime.now(UTC)),
        # Any of these can be absent: a position found by one search and not the
        # other has no score from the one that missed it, which is worth showing.
        "vector_score": (round(item["vector_score"], 4)
                         if item["vector_score"] is not None else None),
        "chunk_score": (round(item["chunk_score"], 4)
                        if item["chunk_score"] is not None else None),
        "text_score": (round(item["text_score"], 4)
                       if item["text_score"] is not None else None),
        "rerank_score": (round(item["rerank_score"], 4)
                         if item["rerank_score"] is not None else None),
        "snippet": " ".join((item["description"] or "").split())[:300],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@app.post("/api/search")
def api_search(request: Question):
    """Retrieval only. No model, no API key, no cost -- this is the endpoint for
    watching the search work on its own."""
    started = time.perf_counter()
    results = search.retrieve(
        request.question, limit=request.show,
        open_only=request.open_only, rerank=request.rerank,
        hybrid=request.hybrid, position_type=request.position_type or None,
    )
    elapsed = time.perf_counter() - started

    return JSONResponse({
        "positions": [as_json(item) for item in results],
        "timings": {"retrieval_ms": round(elapsed * 1000)},
    })


@app.post("/api/chat")
def api_chat(request: Question):
    started = time.perf_counter()
    results = search.retrieve(
        request.question, limit=request.show,
        open_only=request.open_only, rerank=request.rerank,
        hybrid=request.hybrid, position_type=request.position_type or None,
    )
    retrieval_ms = round((time.perf_counter() - started) * 1000)

    if not results:
        return JSONResponse({
            "answer": "Nothing in the database matches that.",
            "positions": [], "given": 0,
            "timings": {"retrieval_ms": retrieval_ms, "answer_ms": 0},
            "usage": None, "missing": [],
        })

    given = results[:request.context]

    started = time.perf_counter()
    try:
        text, usage = ask.answer(request.question, given, request.model)
    except SystemExit as stop:
        # ask.answer exits the process on failure, which is right for a script and
        # wrong for a server. Turn it back into a message.
        return JSONResponse({"error": str(stop)}, status_code=502)
    answer_ms = round((time.perf_counter() - started) * 1000)

    # Note: ask.with_urls() is deliberately NOT called here. On the terminal the URL
    # has to be pasted into the text because there is nowhere else to put it. In a
    # browser the citation itself becomes the link, which reads far better -- the
    # page turns [3] into an anchor pointing at positions[2].url. Either way the
    # address comes from the database and never from the model.

    # Which positions the model actually wrote about. It is told to cover all of
    # them; this reports whether it did.
    mentioned = {int(n) for n in ask.CITED.findall(text)}
    missing = [n for n in range(1, len(given) + 1) if n not in mentioned]

    return JSONResponse({
        "answer": text,
        "positions": [as_json(item) for item in results],
        "given": len(given),
        "missing": missing,
        "timings": {"retrieval_ms": retrieval_ms, "answer_ms": answer_ms},
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        } if usage else None,
    })


@app.get("/api/models")
def api_models():
    """The models this key can reach, chat ones only, grouped as ask.py groups
    them. Fetched live rather than hardcoded, so the list is real."""
    try:
        names = sorted(model.id for model in ask.client().models.list().data)
    except Exception as error:
        return JSONResponse({"error": str(error), "models": [ask.MODEL]})

    chat = [name for name in names if ask.is_chat(name)]
    grouped = []
    for label, _ in ask.FAMILIES:
        members = [name for name in chat if ask.family_of(name) == label]
        if members:
            grouped.append({"family": label, "models": members})

    return JSONResponse({
        "groups": grouped, "default": ask.MODEL, "cheap": list(ask.CHEAP),
    })


@app.get("/api/stats")
def api_stats():
    with psycopg.connect(search.DSN) as conn:
        total, embedded, countries, open_now = conn.execute(
            "SELECT count(*), count(embedding), count(DISTINCT country),"
            "       count(*) FILTER (WHERE closes_at > now())"
            "  FROM positions"
        ).fetchone()
        newest = conn.execute(
            "SELECT max(first_seen)::date::text FROM positions"
        ).fetchone()[0]
        top = conn.execute(
            "SELECT country, count(*) FROM positions"
            " WHERE country IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
        ).fetchall()

    return JSONResponse({
        "positions": total, "embedded": embedded, "countries": countries,
        "open": open_now, "newest": newest,
        "by_country": [{"country": c, "count": n} for c, n in top],
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    # Load the encoder now rather than during the first question, so the first
    # thing typed does not appear to hang for several seconds. The reranker loads
    # on first use; it is bigger and not every request needs it.
    print("loading the embedding model before serving")
    search.encoder()

    url = f"http://{args.host}:{args.port}"
    print(f"\nready at {url}\n")
    if not args.no_open:
        Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
