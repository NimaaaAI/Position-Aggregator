"""A local web interface over the same pipeline the command line uses.

    python chat.py            then open http://127.0.0.1:8001

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
import pycountry
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import ask
import search

ROOT = Path(__file__).parent
PAGE = ROOT / "templates" / "chat.html"
LOGIN_PAGE = ROOT / "templates" / "login.html"
REGISTER_PAGE = ROOT / "templates" / "register.html"
ADMIN_PAGE = ROOT / "templates" / "admin.html"

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


def asked_today(conn, username):
    """How many written answers this person has already had today."""
    return conn.execute(
        "SELECT count(*) FROM activity"
        " WHERE username = %s AND endpoint = 'ask' AND at >= date_trunc('day', now())",
        (username,),
    ).fetchone()[0]


def log(username, endpoint, question, results, ms, model=None, usage=None):
    """Record one question. Written after the work, so a request that failed
    halfway does not count against anyone's daily allowance."""
    with psycopg.connect(search.DSN) as conn:
        conn.execute(
            "INSERT INTO activity (username, endpoint, question, results, model,"
            "                      prompt_tokens, completion_tokens, ms)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (username, endpoint, question[:500], results, model,
             usage.prompt_tokens if usage else None,
             usage.completion_tokens if usage else None, ms),
        )


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
            "INSERT INTO users (username, password_hash, salt, is_admin, active)"
            " VALUES (%s, %s, %s, %s, true)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET password_hash = EXCLUDED.password_hash,"
            "       salt = EXCLUDED.salt, is_admin = EXCLUDED.is_admin,"
            "       active = true",
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
    # An ISO country code, or None for anywhere. Also applied before ranking.
    country: str | None = None


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


class Registration(BaseModel):
    username: str
    password: str
    email: str


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return HTMLResponse(REGISTER_PAGE.read_text(encoding="utf-8"))


@app.post("/api/register")
def api_register(body: Registration):
    """Anyone may apply. Nobody may use the result until an admin approves it, so
    this creates a queue entry rather than an account."""
    name = body.username.strip().lower()
    email = body.email.strip()

    if not (3 <= len(name) <= 32) or not name.replace("_", "").replace("-", "").isalnum():
        return JSONResponse(
            {"error": "Username: 3-32 characters, letters, digits, - and _ only."},
            status_code=400)
    if len(body.password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters."},
                            status_code=400)
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"error": "That email address does not look right."},
                            status_code=400)

    salt = secrets.token_bytes(16)
    with psycopg.connect(search.DSN) as conn:
        taken = conn.execute("SELECT 1 FROM users WHERE username = %s", (name,)).fetchone()
        if taken:
            return JSONResponse({"error": "That username is already taken."},
                                status_code=409)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, email, active)"
            " VALUES (%s, %s, %s, %s, false)",
            (name, hashed(body.password, salt), salt, email),
        )
    return JSONResponse({"ok": True})


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
    with psycopg.connect(search.DSN) as conn:
        limit, = conn.execute("SELECT daily_ask_limit FROM users WHERE username = %s",
                              (user["username"],)).fetchone()
        used = asked_today(conn, user["username"])
    return JSONResponse({**user, "asked_today": used, "daily_ask_limit": limit})


# ---- the admin page -------------------------------------------------------


class AdminAction(BaseModel):
    username: str
    action: str          # approve | disable | remove | signout | limit
    value: int | None = None


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = signed_in(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user["is_admin"]:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(ADMIN_PAGE.read_text(encoding="utf-8"))


@app.get("/api/admin/overview")
def api_admin_overview(request: Request):
    user = signed_in(request)
    if not user or not user["is_admin"]:
        return JSONResponse({"error": "not an administrator"}, status_code=403)

    with psycopg.connect(search.DSN) as conn:
        people = conn.execute(
            "SELECT u.username, u.email, u.is_admin, u.active, u.created_at,"
            "       u.last_login, u.daily_ask_limit,"
            "       (SELECT count(*) FROM activity a"
            "         WHERE a.username = u.username AND a.endpoint = 'ask') AS asks,"
            "       (SELECT count(*) FROM activity a"
            "         WHERE a.username = u.username AND a.endpoint = 'ask'"
            "           AND a.at >= date_trunc('day', now())) AS asks_today,"
            "       (SELECT coalesce(sum(prompt_tokens + completion_tokens), 0)"
            "          FROM activity a WHERE a.username = u.username) AS tokens"
            "  FROM users u ORDER BY u.active, u.created_at DESC"
        ).fetchall()

        live = conn.execute(
            "SELECT username, created_at, last_seen, ip, left(user_agent, 60)"
            "  FROM sessions WHERE expires_at > now() ORDER BY last_seen DESC"
        ).fetchall()

        recent = conn.execute(
            "SELECT username, at, endpoint, question, results,"
            "       coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0), ms"
            "  FROM activity ORDER BY at DESC LIMIT 60"
        ).fetchall()

    def when(value):
        return value.isoformat() if value else None

    return JSONResponse({
        "users": [{
            "username": r[0], "email": r[1], "is_admin": r[2], "active": r[3],
            "created_at": when(r[4]), "last_login": when(r[5]), "limit": r[6],
            "asks": r[7], "asks_today": r[8], "tokens": r[9],
        } for r in people],
        "sessions": [{
            "username": r[0], "created_at": when(r[1]), "last_seen": when(r[2]),
            "ip": r[3], "agent": r[4],
        } for r in live],
        "activity": [{
            "username": r[0], "at": when(r[1]), "endpoint": r[2], "question": r[3],
            "results": r[4], "tokens": r[5], "ms": r[6],
        } for r in recent],
    })


@app.post("/api/admin/user")
def api_admin_user(body: AdminAction, request: Request):
    user = signed_in(request)
    if not user or not user["is_admin"]:
        return JSONResponse({"error": "not an administrator"}, status_code=403)

    # An admin locking themselves out is a support call to nobody, since there is
    # no support. Everything else is fair game.
    if body.username == user["username"] and body.action in ("disable", "remove"):
        return JSONResponse({"error": "You cannot disable or remove yourself."},
                            status_code=400)

    with psycopg.connect(search.DSN) as conn:
        if body.action == "approve":
            conn.execute("UPDATE users SET active = true WHERE username = %s",
                         (body.username,))
        elif body.action == "disable":
            # Sessions go too, or a disabled account keeps working until its
            # cookie happens to expire.
            conn.execute("UPDATE users SET active = false WHERE username = %s",
                         (body.username,))
            conn.execute("DELETE FROM sessions WHERE username = %s", (body.username,))
        elif body.action == "remove":
            conn.execute("DELETE FROM users WHERE username = %s", (body.username,))
        elif body.action == "signout":
            conn.execute("DELETE FROM sessions WHERE username = %s", (body.username,))
        elif body.action == "limit":
            conn.execute("UPDATE users SET daily_ask_limit = %s WHERE username = %s",
                         (max(0, int(body.value or 0)), body.username))
        else:
            return JSONResponse({"error": "unknown action"}, status_code=400)

    return JSONResponse({"ok": True})


@app.post("/api/search")
def api_search(body: Question, request: Request):
    """Retrieval only. No model, no API key, no cost -- this is the endpoint for
    watching the search work on its own."""
    user = signed_in(request)
    if not user:
        return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)

    started = time.perf_counter()
    results = search.retrieve(
        body.question, limit=body.show,
        open_only=body.open_only, rerank=body.rerank,
        hybrid=body.hybrid, position_type=body.position_type or None,
        dedupe=body.dedupe, country=body.country or None,
    )
    elapsed = round((time.perf_counter() - started) * 1000)
    log(user["username"], "search", body.question, len(results), elapsed)

    return JSONResponse({
        "positions": [as_json(item) for item in results],
        "timings": {"retrieval_ms": elapsed},
    })


@app.post("/api/chat")
def api_chat(body: Question, request: Request):
    user = signed_in(request)
    if not user:
        return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)

    # Only the written answer spends the owner's API credit, so only this endpoint
    # is capped. Searching is free and stays unlimited.
    with psycopg.connect(search.DSN) as conn:
        limit, = conn.execute("SELECT daily_ask_limit FROM users WHERE username = %s",
                              (user["username"],)).fetchone()
        used = asked_today(conn, user["username"])
    if used >= limit:
        return JSONResponse(
            {"error": f"You have used your {limit} answers for today. "
                      f"Searching still works and costs nothing."},
            status_code=429)

    started = time.perf_counter()
    results = search.retrieve(
        body.question, limit=body.show,
        open_only=body.open_only, rerank=body.rerank,
        hybrid=body.hybrid, position_type=body.position_type or None,
        dedupe=body.dedupe, country=body.country or None,
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

    log(user["username"], "ask", body.question, len(results),
        retrieval_ms + answer_ms, model=body.model, usage=usage)

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


class Browse(BaseModel):
    limit: int = 100
    offset: int = 0
    open_only: bool = True
    dedupe: bool = True
    position_type: str | None = None
    country: str | None = None


@app.post("/api/browse")
def api_browse(body: Browse, request: Request):
    """The filters with no question. Straight from the table, no models, no cost."""
    user = signed_in(request)
    if not user:
        return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)

    started = time.perf_counter()
    results, total = search.browse(
        limit=min(body.limit, 500), offset=max(0, body.offset),
        open_only=body.open_only, position_type=body.position_type or None,
        country=body.country or None, dedupe=body.dedupe,
    )
    elapsed = round((time.perf_counter() - started) * 1000)
    log(user["username"], "browse", body.country or "any", total, elapsed)

    return JSONResponse({
        "positions": [as_json(item) for item in results],
        "total": total,
        "offset": body.offset,
        "timings": {"retrieval_ms": elapsed},
    })


@app.get("/api/countries")
def api_countries(request: Request):
    """The countries worth offering in the filter, commonest first.

    Counted on open positions only: a country whose every advert has closed is a
    menu entry that can only ever return nothing.
    """
    if not signed_in(request):
        return JSONResponse({"error": "not signed in"}, status_code=401)

    with psycopg.connect(search.DSN) as conn:
        rows = conn.execute(
            "SELECT country_code, count(*) FROM positions"
            " WHERE country_code IS NOT NULL"
            "   AND (closes_at IS NULL OR closes_at > now())"
            " GROUP BY country_code ORDER BY 2 DESC, 1"
        ).fetchall()

    def name(code):
        found = pycountry.countries.get(alpha_2=code)
        return getattr(found, "common_name", None) or (found.name if found else code)

    return JSONResponse({"countries": [
        {"code": code, "name": name(code), "count": count} for code, count in rows
    ]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
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
