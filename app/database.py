import aiosqlite
import os

DB_PATH = os.environ.get("DB_PATH", "/data/vod_bridge.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY,
                uuid TEXT NOT NULL,
                name TEXT NOT NULL,
                year INTEGER,
                rating REAL,
                genre TEXT DEFAULT '',
                description TEXT DEFAULT '',
                tmdb_id TEXT,
                imdb_id TEXT,
                poster_url TEXT,
                cast_info TEXT DEFAULT '',
                stream_id INTEGER,
                content_type TEXT DEFAULT 'video/x-matroska',
                synced_at TEXT,
                tmdb_enriched INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS filter_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                genre TEXT NOT NULL,
                limit_count INTEGER DEFAULT 30,
                sort_by TEXT DEFAULT 'rating',
                sort_order TEXT DEFAULT 'desc',
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_catalog_sync TEXT,
                last_strm_sync TEXT,
                total_movies INTEGER DEFAULT 0,
                active_strm_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'idle',
                message TEXT DEFAULT ''
            );

            INSERT OR IGNORE INTO sync_state (id) VALUES (1);

            CREATE INDEX IF NOT EXISTS idx_movies_genre ON movies(genre);
            CREATE INDEX IF NOT EXISTS idx_movies_rating ON movies(rating);
            CREATE INDEX IF NOT EXISTS idx_movies_year ON movies(year);
            CREATE INDEX IF NOT EXISTS idx_movies_tmdb ON movies(tmdb_id);
        """)
        await db.commit()
    finally:
        await db.close()
