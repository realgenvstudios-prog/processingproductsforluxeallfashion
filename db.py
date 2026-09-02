"""SQLite schema + helpers for the catalog engine. Raw archive stays separate from extracted products."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "catalog.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS instagram_post (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode TEXT UNIQUE NOT NULL,
    post_url TEXT NOT NULL,
    caption TEXT,
    post_date TEXT,
    media_type TEXT,
    profile TEXT,
    collected_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES instagram_post(id),
    position INTEGER,
    local_path TEXT,
    ocr_text TEXT,
    vision_description TEXT,
    phash TEXT,
    excluded INTEGER DEFAULT 0,
    added_by_reviewer INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES instagram_post(id),
    is_product_post INTEGER,
    product_name TEXT,
    brand TEXT,
    category TEXT,
    description TEXT,
    price TEXT,
    currency TEXT,
    sizes TEXT,
    colors TEXT,
    availability_status TEXT,
    confidence_json TEXT,
    review_required INTEGER,
    review_reason TEXT,
    duplicate_of INTEGER REFERENCES product(id),
    dedup_score REAL,
    raw_extraction_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uploaded_image (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES product(id),
    position INTEGER,
    public_url TEXT NOT NULL,
    r2_key TEXT NOT NULL,
    uploaded_at TEXT DEFAULT (datetime('now')),
    UNIQUE(product_id, position)
);
"""


MIGRATIONS = [
    ("media", "excluded", "ALTER TABLE media ADD COLUMN excluded INTEGER DEFAULT 0"),
    ("media", "added_by_reviewer", "ALTER TABLE media ADD COLUMN added_by_reviewer INTEGER DEFAULT 0"),
]


def _run_migrations(conn):
    for table, column, statement in MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(statement)
    conn.commit()


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _run_migrations(conn)
    return conn
