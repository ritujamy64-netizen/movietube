"""SQLite schema: entities (titles), videos (playable files), categories (genres).

Same shape as the article's Entities/Videos/Category design, except an entity
can belong to many categories.
"""
import os
import sqlite3


def _load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Load KEY=VALUE lines from .env (real environment variables win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "library.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT,
    year         INTEGER,
    runtime_min  INTEGER,
    is_movie     INTEGER NOT NULL DEFAULT 1,
    thumbnail    TEXT,
    poster       TEXT,
    backdrop     TEXT,
    rating       REAL,
    popularity   INTEGER NOT NULL DEFAULT 0,
    imdb_id      TEXT,
    tmdb_id      INTEGER,
    tmdb_checked INTEGER NOT NULL DEFAULT 0,
    license      TEXT,
    added_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_entities_popularity ON entities (popularity DESC);
CREATE INDEX IF NOT EXISTS idx_entities_added ON entities (added_at DESC);

CREATE TABLE IF NOT EXISTS entity_categories (
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (entity_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_ec_category ON entity_categories (category_id);

CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY,
    entity_id    INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    name         TEXT,
    season       INTEGER,
    episode      INTEGER,
    url          TEXT NOT NULL,
    duration_sec REAL,
    size_bytes   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_videos_entity ON videos (entity_id);

-- Every item the harvester has looked at, so a crash or re-run never redoes work.
CREATE TABLE IF NOT EXISTS harvest_log (
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status    TEXT NOT NULL,          -- ok | skipped | error
    detail    TEXT,
    at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, source_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts
    USING fts5(name, description, content='entities', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts (rowid, name, description) VALUES (new.id, new.name, new.description);
END;
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts (entities_fts, rowid, name, description) VALUES ('delete', old.id, old.name, old.description);
END;
CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE OF name, description ON entities BEGIN
    INSERT INTO entities_fts (entities_fts, rowid, name, description) VALUES ('delete', old.id, old.name, old.description);
    INSERT INTO entities_fts (rowid, name, description) VALUES (new.id, new.name, new.description);
END;
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def category_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
