# MovieTube

A Netflix-style streaming site for public-domain films. Metadata comes from the
Internet Archive's `feature_films` collection (about 28,000 titles), and video
streams directly from archive.org. Your server never serves video, so hosting
costs stay small.

## Architecture

```
harvester.py  ── scrape API + metadata API ──▶  SQLite (entities / videos / categories)
     ▲  runs weekly in a background thread               │
     │                                                   ▼
   app.py (FastAPI)  ── /api/* JSON ──▶  static/ (vanilla JS SPA)  ── <video src=archive.org/...>
```

- **Harvester**: resumable. Each title is committed as soon as it's processed
  and logged in `harvest_log`, so a crash or re-run only picks up new or failed
  items. Each run also refreshes popularity counts.
- **TMDB enrichment** (optional): set `TMDB_API_KEY` (a free key from
  themoviedb.org) to add proper posters, backdrops, ratings and genres. Titles
  are matched by IMDb ID, or by exact title and year.
- **Frontend**: hero banner, category rows, genre pages, search, a details
  popup, a player with resume, and Continue Watching and My List (stored in
  each visitor's browser, no accounts).

## New & Popular (where to watch newer movies)

With `TMDB_API_KEY` set, a **New & Popular** tab appears. It shows trending
movies, new on streaming, popular, free-with-ads, top-rated and in-theaters
rows. Each movie's popup lists where it's available in the viewer's country:
subscription, free, rent or buy, with provider logos. Major services link to a
search for that title on the service, and the rest link to TMDB's watch page.
The popup also has a YouTube trailer. If a newer movie happens to be in the
free library, it gets a **Watch free here** button. Search shows these newer
movies under the free library's results.

Nothing is hosted or embedded; it only links out. Availability data comes
from JustWatch via TMDB, and the footer credits both, which their terms
require. `WATCH_REGION` sets the default country, and visitors can change it
on the page. TMDB responses are cached in memory for 6 hours.

## Stats dashboard (/admin)

Set `ADMIN_PASSWORD` and open `/admin`. Log in on the login page with that
password. (Scripts can also use HTTP Basic auth, e.g. `curl -u admin:PASSWORD /api/admin/stats`.) The dashboard shows:

- visitors, page views, plays, watch time, and the share of visitors who press
  play, each compared with the previous period
- visitors and plays per day, as charts
- most-watched films with average completion, and films people abandon early
- top searches and searches with no results
- clicks to streaming services and the most-opened New & Popular titles
- pages, traffic sources and devices (plus countries if the site runs behind
  Cloudflare)

Tracking is first-party and anonymous. Each browser gets a random ID in
localStorage. No cookies, no IP addresses and no third parties are involved.
Events are kept for 400 days. Without `ADMIN_PASSWORD`, `/admin` is disabled.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env                   # then add your TMDB key and admin password
python harvester.py --limit 300        # quick test library
HARVEST_INTERVAL_HOURS=0 uvicorn app:app --reload
```

A full harvest (`python harvester.py`) takes a few hours the first time.

## Deploy

Any host that runs a Docker container and has a persistent disk works, such as
Fly.io, Railway, Render or a small VPS. Mount a volume at `/data`.

```bash
docker build -t movietube .
docker run -p 8000:8000 -v movietube-data:/data -e TMDB_API_KEY=... movietube
```

On first boot the library is empty and fills in while you watch. After that it
re-harvests weekly (`HARVEST_INTERVAL_HOURS`, default 168). On very small hosts,
set `HARVEST_LIMIT=2000` to keep it light.

## Content & legal notes

- Everything in the collection was uploaded as public domain, but the
  Archive's labeling isn't perfect. For a stricter catalog, run with
  `--licensed-only`, which keeps only items with an explicit public-domain or
  Creative Commons license URL. Remove any title you get a valid complaint
  about.
- The TMDB attribution in the footer is required by TMDB's terms if you use
  their data.
- Be polite to archive.org: the harvester uses 8 workers with retry and
  back-off. Don't raise that much.
