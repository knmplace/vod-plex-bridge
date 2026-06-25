import asyncio
import json
import httpx
import logging
import os
from datetime import datetime, timezone

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, TMDB_API_KEY, TMDB_READ_TOKEN
from database import get_db

logger = logging.getLogger(__name__)

PAGE_SIZE = 100

_cancel = False


def request_cancel():
    global _cancel
    _cancel = True


def is_cancelled():
    return _cancel


async def _upsert_movie(db, movie):
    custom = movie.get("custom_properties") or {}
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except (json.JSONDecodeError, TypeError):
            custom = {}

    poster_url = None
    logo = movie.get("logo")
    if logo and logo.get("url"):
        poster_url = logo["url"]

    trailer_key = custom.get("youtube_trailer") or custom.get("trailer") or None

    await db.execute("""
        INSERT INTO movies (id, uuid, name, year, rating, genre, description,
                           tmdb_id, imdb_id, poster_url, cast_info, trailer_key, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid=excluded.uuid, name=excluded.name, year=excluded.year,
            rating=excluded.rating, genre=excluded.genre,
            description=excluded.description, tmdb_id=excluded.tmdb_id,
            poster_url=excluded.poster_url, cast_info=excluded.cast_info,
            trailer_key=excluded.trailer_key,
            synced_at=excluded.synced_at
    """, (
        movie["id"],
        movie["uuid"],
        movie.get("name", ""),
        movie.get("year"),
        float(movie.get("rating") or 0),
        movie.get("genre", ""),
        movie.get("description", ""),
        movie.get("tmdb_id"),
        movie.get("imdb_id"),
        poster_url,
        custom.get("cast", ""),
        trailer_key,
        datetime.now(timezone.utc).isoformat(),
    ))


BATCH_SIZE = 50


async def scrape_catalog(max_movies: int = 0, category_ids: list = None, account_ids: list = None):
    global _cancel
    _cancel = False

    db = await get_db()
    try:
        target_movie_ids = None
        if category_ids:
            placeholders = ",".join("?" for _ in category_ids)
            rows = await db.execute(
                f"SELECT DISTINCT movie_id FROM movie_categories WHERE category_id IN ({placeholders})",
                category_ids,
            )
            target_movie_ids = {r["movie_id"] for r in await rows.fetchall()}

            if not target_movie_ids:
                mapping_file = os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json")
                if os.path.exists(mapping_file):
                    logger.info("movie_categories empty, reloading from dump file")
                    with open(mapping_file) as f:
                        dump_cats = json.load(f)
                    for dc in dump_cats:
                        for mid in dc.get("movie_ids", []):
                            await db.execute(
                                "INSERT OR IGNORE INTO movie_categories (movie_id, category_id) VALUES (?, ?)",
                                (mid, dc["id"]),
                            )
                    await db.commit()
                    rows = await db.execute(
                        f"SELECT DISTINCT movie_id FROM movie_categories WHERE category_id IN ({placeholders})",
                        category_ids,
                    )
                    target_movie_ids = {r["movie_id"] for r in await rows.fetchall()}
                    logger.info(f"After reload: {len(target_movie_ids)} target movies")

            if target_movie_ids and account_ids:
                stream_map_file = os.environ.get("STREAM_MAPPING_FILE", "/data/stream_mapping.json")
                if os.path.exists(stream_map_file):
                    with open(stream_map_file) as f:
                        stream_map = json.load(f)
                    acct_set = set(account_ids)
                    provider_movie_ids = set()
                    for mid, info in stream_map.items():
                        entries = info if isinstance(info, list) else [info]
                        if any(e.get("account_id") in acct_set for e in entries):
                            provider_movie_ids.add(int(mid))
                    before = len(target_movie_ids)
                    target_movie_ids = target_movie_ids & provider_movie_ids
                    logger.info(f"Provider filter: {before} -> {len(target_movie_ids)} movies (accounts {account_ids})")

            logger.info(f"Category filter: {len(target_movie_ids)} target movies across {len(category_ids)} categories")

            if not target_movie_ids:
                await db.execute(
                    "UPDATE sync_state SET status = 'idle', message = 'No movies in selected categories. Load categories first.' WHERE id = 1"
                )
                await db.commit()
                return 0

        await db.execute(
            "UPDATE sync_state SET status = 'scraping', message = 'Starting catalog scrape...', lang_status = '' WHERE id = 1"
        )
        await db.commit()

        req_headers = {}
        if DISPATCHARR_API_KEY:
            req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        total_synced = 0

        if target_movie_ids is not None:
            # Targeted fetch: only request movies we need by ID
            remaining = set(target_movie_ids)
            if max_movies > 0:
                remaining = set(list(remaining)[:max_movies])
            total_target = len(remaining)

            async with httpx.AsyncClient(timeout=30.0) as client:
                id_list = sorted(remaining)
                for i in range(0, len(id_list), BATCH_SIZE):
                    if _cancel:
                        break

                    batch = id_list[i:i + BATCH_SIZE]
                    for movie_id in batch:
                        if _cancel:
                            break
                        try:
                            url = f"{DISPATCHARR_URL}/api/vod/movies/{movie_id}/"
                            resp = await client.get(url, headers=req_headers)
                            if resp.status_code != 200:
                                continue
                            movie = resp.json()
                            await _upsert_movie(db, movie)
                            total_synced += 1
                            remaining.discard(movie_id)
                        except Exception as e:
                            logger.warning(f"Failed to fetch movie {movie_id}: {e}")

                    await db.commit()
                    await db.execute(
                        "UPDATE sync_state SET message = ? WHERE id = 1",
                        (f"Fetched {total_synced}/{total_target} movies...",),
                    )
                    await db.commit()
                    await asyncio.sleep(0.05)
        else:
            # Full scrape: page through all movies
            async with httpx.AsyncClient(timeout=30.0) as client:
                page = 1
                while True:
                    if _cancel:
                        break
                    if max_movies > 0 and total_synced >= max_movies:
                        break

                    url = f"{DISPATCHARR_URL}/api/vod/movies/?page={page}&page_size={PAGE_SIZE}"
                    resp = await client.get(url, headers=req_headers)
                    if resp.status_code != 200:
                        logger.error(f"API error on page {page}: {resp.status_code}")
                        break

                    data = resp.json()
                    results = data.get("results", [])
                    if not results:
                        break

                    for movie in results:
                        if _cancel:
                            break
                        if max_movies > 0 and total_synced >= max_movies:
                            break
                        await _upsert_movie(db, movie)
                        total_synced += 1

                    await db.commit()
                    await db.execute(
                        "UPDATE sync_state SET message = ? WHERE id = 1",
                        (f"Scraped {total_synced} movies (page {page})...",),
                    )
                    await db.commit()

                    if not data.get("next"):
                        break
                    page += 1
                    await asyncio.sleep(0.1)

        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies")
        count = (await count_row.fetchone())["cnt"]

        status_msg = "cancelled" if _cancel else "complete"
        await db.execute(
            "UPDATE sync_state SET last_catalog_sync = ?, total_movies = ?, status = 'idle', message = ? WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(), count, f"Catalog sync {status_msg}: {total_synced} movies"),
        )
        await db.commit()
        logger.info(f"Catalog scrape {status_msg}: {total_synced} movies synced, {count} total in DB")
        _cancel = False
        return total_synced
    finally:
        await db.close()


async def enrich_from_tmdb(batch_size: int = 50):
    global _cancel

    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        logger.warning("TMDB_API_KEY/TMDB_READ_TOKEN not set, skipping enrichment")
        return 0

    db = await get_db()
    try:
        remaining_row = await db.execute(
            "SELECT COUNT(*) as cnt FROM movies WHERE tmdb_id IS NOT NULL AND tmdb_enriched = 0 AND tmdb_id != ''"
        )
        remaining = (await remaining_row.fetchone())["cnt"]

        rows = await db.execute(
            "SELECT id, tmdb_id FROM movies WHERE tmdb_id IS NOT NULL AND tmdb_enriched = 0 AND tmdb_id != '' LIMIT ?",
            (batch_size,),
        )
        movies = await rows.fetchall()
        if not movies:
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = 'TMDB enrichment complete' WHERE id = 1"
            )
            await db.commit()
            return 0

        await db.execute(
            "UPDATE sync_state SET status = 'enriching', message = ? WHERE id = 1",
            (f"Enriching genres from TMDB... {remaining} remaining",),
        )
        await db.commit()

        enriched = 0
        async with httpx.AsyncClient(timeout=10.0) as client:
            for movie in movies:
                if _cancel:
                    logger.info("TMDB enrichment cancelled by user")
                    break
                try:
                    url = f"https://api.themoviedb.org/3/movie/{movie['tmdb_id']}"
                    headers = {}
                    params = {}
                    if TMDB_READ_TOKEN:
                        headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
                    else:
                        params["api_key"] = TMDB_API_KEY
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code != 200:
                        continue

                    tmdb_data = resp.json()
                    genres = ", ".join(g["name"] for g in tmdb_data.get("genres", []))
                    description = tmdb_data.get("overview", "")
                    imdb_id = tmdb_data.get("imdb_id")
                    poster_path = tmdb_data.get("poster_path")
                    poster_url = f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{poster_path}" if poster_path else None
                    runtime_min = tmdb_data.get("runtime")
                    duration_seconds = runtime_min * 60 if runtime_min else None

                    await db.execute("""
                        UPDATE movies SET
                            genre = CASE WHEN genre = '' OR genre IS NULL THEN ? ELSE genre END,
                            description = CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END,
                            imdb_id = CASE WHEN imdb_id IS NULL THEN ? ELSE imdb_id END,
                            poster_url = CASE WHEN poster_url IS NULL THEN ? ELSE poster_url END,
                            duration_seconds = CASE WHEN duration_seconds IS NULL THEN ? ELSE duration_seconds END,
                            tmdb_enriched = 1
                        WHERE id = ?
                    """, (genres, description, imdb_id, poster_url, duration_seconds, movie["id"]))
                    enriched += 1
                    await asyncio.sleep(0.25)
                except Exception as e:
                    logger.error(f"TMDB enrichment failed for movie {movie['id']}: {e}")

        await db.commit()
        logger.info(f"TMDB enrichment complete: {enriched} movies enriched")
        _cancel = False
        return enriched
    finally:
        await db.close()
