"""First-party analytics: anonymous events in SQLite + a password-protected stats API.

Visitors get no cookies and no IP addresses are stored; each browser gets a random ID in localStorage.
The dashboard lives at /admin (login page + a signed session cookie for the admin only)
and is enabled by setting ADMIN_PASSWORD.
"""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from urllib.parse import parse_qs

from db import connect

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_COOKIE = "mt_admin"
SESSION_DAYS = 7
_login_attempts: dict = defaultdict(list)  # client ip -> recent failure timestamps

router = APIRouter()
basic = HTTPBasic(auto_error=False)
RETENTION_DAYS = 400

EVENT_TYPES = {"pageview", "title_open", "play", "watch", "search", "new_open", "outbound", "trailer"}
VISITOR_RE = re.compile(r"^[a-z0-9-]{8,40}$")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|facebookexternalhit|headlesschrome|lighthouse", re.I)
_rate: dict = defaultdict(lambda: [0, 0.0])  # visitor -> [count, window_start]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    day       TEXT NOT NULL,          -- UTC date, YYYY-MM-DD
    visitor   TEXT NOT NULL,
    type      TEXT NOT NULL,
    entity_id INTEGER,                -- free-library title
    tmdb_id   INTEGER,                -- New & Popular title
    label     TEXT,                   -- page / search term / provider / title
    label2    TEXT,
    num       REAL,                   -- seconds watched / result count / part
    num2      REAL,                   -- position in the film, percent
    device    TEXT,
    referrer  TEXT,
    country   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day_type ON events (day, type);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


def init():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def device_of(ua: str) -> str:
    if re.search(r"ipad|tablet|(android(?!.*mobile))", ua, re.I):
        return "Tablet"
    if re.search(r"mobi|iphone|android", ua, re.I):
        return "Mobile"
    return "Desktop"


def clip(v, n=120):
    return str(v)[:n].strip() if v not in (None, "") else None


def num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e7 else None
    except (TypeError, ValueError):
        return None


def integer(v):
    n = num(v)
    return int(n) if n is not None else None


@router.post("/api/events", status_code=204)
async def collect(request: Request):
    ua = request.headers.get("user-agent", "")
    if BOT_RE.search(ua) or request.headers.get("dnt") == "1" and os.environ.get("RESPECT_DNT") == "1":
        return Response(status_code=204)
    try:
        body = json.loads((await request.body())[:64_000])
        visitor = str(body.get("v", ""))
        events = body.get("events") or []
    except (ValueError, AttributeError):
        raise HTTPException(400)
    if not VISITOR_RE.match(visitor) or not isinstance(events, list):
        raise HTTPException(400)

    now = time.time()
    bucket = _rate[visitor]
    if now - bucket[1] > 60:
        bucket[:] = [0, now]
    bucket[0] += len(events)
    if bucket[0] > 300:  # per visitor per minute
        return Response(status_code=204)
    if len(_rate) > 50_000:
        _rate.clear()

    country = clip(request.headers.get("cf-ipcountry") or request.headers.get("x-vercel-ip-country"), 2)
    device = device_of(ua)
    day = dt.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
    rows = []
    for e in events[:50]:
        if not isinstance(e, dict) or e.get("t") not in EVENT_TYPES:
            continue
        label = clip(e.get("l"))
        if e["t"] == "search" and label:
            label = label.lower()
        rows.append((int(now), day, visitor, e["t"], integer(e.get("e")), integer(e.get("m")), label,
                     clip(e.get("l2")), num(e.get("n")), num(e.get("n2")), device, clip(e.get("r"), 80), country))
    if rows:
        conn = connect()
        try:
            conn.executemany(
                """INSERT INTO events (ts, day, visitor, type, entity_id, tmdb_id, label, label2, num, num2,
                                       device, referrer, country) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows)
            conn.commit()
        finally:
            conn.close()
    return Response(status_code=204)


# ---------------------------------------------------------------- admin

def _password():
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise HTTPException(404, "Stats are disabled. Set ADMIN_PASSWORD to enable /admin.")
    return password


def _sign(value: str, password: str) -> str:
    # Keyed by the password, so changing ADMIN_PASSWORD logs everyone out.
    key = hashlib.sha256(("movietube-admin:" + password).encode()).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _session_valid(token: str, password: str) -> bool:
    try:
        expires, sig = token.split(".", 1)
        return int(expires) > time.time() and hmac.compare_digest(sig, _sign(expires, password))
    except (ValueError, AttributeError):
        return False


def is_admin(request: Request, creds) -> bool:
    password = _password()
    if _session_valid(request.cookies.get(SESSION_COOKIE, ""), password):
        return True
    # Basic auth still works for scripts, e.g. curl -u admin:PASSWORD .../api/admin/stats
    return bool(creds) and secrets.compare_digest(creds.password.encode(), password.encode())


def require_admin(request: Request, creds: HTTPBasicCredentials = Depends(basic)):
    if not is_admin(request, creds):
        raise HTTPException(401, "Login required")
    return True


@router.get("/admin")
def admin_page(request: Request, creds: HTTPBasicCredentials = Depends(basic)):
    page = "admin.html" if is_admin(request, creds) else "admin-login.html"
    return FileResponse(os.path.join(STATIC, page), headers={"Cache-Control": "no-store"})


@router.post("/admin/login")
async def admin_login(request: Request):
    password = _password()
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    recent = [t for t in _login_attempts[ip] if now - t < 600]
    _login_attempts[ip] = recent
    if len(recent) >= 10:  # 10 wrong tries per 10 minutes
        return RedirectResponse("/admin?e=locked", status_code=303)

    form = parse_qs((await request.body())[:2000].decode("utf-8", "ignore"))
    given = (form.get("password") or [""])[0]
    if not secrets.compare_digest(given.encode(), password.encode()):
        recent.append(now)
        await asyncio.sleep(0.5)
        return RedirectResponse("/admin?e=wrong", status_code=303)

    _login_attempts.pop(ip, None)
    expires = str(int(now) + SESSION_DAYS * 86400)
    resp = RedirectResponse("/admin", status_code=303)
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(SESSION_COOKIE, f"{expires}.{_sign(expires, password)}", max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="strict", secure=https, path="/")
    return resp


@router.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def period_kpis(conn, start, end):
    r = conn.execute(
        """SELECT COUNT(DISTINCT visitor) AS visitors,
                  SUM(type = 'pageview') AS pageviews,
                  SUM(type = 'play') AS plays,
                  COALESCE(SUM(CASE WHEN type = 'watch' THEN num END), 0) / 3600.0 AS watch_hours,
                  COUNT(DISTINCT CASE WHEN type = 'play' THEN visitor END) AS viewers
           FROM events WHERE day BETWEEN ? AND ?""", (start, end)).fetchone()
    return {k: (r[k] or 0) for k in r.keys()}


@router.get("/api/admin/stats")
def stats(days: int = 30, _=Depends(require_admin)):
    days = max(1, min(days, RETENTION_DAYS))
    today = dt.datetime.utcnow().date()
    start = today - dt.timedelta(days=days - 1)
    prev_start, prev_end = start - dt.timedelta(days=days), start - dt.timedelta(days=1)
    s, e = start.isoformat(), today.isoformat()
    conn = connect()
    try:
        kpis = period_kpis(conn, s, e)
        prev = period_kpis(conn, prev_start.isoformat(), prev_end.isoformat())

        per_day = {r["day"]: r for r in rows(conn, """
            SELECT day, COUNT(DISTINCT visitor) AS visitors, SUM(type = 'play') AS plays,
                   COALESCE(SUM(CASE WHEN type = 'watch' THEN num END), 0) / 3600.0 AS watch_hours
            FROM events WHERE day BETWEEN ? AND ? GROUP BY day""", (s, e))}
        daily = []
        for i in range(days):
            d = (start + dt.timedelta(days=i)).isoformat()
            r = per_day.get(d, {})
            daily.append({"day": d, "visitors": r.get("visitors", 0), "plays": r.get("plays") or 0,
                          "watch_hours": round(r.get("watch_hours") or 0, 2)})

        # Per-film: plays, unique viewers, watch time, and how far viewers get on average.
        films = rows(conn, """
            WITH p AS (SELECT entity_id, COUNT(*) AS plays, COUNT(DISTINCT visitor) AS viewers
                       FROM events WHERE type = 'play' AND day BETWEEN :s AND :e GROUP BY entity_id),
                 w AS (SELECT entity_id, SUM(num) / 3600.0 AS watch_hours FROM events
                       WHERE type = 'watch' AND day BETWEEN :s AND :e GROUP BY entity_id),
                 c AS (SELECT entity_id, AVG(mx) AS completion FROM
                         (SELECT entity_id, visitor, MAX(num2) AS mx FROM events
                          WHERE type = 'watch' AND day BETWEEN :s AND :e GROUP BY entity_id, visitor)
                       GROUP BY entity_id)
            SELECT p.entity_id AS id, en.name, en.year, p.plays, p.viewers,
                   ROUND(COALESCE(w.watch_hours, 0), 2) AS watch_hours, ROUND(c.completion, 1) AS completion
            FROM p JOIN entities en ON en.id = p.entity_id
            LEFT JOIN w ON w.entity_id = p.entity_id LEFT JOIN c ON c.entity_id = p.entity_id
            ORDER BY p.plays DESC, p.viewers DESC""", {"s": s, "e": e})
        top_films = films[:15]
        abandoned = sorted([f for f in films if f["viewers"] >= 3 and f["completion"] is not None and f["completion"] < 25],
                           key=lambda f: f["completion"])[:10]

        top = lambda sql: rows(conn, sql, (s, e))  # noqa: E731
        return {
            "range": {"start": s, "end": e, "days": days},
            "kpis": kpis,
            "prev": prev,
            "live": conn.execute("SELECT COUNT(DISTINCT visitor) FROM events WHERE ts >= ?", (int(time.time()) - 300,)).fetchone()[0],
            "daily": daily,
            "top_films": top_films,
            "abandoned": abandoned,
            "searches": top("""SELECT label, COUNT(*) AS n, MAX(num) AS results FROM events
                               WHERE type = 'search' AND label IS NOT NULL AND day BETWEEN ? AND ?
                               GROUP BY label ORDER BY n DESC LIMIT 12"""),
            "no_results": top("""SELECT label, COUNT(*) AS n FROM events
                                 WHERE type = 'search' AND num = 0 AND label IS NOT NULL AND day BETWEEN ? AND ?
                                 GROUP BY label ORDER BY n DESC LIMIT 12"""),
            "providers": top("""SELECT label, COUNT(*) AS n FROM events
                                WHERE type = 'outbound' AND label IS NOT NULL AND day BETWEEN ? AND ?
                                GROUP BY label ORDER BY n DESC LIMIT 10"""),
            "new_titles": top("""SELECT label, tmdb_id, COUNT(*) AS n,
                                        (SELECT COUNT(*) FROM events o WHERE o.type = 'outbound' AND o.tmdb_id = ev.tmdb_id
                                           AND o.day BETWEEN ?1 AND ?2) AS clicks
                                 FROM events ev WHERE type = 'new_open' AND day BETWEEN ?1 AND ?2
                                 GROUP BY tmdb_id ORDER BY n DESC LIMIT 10"""),
            "pages": top("""SELECT label, COUNT(*) AS n FROM events
                            WHERE type = 'pageview' AND day BETWEEN ? AND ? GROUP BY label ORDER BY n DESC LIMIT 8"""),
            "devices": top("""SELECT device AS label, COUNT(DISTINCT visitor) AS n FROM events
                              WHERE day BETWEEN ? AND ? GROUP BY device ORDER BY n DESC"""),
            "referrers": top("""SELECT COALESCE(referrer, 'Direct / unknown') AS label, COUNT(DISTINCT visitor) AS n FROM events
                                WHERE type = 'pageview' AND num = 1 AND day BETWEEN ? AND ? GROUP BY 1 ORDER BY n DESC LIMIT 8"""),
            "countries": top("""SELECT country AS label, COUNT(DISTINCT visitor) AS n FROM events
                                WHERE country IS NOT NULL AND day BETWEEN ? AND ? GROUP BY country ORDER BY n DESC LIMIT 8"""),
        }
    finally:
        conn.close()
