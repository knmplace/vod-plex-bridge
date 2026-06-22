import asyncio
import httpx
import logging
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


async def scrape_catalog(max_movies: int = 0):
    global _cancel
    _cancel = False

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = 'scraping', message = 'Starting catalog scrape...' WHERE id = 1"
        )
        await db.commit()

        req_headers = {}
        if DISPATCHARR_API_KEY:
            req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        async with httpx.AsyncClient(timeout=30.0) as client:
            page = 1
            total_synced = 0

            while True:
                if _cancel:
                    logger.info("Catalog scrape cancelled by user")
                    break

                if max_movies > 0 and total_synced >= max_movies:
                    logger.info(f"Reached scrape limit of {max_movies}")
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

                    custom = movie.get("custom_properties") or {}
                    if isinstance(custom, str):
                        import json
                        try:
                            custom = json.loads(custom)
                        except (json.JSONDecodeError, TypeError):
                            custom = {}

                    poster_url = None
                    logo = movie.get("logo")
                    if logo and logo.get("url"):
                        poster_url = logo["url"]

                    await db.execute("""
                        INSERT INTO movies (id, uuid, name, year, rating, genre, description,
                                           tmdb_id, imdb_id, poster_url, cast_info, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            uuid=excluded.uuid, name=excluded.name, year=excluded.year,
                            rating=excluded.rating, genre=excluded.genre,
                            description=excluded.description, tmdb_id=excluded.tmdb_id,
                            poster_url=excluded.poster_url, cast_info=excluded.cast_info,
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
                        datetime.now(timezone.utc).isoformat(),
                    ))
                    total_synced += 1

                await db.commit()

                limit_msg = f" (limit: {max_movies})" if max_movies > 0 else ""
                await db.execute(
                    "UPDATE sync_state SET message = ? WHERE id = 1",
                    (f"Scraped {total_synced} movies (page {page}){limit_msg}...",),
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
        rows = await db.execute(
            "SELECT id, tmdb_id FROM movies WHERE tmdb_id IS NOT NULL AND tmdb_enriched = 0 AND tmdb_id != '' LIMIT ?",
            (batch_size,),
        )
        movies = await rows.fetchall()
        if not movies:
            return 0

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

                    await db.execute("""
                        UPDATE movies SET
                            genre = CASE WHEN genre = '' OR genre IS NULL THEN ? ELSE genre END,
                            description = CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END,
                            imdb_id = CASE WHEN imdb_id IS NULL THEN ? ELSE imdb_id END,
                            poster_url = CASE WHEN poster_url IS NULL THEN ? ELSE poster_url END,
                            tmdb_enriched = 1
                        WHERE id = ?
                    """, (genres, description, imdb_id, poster_url, movie["id"]))
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
