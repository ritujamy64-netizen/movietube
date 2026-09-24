"""New & Popular: newer movies from TMDB with where-to-watch links (streaming data by JustWatch).

We never host or embed these films; we only link out to the services that carry them.
Enabled when TMDB_API_KEY is set.
"""
import datetime as dt
import os
import threading
import time
from urllib.parse import quote_plus

import requests
from fastapi import APIRouter, HTTPException, Query

from db import connect

router = APIRouter(prefix="/api/discover")
TMDB_API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"
DEFAULT_REGION = os.environ.get("WATCH_REGION", "US")
CACHE_TTL = 6 * 3600

_http = requests.Session()
_cache: dict = {}
_lock = threading.Lock()

# Provider name -> search URL on that service. Anything not listed links to TMDB's watch page,
# which has JustWatch deep links for every provider.
PROVIDER_SEARCH = {
    "netflix": "https://www.netflix.com/search?q={q}",
    "amazon prime video": "https://www.amazon.com/s?k={q}&i=instant-video",
    "amazon video": "https://www.amazon.com/s?k={q}&i=instant-video",
    "hulu": "https://www.hulu.com/search?q={q}",
    "apple tv": "https://tv.apple.com/search?term={q}",
    "apple tv plus": "https://tv.apple.com/search?term={q}",
    "google play movies": "https://play.google.com/store/search?q={q}&c=movies",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "tubi tv": "https://tubitv.com/search/{q}",
}
KINDS = [("flatrate", "Stream"), ("free", "Free"), ("ads", "Free with ads"), ("rent", "Rent"), ("buy", "Buy")]


def enabled() -> bool:
    return bool(os.environ.get("TMDB_API_KEY"))


def tmdb_get(path: str, **params):
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        raise HTTPException(503, "TMDB_API_KEY is not set")
    cache_key = (path, tuple(sorted(params.items())))
    with _lock:
        hit = _cache.get(cache_key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    headers = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    r = _http.get(f"{TMDB_API}{path}", params=params, headers=headers, timeout=20)
    if r.status_code == 404:
        raise HTTPException(404)
    r.raise_for_status()
    data = r.json()
    with _lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[cache_key] = (time.time(), data)
    return data


def card(m):
    date = m.get("release_date") or ""
    return {
        "id": m["id"],
        "name": m.get("title") or m.get("name"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "backdrop": IMG + "w780" + m["backdrop_path"] if m.get("backdrop_path") else None,
        "poster": IMG + "w342" + m["poster_path"] if m.get("poster_path") else None,
        "rating": m.get("vote_average") or None,
        "href": f"#/new/{m['id']}",
    }


def cards(data, n=20):
    return [card(m) for m in data.get("results", []) if m.get("backdrop_path") or m.get("poster_path")][:n]


def region_code(region: str) -> str:
    region = (region or DEFAULT_REGION).upper()
    return region if len(region) == 2 and region.isalpha() else DEFAULT_REGION


@router.get("")
def discover_home(region: str = Query(DEFAULT_REGION)):
    region = region_code(region)
    today = dt.date.today()
    streaming = {"watch_region": region, "with_watch_monetization_types": "flatrate", "include_adult": "false"}
    rows = [
        ("Trending This Week", "/trending/movie/week", {}),
        ("New on Streaming", "/discover/movie", {**streaming, "sort_by": "popularity.desc",
                                                 "primary_release_date.gte": str(today - dt.timedelta(days=180)),
                                                 "vote_count.gte": 20}),
        ("Popular on Streaming", "/discover/movie", {**streaming, "sort_by": "popularity.desc", "vote_count.gte": 200}),
        ("Free to Stream", "/discover/movie", {"watch_region": region, "with_watch_monetization_types": "free|ads",
                                               "sort_by": "popularity.desc", "vote_count.gte": 100, "include_adult": "false"}),
        ("Top Rated on Streaming", "/discover/movie", {**streaming, "sort_by": "vote_average.desc", "vote_count.gte": 3000}),
        ("In Theaters Now", "/movie/now_playing", {"region": region}),
    ]
    out = []
    for title, path, params in rows:
        items = cards(tmdb_get(path, **params))
        if items:
            out.append({"title": title, "items": items})
    hero = next((i for r in out for i in r["items"] if i["backdrop"]), None)
    if hero:
        hero = {**hero, "description": movie(hero["id"], region)["overview"]}
    return {"region": region, "hero": hero, "rows": out}


@router.get("/search")
def discover_search(q: str = Query("", max_length=100)):
    if not q.strip():
        return {"items": []}
    return {"items": cards(tmdb_get("/search/movie", query=q.strip(), include_adult="false"), 18)}


@router.get("/regions")
def regions():
    data = tmdb_get("/watch/providers/regions")
    return sorted(({"code": r["iso_3166_1"], "name": r["english_name"]} for r in data.get("results", [])),
                  key=lambda r: r["name"])


@router.get("/movie/{tmdb_id}")
def movie(tmdb_id: int, region: str = Query(DEFAULT_REGION)):
    region = region_code(region)
    m = tmdb_get(f"/movie/{tmdb_id}", append_to_response="watch/providers,videos")
    title = m.get("title", "")
    wp = (m.get("watch/providers") or {}).get("results", {}).get(region, {})
    watch_page = wp.get("link") or f"https://www.themoviedb.org/movie/{tmdb_id}/watch?locale={region}"

    where = []
    for kind, label in KINDS:
        providers = []
        for p in sorted(wp.get(kind, []), key=lambda p: p.get("display_priority", 99)):
            name = p["provider_name"].lower()
            tpl = next((t for k, t in PROVIDER_SEARCH.items() if name == k or name.startswith(k + " ")), None)
            providers.append({
                "name": p["provider_name"],
                "logo": IMG + "w92" + p["logo_path"] if p.get("logo_path") else None,
                "url": tpl.format(q=quote_plus(title)) if tpl else watch_page,
            })
        if providers:
            where.append({"kind": kind, "label": label, "providers": providers})

    videos = [v for v in (m.get("videos") or {}).get("results", []) if v.get("site") == "YouTube"]
    videos.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official")))

    conn = connect()
    try:
        local = conn.execute("SELECT id FROM entities WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
    finally:
        conn.close()

    similar = tmdb_get(f"/movie/{tmdb_id}/recommendations")
    return {
        **card(m),
        "overview": m.get("overview"),
        "runtime_min": m.get("runtime"),
        "genres": [g["name"] for g in m.get("genres", [])],
        "tagline": m.get("tagline"),
        "region": region,
        "where": where,
        "watch_page": watch_page,
        "trailer": videos[0]["key"] if videos else None,
        "local_id": local["id"] if local else None,
        "similar": cards(similar, 12),
    }
