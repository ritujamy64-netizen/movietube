"""MovieTube web server: JSON API + static frontend.

    uvicorn app:app --host 0.0.0.0 --port 8000

Env:
    DB_PATH                 SQLite file (default ./data/library.db)
    HARVEST_INTERVAL_HOURS  run the harvester in the background every N hours (0 = never; default 168 = weekly)
    HARVEST_LIMIT           cap items per harvest (handy on tiny hosts)
    TMDB_API_KEY            optional, enables TMDB artwork and the New & Popular (where-to-watch) section
    WATCH_REGION            default country for where-to-watch links (default US)
    ADMIN_PASSWORD          enables the private stats dashboard at /admin
"""
import logging
import os
import random
import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import requests

import analytics
import discover
import harvester
from db import connect

log = logging.getLogger("movietube")
STATIC = os.path.join(os.path.dirname(__file__), "static")
CARD_FIELDS = "e.id, e.name, e.year, e.runtime_min, e.thumbnail, e.poster, e.backdrop, e.rating"
harvest_state = {"running": False, "last_run": None, "last_error": None}


def harvest_loop(interval_hours: float, limit):
    while True:
        harvest_state["running"] = True
        try:
            harvester.run(limit=limit)
            harvest_state["last_error"] = None
        except Exception as e:  # noqa: BLE001
            log.exception("harvest failed")
            harvest_state["last_error"] = str(e)
        harvest_state["running"] = False
        harvest_state["last_run"] = time.time()
        time.sleep(interval_hours * 3600)


@asynccontextmanager
async def lifespan(_app):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    connect().close()  # create schema
    analytics.init()
    interval = float(os.environ.get("HARVEST_INTERVAL_HOURS", "168"))
    limit = int(os.environ["HARVEST_LIMIT"]) if os.environ.get("HARVEST_LIMIT") else None
    if interval > 0:
        threading.Thread(target=harvest_loop, args=(interval, limit), daemon=True).start()
    yield


app = FastAPI(title="MovieTube", lifespan=lifespan)
app.include_router(discover.router)
app.include_router(analytics.router)


def q(sql, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@app.get("/api/status")
def status():
    stats = q("SELECT (SELECT COUNT(*) FROM entities) AS titles, (SELECT COUNT(*) FROM videos) AS videos")[0]
    return {**stats, "harvest": harvest_state, "discover": discover.enabled()}


@app.get("/api/home")
def home():
    top = q(f"SELECT {CARD_FIELDS}, e.description FROM entities e ORDER BY e.popularity DESC LIMIT 40")
    with_art = [t for t in top if t["backdrop"]] or top
    hero = random.choice(with_art) if with_art else None

    rows = [
        {"title": "Trending Now", "items": q(f"SELECT {CARD_FIELDS} FROM entities e ORDER BY e.popularity DESC LIMIT 30")},
        {"title": "Recently Added", "items": q(f"SELECT {CARD_FIELDS} FROM entities e ORDER BY e.added_at DESC, e.id DESC LIMIT 30")},
    ]
    rated = q(f"SELECT {CARD_FIELDS} FROM entities e WHERE e.rating >= 6.5 ORDER BY e.rating DESC, e.popularity DESC LIMIT 30")
    if len(rated) >= 8:
        rows.append({"title": "Critically Acclaimed", "items": rated})
    cats = q("""SELECT c.id, c.name, COUNT(*) AS n FROM categories c
                JOIN entity_categories ec ON ec.category_id = c.id
                GROUP BY c.id HAVING n >= 8 ORDER BY n DESC""")
    for c in cats:
        items = q(f"""SELECT {CARD_FIELDS} FROM entities e JOIN entity_categories ec ON ec.entity_id = e.id
                      WHERE ec.category_id = ? ORDER BY e.popularity DESC LIMIT 30""", (c["id"],))
        rows.append({"title": c["name"], "category_id": c["id"], "items": items})
    return {"hero": hero, "rows": [r for r in rows if r["items"]]}


@app.get("/api/categories")
def categories():
    return q("""SELECT c.id, c.name, COUNT(*) AS n FROM categories c
                JOIN entity_categories ec ON ec.category_id = c.id GROUP BY c.id ORDER BY c.name""")


@app.get("/api/category/{cat_id}")
def category(cat_id: int, page: int = Query(1, ge=1), sort: str = "popular"):
    cat = q("SELECT id, name FROM categories WHERE id = ?", (cat_id,))
    if not cat:
        raise HTTPException(404)
    order = {"popular": "e.popularity DESC", "year": "e.year DESC", "az": "e.name COLLATE NOCASE",
             "rating": "e.rating DESC"}.get(sort, "e.popularity DESC")
    items = q(f"""SELECT {CARD_FIELDS} FROM entities e JOIN entity_categories ec ON ec.entity_id = e.id
                  WHERE ec.category_id = ? ORDER BY {order} LIMIT 60 OFFSET ?""", (cat_id, (page - 1) * 60))
    return {**cat[0], "page": page, "items": items}


@app.get("/api/search")
def search(q_: str = Query("", alias="q", max_length=100)):
    terms = re.findall(r"\w+", q_)
    if not terms:
        return {"items": []}
    fts = " ".join(f'"{t}"*' for t in terms)
    items = q(f"""SELECT {CARD_FIELDS} FROM entities_fts f JOIN entities e ON e.id = f.rowid
                  WHERE entities_fts MATCH ? ORDER BY bm25(entities_fts, 10.0, 1.0) - e.popularity / 1e7
                  LIMIT 60""", (fts,))
    return {"items": items}


@app.get("/api/title/{entity_id}")
def title(entity_id: int):
    rows = q("SELECT * FROM entities WHERE id = ?", (entity_id,))
    if not rows:
        raise HTTPException(404)
    e = rows[0]
    e["source_url"] = f"https://archive.org/details/{e['source_id']}" if e["source"] == "ia" else None
    e["videos"] = q("SELECT id, name, season, episode, url, duration_sec FROM videos WHERE entity_id = ? ORDER BY season, episode, id", (entity_id,))
    e["genres"] = q("""SELECT c.id, c.name FROM categories c JOIN entity_categories ec ON ec.category_id = c.id
                       WHERE ec.entity_id = ? ORDER BY c.name""", (entity_id,))
    e["similar"] = q(f"""SELECT {CARD_FIELDS} FROM entities e JOIN entity_categories ec ON ec.entity_id = e.id
                         WHERE ec.category_id IN (SELECT category_id FROM entity_categories WHERE entity_id = ?)
                           AND e.id != ? GROUP BY e.id ORDER BY COUNT(*) DESC, e.popularity DESC LIMIT 12""",
                      (entity_id, entity_id))
    return e


# archive.org's generic /download/ link sometimes redirects to a slow backup copy
# (e.g. *.ca.archive.org), where playback stalls. Send players to the item's primary server instead.
DOWNLOAD_RE = re.compile(r"^https://archive\.org/download/([^/]+)/(.+)$")
_servers: dict = {}  # identifier -> (fetched_at, server, dir)
_ia = requests.Session()


def primary_location(identifier: str):
    hit = _servers.get(identifier)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1], hit[2]
    d = _ia.get(f"https://archive.org/metadata/{identifier}", timeout=10).json()
    server = (d.get("workable_servers") or [d.get("server")])[0]
    if not server or not d.get("dir"):
        raise ValueError("no server")
    if len(_servers) > 5000:
        _servers.clear()
    _servers[identifier] = (time.time(), server, d["dir"])
    return server, d["dir"]


@app.get("/api/stream/{video_id}")
def stream(video_id: int):
    rows = q("SELECT url FROM videos WHERE id = ?", (video_id,))
    if not rows:
        raise HTTPException(404)
    url = rows[0]["url"]
    m = DOWNLOAD_RE.match(url)
    if m:
        try:
            server, path = primary_location(m.group(1))
            url = f"https://{server}{path}/{m.group(2)}"
        except Exception:  # noqa: BLE001 - fall back to archive.org's own redirect
            log.warning("could not resolve primary server for %s", m.group(1))
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, max-age=3600"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))

