"""The Harvester: fills the library from the Internet Archive's public-domain film collection.

Resumable: every item is committed as soon as it is processed and recorded in
harvest_log, so a crash or Ctrl+C loses nothing and a re-run only fetches new items.

    python harvester.py                  # harvest everything (~28k films)
    python harvester.py --limit 300      # quick test: the 300 most-watched films
    python harvester.py --licensed-only  # only items with an explicit PD / CC license
    TMDB_API_KEY=... python harvester.py --tmdb-only   # just (re)run TMDB enrichment
"""
import argparse
import html
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import category_id, connect

log = logging.getLogger("harvester")

SOURCE = "ia"
SCRAPE_URL = "https://archive.org/services/search/v1/scrape"
META_URL = "https://archive.org/metadata/{}"
DOWNLOAD_URL = "https://archive.org/download/{}/{}"
THUMB_URL = "https://archive.org/services/img/{}"
DEFAULT_QUERY = "collection:feature_films AND mediatype:movies"

# Canonical genre -> pattern matched against IA subjects and collection names.
GENRE_RULES = [
    ("Comedy", r"comed|slapstick|chaplin|keaton|laurel|stooges|lloyd"),
    ("Film Noir", r"noir"),
    ("Horror", r"horror|zombie|vampire|monster|ghoul"),
    ("Sci-Fi", r"sci-?fi|science.?fiction"),
    ("Western", r"western|cowboy"),
    ("Mystery & Crime", r"myster|crime|detective|gangster|thriller|suspense"),
    ("Drama", r"drama"),
    ("Romance", r"romanc"),
    ("Action & Adventure", r"action|adventure|swashbuckl|jungle"),
    ("War", r"\bwar\b|wwii|world war"),
    ("Animation", r"animat|cartoon"),
    ("Musicals", r"musical"),
    ("Documentary", r"documentar"),
    ("Silent Films", r"silent"),
    ("Cult & Exploitation", r"exploitation|cult|b-movie|grindhouse"),
]
GENRE_RES = [(name, re.compile(pat, re.I)) for name, pat in GENRE_RULES]
IMDB_RE = re.compile(r"\btt\d{7,8}\b")
TAG_RE = re.compile(r"<[^>]+>")

# Preference order for which MP4 derivative to stream.
FORMAT_RANK = {"h.264": 0, "h.264 hd": 1, "mpeg4": 2, "512kb mpeg4": 3, "h.264 ia": 4}


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.headers["User-Agent"] = "MovieTube-Harvester/1.0 (self-hosted public-domain film library)"
    return s


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def first(v):
    v = as_list(v)
    return v[0] if v else None


def clean_text(v) -> str:
    text = " ".join(str(x) for x in as_list(v))
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = html.unescape(TAG_RE.sub("", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:2000]


def parse_seconds(v):
    if v in (None, ""):
        return None
    try:
        if ":" in str(v):
            secs = 0.0
            for part in str(v).split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(v)
    except ValueError:
        return None


def parse_year(meta):
    for key in ("year", "date"):
        m = re.search(r"\b(18|19|20)\d{2}\b", str(first(meta.get(key)) or ""))
        if m:
            return int(m.group(0))
    return None


def pick_videos(identifier, files):
    """Pick the best MP4 derivative; if an item has several parts, return all of them."""
    by_rank = {}
    for f in files:
        name = f.get("name", "")
        if not name.lower().endswith(".mp4"):
            continue
        rank = FORMAT_RANK.get(f.get("format", "").lower())
        if rank is None:
            continue
        length = parse_seconds(f.get("length"))
        if length is not None and length < 30:
            continue
        by_rank.setdefault(rank, []).append(f)
    if not by_rank:
        return []
    chosen = sorted(by_rank[min(by_rank)], key=lambda f: f["name"])
    videos = []
    for i, f in enumerate(chosen, 1):
        label = f.get("title") or re.sub(r"(_512kb)?\.mp4$", "", f["name"].rsplit("/", 1)[-1]).replace("_", " ")
        videos.append({
            "name": label if len(chosen) > 1 else None,
            "episode": i if len(chosen) > 1 else None,
            "url": DOWNLOAD_URL.format(identifier, requests.utils.quote(f["name"])),
            "duration_sec": parse_seconds(f.get("length")),
            "size_bytes": int(f["size"]) if str(f.get("size", "")).isdigit() else None,
        })
    return videos


def genres_for(meta):
    haystack = " ".join(str(x) for x in as_list(meta.get("subject")) + as_list(meta.get("collection")))
    haystack = haystack.replace("_", " ")
    return [name for name, rx in GENRE_RES if rx.search(haystack)]


def list_items(http, query, limit=None):
    """All identifiers matching the query, most-downloaded first."""
    items, cursor = [], None
    while True:
        params = {"q": query, "fields": "identifier,title,downloads", "count": 1000}
        if cursor:
            params["cursor"] = cursor
        r = http.get(SCRAPE_URL, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        items.extend(data.get("items", []))
        log.info("listed %d items", len(items))
        cursor = data.get("cursor")
        if not cursor:
            break
    items.sort(key=lambda i: int(i.get("downloads") or 0), reverse=True)
    return items[:limit] if limit else items


def fetch_item(http, identifier, licensed_only):
    """Network + parsing only (runs in worker threads). Returns (status, payload)."""
    r = http.get(META_URL.format(identifier), timeout=60)
    r.raise_for_status()
    data = r.json()
    meta = data.get("metadata") or {}
    if not meta:
        return "skipped", "no metadata"
    if data.get("is_dark") or meta.get("access-restricted-item") == "true":
        return "skipped", "restricted"
    license_url = first(meta.get("licenseurl"))
    if licensed_only and not (license_url and re.search(r"publicdomain|creativecommons", license_url)):
        return "skipped", "no explicit license"
    videos = pick_videos(identifier, data.get("files", []))
    if not videos:
        return "skipped", "no playable mp4"

    description = clean_text(meta.get("description"))
    imdb = IMDB_RE.search(" ".join(str(x) for x in as_list(meta.get("description")) + as_list(meta.get("external-identifier"))))
    runtime = parse_seconds(first(meta.get("runtime"))) or sum(v["duration_sec"] or 0 for v in videos) or None
    return "ok", {
        "name": clean_text(meta.get("title")) or identifier,
        "description": description,
        "year": parse_year(meta),
        "runtime_min": round(runtime / 60) if runtime else None,
        "is_movie": 1,
        "thumbnail": THUMB_URL.format(identifier),
        "imdb_id": imdb.group(0) if imdb else None,
        "license": license_url,
        "genres": genres_for(meta),
        "videos": videos,
    }


def save_item(conn, identifier, popularity, item):
    cur = conn.execute(
        """INSERT INTO entities (source, source_id, name, description, year, runtime_min, is_movie,
                                 thumbnail, popularity, imdb_id, license)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (SOURCE, identifier, item["name"], item["description"], item["year"], item["runtime_min"],
         item["is_movie"], item["thumbnail"], popularity, item["imdb_id"], item["license"]),
    )
    entity_id = cur.lastrowid
    for g in item["genres"]:
        conn.execute("INSERT OR IGNORE INTO entity_categories VALUES (?, ?)", (entity_id, category_id(conn, g)))
    for v in item["videos"]:
        conn.execute(
            "INSERT INTO videos (entity_id, name, episode, url, duration_sec, size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, v["name"], v["episode"], v["url"], v["duration_sec"], v["size_bytes"]),
        )


def mark(conn, identifier, status, detail=None):
    conn.execute(
        "INSERT OR REPLACE INTO harvest_log (source, source_id, status, detail) VALUES (?, ?, ?, ?)",
        (SOURCE, identifier, status, detail),
    )


def harvest(conn, query=DEFAULT_QUERY, limit=None, workers=8, licensed_only=False):
    http = session()
    items = list_items(http, query, limit)

    # Refresh popularity for titles we already have (cheap: no extra requests).
    conn.executemany(
        "UPDATE entities SET popularity = ? WHERE source = ? AND source_id = ?",
        [(int(i.get("downloads") or 0), SOURCE, i["identifier"]) for i in items],
    )
    conn.commit()

    done = {r[0] for r in conn.execute(
        "SELECT source_id FROM harvest_log WHERE source = ? AND status IN ('ok', 'skipped')", (SOURCE,))}
    todo = [i for i in items if i["identifier"] not in done]
    log.info("%d items listed, %d already processed, %d to fetch", len(items), len(items) - len(todo), len(todo))

    counts = {"ok": 0, "skipped": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_item, http, i["identifier"], licensed_only): i for i in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            item = futures[fut]
            ident = item["identifier"]
            try:
                status, payload = fut.result()
                if status == "ok":
                    save_item(conn, ident, int(item.get("downloads") or 0), payload)
                    mark(conn, ident, "ok")
                else:
                    mark(conn, ident, status, payload)
            except Exception as e:  # noqa: BLE001 - log and keep harvesting
                conn.rollback()
                status = "error"
                mark(conn, ident, "error", str(e)[:500])
                log.warning("error on %s: %s", ident, e)
            conn.commit()
            counts[status] += 1
            if n % 100 == 0 or n == len(todo):
                log.info("progress %d/%d  %s", n, len(todo), counts)
    return counts


# ---------------------------------------------------------------- TMDB (optional)

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/"


def tmdb_session(key):
    s = session()
    if key.startswith("eyJ"):  # v4 read-access token
        s.headers["Authorization"] = f"Bearer {key}"
    else:
        s.params = {"api_key": key}
    return s


def normalize_title(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def tmdb_match(http, row):
    if row["imdb_id"]:
        r = http.get(f"{TMDB_API}/find/{row['imdb_id']}", params={"external_source": "imdb_id"}, timeout=30)
        r.raise_for_status()
        hits = r.json().get("movie_results") or []
        if hits:
            return hits[0]
    if not row["year"]:
        return None  # title-only search on archive titles is too error-prone
    r = http.get(f"{TMDB_API}/search/movie", params={"query": normalize_title(row["name"]), "year": row["year"]}, timeout=30)
    r.raise_for_status()
    want = normalize_title(row["name"])
    for hit in r.json().get("results", [])[:5]:
        if normalize_title(hit.get("title", "")) == want or normalize_title(hit.get("original_title", "")) == want:
            return hit
    return None


def enrich_tmdb(conn, key, workers=4):
    """Add real posters, backdrops, ratings and genres from TMDB where we can match a title."""
    http = tmdb_session(key)
    genre_names = {g["id"]: g["name"] for g in http.get(f"{TMDB_API}/genre/movie/list", timeout=30).json().get("genres", [])}
    rows = conn.execute("SELECT id, name, year, imdb_id, description FROM entities WHERE tmdb_checked = 0").fetchall()
    log.info("TMDB: %d titles to check", len(rows))
    matched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(tmdb_match, http, r): r for r in rows}
        for n, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                hit = fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("TMDB error on %s: %s", row["name"], e)
                continue  # leave unchecked; retried next run
            if hit:
                matched += 1
                desc = row["description"] if len(row["description"] or "") > 80 else (hit.get("overview") or row["description"])
                conn.execute(
                    """UPDATE entities SET tmdb_id = ?, rating = ?, description = ?,
                           poster = ?, backdrop = ? WHERE id = ?""",
                    (hit["id"], hit.get("vote_average") or None, desc,
                     TMDB_IMG + "w342" + hit["poster_path"] if hit.get("poster_path") else None,
                     TMDB_IMG + "w1280" + hit["backdrop_path"] if hit.get("backdrop_path") else None,
                     row["id"]),
                )
                for gid in hit.get("genre_ids", []):
                    if gid in genre_names:
                        conn.execute("INSERT OR IGNORE INTO entity_categories VALUES (?, ?)",
                                     (row["id"], category_id(conn, genre_names[gid])))
            conn.execute("UPDATE entities SET tmdb_checked = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            if n % 200 == 0:
                log.info("TMDB progress %d/%d, matched %d", n, len(rows), matched)
    log.info("TMDB done: matched %d of %d", matched, len(rows))


def run(limit=None, licensed_only=False, tmdb_only=False, workers=8):
    conn = connect()
    try:
        if not tmdb_only:
            harvest(conn, limit=limit, workers=workers, licensed_only=licensed_only)
        key = os.environ.get("TMDB_API_KEY")
        if key:
            enrich_tmdb(conn, key)
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the N most-downloaded items")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--licensed-only", action="store_true", help="require an explicit public-domain/CC license URL")
    ap.add_argument("--tmdb-only", action="store_true", help="skip harvesting, only run TMDB enrichment")
    a = ap.parse_args()
    run(limit=a.limit, licensed_only=a.licensed_only, tmdb_only=a.tmdb_only, workers=a.workers)
