"""A local web interface over the same pipeline the command line uses.

    python chat.py            then open http://127.0.0.1:8000

Binds to localhost only. This is one person's tool on one machine: there is no
login because there is nobody else, and nothing is reachable from the network.

Everything here is a thin wrapper. Retrieval is search.retrieve(), the answer is
ask.answer(), and the URLs are pasted in by ask.with_urls() -- the same code the
terminal runs, so the two cannot drift apart.
"""

import argparse
import getpass
import hashlib
import hmac
import secrets
import sys
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Timer

import psycopg
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import ask
import search

ROOT = Path(__file__).parent
PAGE = ROOT / "templates" / "chat.html"
LOGIN_PAGE = ROOT / "templates" / "login.html"

COOKIE = "session"
SESSION_DAYS = 14

# scrypt's cost. n is the work factor; 2**14 takes roughly 0.1s here, which is
# nothing to type a password once and a great deal to try millions of them.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

app = FastAPI(title="Position search", docs_url=None, redoc_url=None)


def hashed(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


def signed_in(request):
    """The user this request belongs to, or None.

    The cookie carries only a token; whether it means anything is decided here,
    against the database. So a session ends the moment its row goes -- there is
    nothing cached and nothing to expire on the browser's side.
    """
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with psycopg.connect(search.DSN) as conn:
        row = conn.execute(
            "SELECT s.username, u.is_admin FROM sessions s"
            "  JOIN users u ON u.username = s.username"
            " WHERE s.token = %s AND s.expires_at > now() AND u.active",
            (token,),
        ).fetchone()
        if row:
            conn.execute("UPDATE sessions SET last_seen = now() WHERE token = %s",
                         (token,))
    return {"username": row[0], "is_admin": row[1]} if row else None


def add_user(username, is_admin):
    """Create a sign-in. Called from the command line, never over the web: there is
    no way to register, only to be given an account."""
    password = getpass.getpass(f"password for {username}: ")
    if password != getpass.getpass("repeat: "):
        sys.exit("the two passwords differ")
    if len(password) < 8:
        sys.exit("at least 8 characters, please")

    salt = secrets.token_bytes(16)
    with psycopg.connect(search.DSN) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash = EXCLUDED.password_hash,"
            "       salt = EXCLUDED.salt, is_admin = EXCLUDED.is_admin",
            (username, hashed(password, salt), salt, is_admin),
        )
    print(f"{username} can now sign in{' as an admin' if is_admin else ''}")


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
    # Collapse the same job carried by several boards into one row.
    dedupe: bool = True
    # "phd", "postdoc", ... or None for any. Applied before ranking.
    position_type: str | None = None


def as_json(item):
    """One position, shaped for the browser. Dates become strings; nothing else
    survives JSON."""
    closes = item["closes_at"]
    return {
        "source": item["source"],
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
        # The other boards carrying this same job, if any. Kept rather than dropped
        # so their deadline and link stay reachable from the one row shown.
        "also_on": [{"source": copy["source"], "url": copy["url"]}
                    for copy in item.get("also_on", [])],
    }


class Credentials(BaseModel):
    username: str
    password: str


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not signed_in(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if signed_in(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(LOGIN_PAGE.read_text(encoding="utf-8"))


@app.post("/api/login")
def api_login(body: Credentials, request: Request):
    with psycopg.connect(search.DSN) as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM users"
            " WHERE username = %s AND active",
            (body.username,),
        ).fetchone()

        # One message for every failure -- unknown name, wrong password, disabled
        # account. Saying which would tell someone who is guessing that they had
        # half of it right.
        if row is None or not hmac.compare_digest(row[0], hashed(body.password, row[1])):
            return JSONResponse({"error": "Wrong username or password."},
                                status_code=401)

        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, username, expires_at, ip, user_agent)"
            " VALUES (%s, %s, %s, %s, %s)",
            (token, body.username, datetime.now(UTC) + timedelta(days=SESSION_DAYS),
             request.client.host if request.client else None,
             request.headers.get("user-agent", "")[:300]),
        )
        conn.execute("UPDATE users SET last_login = now() WHERE username = %s",
                     (body.username,))

    response = JSONResponse({"ok": True})
    # httponly so no script can read it, samesite=lax so another site cannot make
    # the browser use it. secure is left off because this serves over plain http on
    # localhost; it belongs here the moment there is a domain in front.
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        max_age=SESSION_DAYS * 86400, path="/")
    return response


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with psycopg.connect(search.DSN) as conn:
            conn.execute("DELETE FROM sessions WHERE token = %s", (token,))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/api/me")
def api_me(request: Request):
    user = signed_in(request)
    if not user:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    return JSONResponse(user)


@app.post("/api/search")
def api_search(body: Question, request: Request):
    """Retrieval only. No model, no API key, no cost -- this is the endpoint for
    watching the search work on its own."""
    if not signed_in(request):
        return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)

    started = time.perf_counter()
    results = search.retrieve(
        body.question, limit=body.show,
        open_only=body.open_only, rerank=body.rerank,
        hybrid=body.hybrid, position_type=body.position_type or None,
        dedupe=body.dedupe,
    )
    elapsed = time.perf_counter() - started

    return JSONResponse({
        "positions": [as_json(item) for item in results],
        "timings": {"retrieval_ms": round(elapsed * 1000)},
    })


@app.post("/api/chat")
def api_chat(body: Question, request: Request):
    if not signed_in(request):
        return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)

    started = time.perf_counter()
    results = search.retrieve(
        body.question, limit=body.show,
        open_only=body.open_only, rerank=body.rerank,
        hybrid=body.hybrid, position_type=body.position_type or None,
        dedupe=body.dedupe,
    )
    retrieval_ms = round((time.perf_counter() - started) * 1000)

    if not results:
        return JSONResponse({
            "answer": "Nothing in the database matches that.",
            "positions": [], "given": 0,
            "timings": {"retrieval_ms": retrieval_ms, "answer_ms": 0},
            "usage": None, "missing": [],
        })

    given = results[:body.context]

    started = time.perf_counter()
    try:
        text, usage = ask.answer(body.question, given, body.model)
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
def api_models(request: Request):
    if not signed_in(request):
        return JSONResponse({"error": "not signed in"}, status_code=401)
    return models_for_page()


def models_for_page():
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
def api_stats(request: Request):
    if not signed_in(request):
        return JSONResponse({"error": "not signed in"}, status_code=401)

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
    parser.add_argument("--add-user", metavar="NAME",
                        help="create a sign-in, or reset an existing password")
    parser.add_argument("--admin", action="store_true",
                        help="with --add-user, let them see the admin page")
    args = parser.parse_args()

    if args.add_user:
        add_user(args.add_user, args.admin)
        return

    with psycopg.connect(search.DSN) as conn:
        people = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    if not people:
        sys.exit("no accounts exist yet -- run:  python chat.py --add-user <name> --admin")

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
