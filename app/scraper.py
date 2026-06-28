import asyncio
import json
import re
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
    cast_info = custom.get("actors") or custom.get("cast") or ""
    director = custom.get("director") or ""
    backdrop_paths = custom.get("backdrop_path") or []
    backdrop_url = backdrop_paths[0] if isinstance(backdrop_paths, list) and backdrop_paths else ""

    new_uuid = movie["uuid"]
    movie_id = movie["id"]

    row = await db.execute("SELECT uuid FROM movies WHERE id = ?", (movie_id,))
    existing = await row.fetchone()
    if existing and existing["uuid"] and existing["uuid"] != new_uuid:
        await db.execute(
            "UPDATE movies SET header_data = NULL, header_size = 0, "
            "tail_data = NULL, tail_size = 0, tail_offset = 0, file_size = NULL "
            "WHERE id = ?",
            (movie_id,),
        )
        logger.info("UUID changed for movie %d (%s -> %s), cleared caches", movie_id, existing["uuid"], new_uuid)

    await db.execute("""
        INSERT INTO movies (id, uuid, name, year, rating, genre, description,
                           tmdb_id, imdb_id, poster_url, cast_info, trailer_key,
                           director, backdrop_url, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid=excluded.uuid, name=excluded.name, year=excluded.year,
            rating=excluded.rating, genre=excluded.genre,
            description=excluded.description, tmdb_id=excluded.tmdb_id,
            poster_url=excluded.poster_url,
            cast_info = CASE WHEN movies.cast_info IS NULL OR movies.cast_info = '' THEN excluded.cast_info ELSE movies.cast_info END,
            trailer_key=excluded.trailer_key,
            director = CASE WHEN movies.director IS NULL OR movies.director = '' THEN excluded.director ELSE movies.director END,
            backdrop_url = CASE WHEN movies.backdrop_url IS NULL OR movies.backdrop_url = '' THEN excluded.backdrop_url ELSE movies.backdrop_url END,
            synced_at=excluded.synced_at
    """, (
        movie_id,
        new_uuid,
        movie.get("name", ""),
        movie.get("year"),
        float(movie.get("rating") or 0),
        movie.get("genre", ""),
        movie.get("description", ""),
        movie.get("tmdb_id"),
        movie.get("imdb_id"),
        poster_url,
        cast_info,
        trailer_key,
        director,
        backdrop_url,
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
            mapping_file = os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json")
            if os.path.exists(mapping_file):
                with open(mapping_file) as f:
                    dump_cats = json.load(f)
                selected_cat_set = set(category_ids)
                target_movie_ids = set()
                for dc in dump_cats:
                    if dc["id"] in selected_cat_set:
                        target_movie_ids.update(dc.get("movie_ids", []))
                logger.info(f"Category mapping: {len(target_movie_ids)} movies across {len(category_ids)} categories")

                # Populate movie_categories table for future queries
                for dc in dump_cats:
                    if dc["id"] in selected_cat_set:
                        for mid in dc.get("movie_ids", []):
                            await db.execute(
                                "INSERT OR IGNORE INTO movie_categories (movie_id, category_id) VALUES (?, ?)",
                                (mid, dc["id"]),
                            )
                await db.commit()
            else:
                placeholders = ",".join("?" for _ in category_ids)
                rows = await db.execute(
                    f"SELECT DISTINCT movie_id FROM movie_categories WHERE category_id IN ({placeholders})",
                    category_ids,
                )
                target_movie_ids = {r["movie_id"] for r in await rows.fetchall()}

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
                    "UPDATE sync_state SET status = 'idle', message = 'No movies in selected categories/providers. Try selecting more providers or reload categories.' WHERE id = 1"
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
                            if not movie.get("name", "").strip():
                                logger.info(f"Skipping movie {movie_id}: empty name")
                                remaining.discard(movie_id)
                                continue
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
                        if not movie.get("name", "").strip():
                            continue
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
        pass


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
        pass


def _clean_title(name: str) -> str:
    """Strip year suffixes, brackets, quality tags, and codec info from movie names for TMDB search."""
    clean = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', name).strip()
    clean = re.sub(r'\s*\(\d{4}\)\s*$', '', clean).strip()
    clean = re.sub(r'\[.*?\]', '', clean).strip()
    clean = re.sub(r'\b(1080p|720p|480p|2160p|4K|WEB|HDTV|BluRay|BRRip|DVDRip|AAC|x264|x265|HEVC|VFF)\b.*', '', clean, flags=re.IGNORECASE).strip()
    return clean


def _title_similarity(a: str, b: str) -> float:
    """Simple word-overlap similarity between two titles."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


async def search_tmdb_for_missing(batch_size: int = 50) -> dict:
    """Search TMDB by title for movies that have no tmdb_id and haven't been searched yet.
    Returns {found: int, not_found: int, skipped: int}."""
    global _cancel

    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        logger.warning("TMDB API key not set, skipping title search")
        return {"found": 0, "not_found": 0, "skipped": 0}

    db = await get_db()
    rows = await db.execute(
        "SELECT id, name, year FROM movies "
        "WHERE (tmdb_id IS NULL OR tmdb_id = '') AND tmdb_searched = 0 AND name != '' "
        "LIMIT ?",
        (batch_size,),
    )
    movies = [dict(r) for r in await rows.fetchall()]

    if not movies:
        return {"found": 0, "not_found": 0, "skipped": 0}

    logger.info("TMDB title search: %d movies to look up", len(movies))

    found = 0
    not_found = 0
    skipped = 0

    headers = {}
    params_base = {}
    if TMDB_READ_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
    else:
        params_base["api_key"] = TMDB_API_KEY

    async with httpx.AsyncClient(timeout=10.0) as client:
        for movie in movies:
            if _cancel:
                break

            movie_id = movie["id"]
            raw_name = movie["name"]
            year = movie["year"]

            clean = _clean_title(raw_name)
            if not clean or len(clean) < 2:
                await db.execute("UPDATE movies SET tmdb_searched = 1 WHERE id = ?", (movie_id,))
                skipped += 1
                continue

            try:
                search_params = {**params_base, "query": clean}
                if year:
                    search_params["year"] = year

                resp = await client.get(
                    "https://api.themoviedb.org/3/search/movie",
                    params=search_params,
                    headers=headers,
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    await asyncio.sleep(retry_after)
                    resp = await client.get(
                        "https://api.themoviedb.org/3/search/movie",
                        params=search_params,
                        headers=headers,
                    )

                if resp.status_code != 200:
                    skipped += 1
                    continue

                results = resp.json().get("results", [])
                best = None
                best_score = 0.0

                for r in results[:5]:
                    tmdb_title = r.get("title", "")
                    tmdb_year = (r.get("release_date") or "")[:4]
                    score = _title_similarity(clean, tmdb_title)
                    if year and tmdb_year and str(year) == tmdb_year:
                        score += 0.3
                    if score > best_score:
                        best_score = score
                        best = r

                if best and best_score >= 0.5:
                    tmdb_id = str(best["id"])
                    poster_path = best.get("poster_path")
                    poster_url = f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{poster_path}" if poster_path else None
                    description = best.get("overview", "")
                    lang = best.get("original_language", "")

                    await db.execute(
                        "UPDATE movies SET tmdb_id = ?, tmdb_searched = 1, "
                        "poster_url = CASE WHEN poster_url IS NULL THEN ? ELSE poster_url END, "
                        "description = CASE WHEN description = '' OR description IS NULL THEN ? ELSE description END, "
                        "language = CASE WHEN language = '' OR language IS NULL THEN ? ELSE language END "
                        "WHERE id = ?",
                        (tmdb_id, poster_url, description, lang, movie_id),
                    )
                    found += 1
                    logger.info("TMDB search matched: %s → %s (tmdb=%s, score=%.2f)",
                                raw_name, best.get("title"), tmdb_id, best_score)
                else:
                    await db.execute("UPDATE movies SET tmdb_searched = 1 WHERE id = ?", (movie_id,))
                    not_found += 1
                    logger.info("TMDB search: no match for '%s' (best_score=%.2f)", raw_name, best_score)

                await asyncio.sleep(0.25)

            except Exception as e:
                logger.warning("TMDB search error for movie %d (%s): %s", movie_id, raw_name, e)
                skipped += 1

    await db.commit()
    logger.info("TMDB title search complete: %d found, %d not found, %d skipped", found, not_found, skipped)
    return {"found": found, "not_found": not_found, "skipped": skipped}
