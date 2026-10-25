import asyncio
import json
import re
import httpx
import logging
import os
from datetime import datetime, timezone

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY
from database import get_db
import tmdb_local

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


MOVIE_METADATA_FILE = os.environ.get("MOVIE_METADATA_FILE", "/data/movie_metadata.json")
CATEGORY_MAPPING_FILE = os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json")
# Dispatcharr's Postgres runs out of shared memory and returns 500s on list pages above this.
FETCH_CONCURRENCY = 4
# Below this many ids, individual GETs beat paging the whole list.
PAGE_LIST_MIN_IDS = 2000


def _load_metadata_dump():
    """Movies from the host dump keyed by id, or None when the file is missing,
    unreadable, or left over from an older dump run than the category mapping."""
    try:
        mtime = os.path.getmtime(MOVIE_METADATA_FILE)
        if os.path.exists(CATEGORY_MAPPING_FILE) and mtime < os.path.getmtime(CATEGORY_MAPPING_FILE) - 600:
            logger.warning("Movie metadata dump is older than the category dump, using the Dispatcharr API")
            return None
        with open(MOVIE_METADATA_FILE) as f:
            return {m["id"]: m for m in json.load(f)}
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"Movie metadata dump unreadable, using the Dispatcharr API: {e}")
        return None


async def fetch_movies(ids, progress=None) -> tuple[dict, set, int]:
    """Fetch movie records for `ids` from Dispatcharr.

    Reads the host metadata dump when there is a current one, otherwise the
    API. Ids the dump doesn't have are looked up individually, so "gone"
    always means a real 404. Returns (found {id: movie}, gone ids, failed count).
    """
    wanted = set(ids)
    dump = await asyncio.to_thread(_load_metadata_dump)
    if dump is None:
        return await _fetch_movies_api(wanted, progress)

    found = {i: dump[i] for i in wanted if i in dump and (dump[i].get("name") or "").strip()}
    missing = wanted - dump.keys()
    logger.info(f"Read {len(found)} movies from the metadata dump, {len(missing)} not in it")
    if not missing:
        return found, set(), 0
    api_found, gone, failed = await _fetch_movies_api(missing, progress)
    found.update(api_found)
    return found, gone, failed


async def _fetch_movies_api(wanted: set, progress=None) -> tuple[dict, set, int]:
    """Fetch from the Dispatcharr API. Large requests page the whole list
    concurrently (100 per page is the API cap); anything the list didn't
    return is then looked up individually."""
    found = {}
    seen = set()
    headers = {"X-API-Key": DISPATCHARR_API_KEY} if DISPATCHARR_API_KEY else {}
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async with httpx.AsyncClient(timeout=60.0) as client:
        async def get_page(page):
            for attempt in range(3):
                try:
                    async with sem:
                        resp = await client.get(
                            f"{DISPATCHARR_URL}/api/vod/movies/?page={page}&page_size={PAGE_SIZE}",
                            headers=headers)
                    if resp.status_code == 200:
                        return resp.json()
                    if resp.status_code == 404:
                        return None
                except Exception as e:
                    logger.warning(f"Movie list page {page} failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(1 + attempt)
            return None

        def keep(data):
            for m in (data or {}).get("results", []):
                if m["id"] in wanted:
                    seen.add(m["id"])
                    if m.get("name", "").strip():
                        found[m["id"]] = m

        if len(wanted) >= PAGE_LIST_MIN_IDS:
            first = await get_page(1)
            if not first:
                logger.error("Dispatcharr movie list unavailable, skipping fetch")
                return found, set(), len(wanted)
            keep(first)
            pages = -(-int(first.get("count") or 0) // PAGE_SIZE)
            done = 1
            tasks = [asyncio.create_task(get_page(p)) for p in range(2, pages + 1)]
            try:
                for task in asyncio.as_completed(tasks):
                    keep(await task)
                    done += 1
                    if progress and done % 20 == 0:
                        await progress(f"Reading Dispatcharr catalog: page {done}/{pages}...")
                    if _cancel:
                        break
            finally:
                for t in tasks:
                    t.cancel()
            if _cancel:
                return found, set(), 0

        # Anything the list didn't return (moved between pages mid-read, failed page, or truly gone).
        gone, failed = set(), 0
        missing = sorted(wanted - seen)
        if missing:
            if progress:
                await progress(f"Checking {len(missing)} movies individually...")

            async def get_one(movie_id):
                nonlocal failed
                for attempt in range(3):
                    try:
                        async with sem:
                            resp = await client.get(f"{DISPATCHARR_URL}/api/vod/movies/{movie_id}/", headers=headers)
                        if resp.status_code == 200:
                            m = resp.json()
                            if m.get("name", "").strip():
                                found[movie_id] = m
                            return
                        if resp.status_code == 404:
                            gone.add(movie_id)
                            return
                    except Exception as e:
                        logger.warning(f"Failed to fetch movie {movie_id} (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(1 + attempt)
                failed += 1

            await asyncio.gather(*(get_one(m) for m in missing))
    return found, gone, failed


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
                await db.executemany(
                    "INSERT OR IGNORE INTO movie_categories (movie_id, category_id) VALUES (?, ?)",
                    [(mid, dc["id"]) for dc in dump_cats if dc["id"] in selected_cat_set
                     for mid in dc.get("movie_ids", [])],
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

            async def progress(msg):
                await db.execute("UPDATE sync_state SET message = ? WHERE id = 1", (msg,))
                await db.commit()

            found, _gone, _failed = await fetch_movies(remaining, progress)
            for i, movie in enumerate(found.values()):
                if _cancel:
                    break
                await _upsert_movie(db, movie)
                total_synced += 1
                if (i + 1) % 1000 == 0:
                    await db.commit()
                    await progress(f"Saved {total_synced}/{total_target} movies...")
            await db.commit()
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


async def enrich_from_tmdb(limit: int = 5000) -> dict:
    """Same flow as VOD Manager: each movie with a TMDB ID is enriched once (local
    store first, TMDB on a miss), and re-applied when the store refreshes that
    title. TMDB wins for genre/description/poster/IMDb ID; runtime only fills a
    blank duration, since the provider's real length drives the proxy throttle."""
    global _cancel
    result = {"enriched": 0, "changed": 0, "nfo_rewritten": 0, "store_refreshed": 0, "remaining": 0}
    if not tmdb_local.has_credentials():
        logger.warning("TMDB_API_KEY/TMDB_READ_TOKEN not set, skipping enrichment")
        return result

    import time
    import tmdb_store
    run_started = time.time()
    db = await get_db()

    # Titles the store refreshed since the last pass are re-applied from the store.
    since = float(await asyncio.to_thread(tmdb_store.get_meta, "catalog_applied_at", "0") or 0)
    if since:
        ids = [str(i) for i in await asyncio.to_thread(tmdb_store.list_fetched_since, "movie", since)]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur = await db.execute(
                f"UPDATE movies SET tmdb_enriched_at = NULL WHERE tmdb_enriched_at IS NOT NULL "
                f"AND tmdb_id IN ({','.join('?' * len(chunk))})", chunk,
            )
            result["store_refreshed"] += cur.rowcount
        await db.commit()

    pending = "tmdb_id IS NOT NULL AND tmdb_id != '' AND tmdb_enriched_at IS NULL AND name != ''"
    rows = await db.execute(
        f"SELECT id, tmdb_id, genre, description, imdb_id, poster_url, duration_seconds, activated "
        f"FROM movies WHERE {pending} ORDER BY activated DESC LIMIT ?", (limit,),
    )
    movies = await rows.fetchall()

    rewrite = []
    for movie in movies:
        if _cancel:
            logger.info("TMDB enrichment cancelled by user")
            break
        try:
            tmdb_data, local = await tmdb_local.movie_details(movie["tmdb_id"])
            if tmdb_data is None:
                continue
            poster_path = tmdb_data.get("poster_path")
            runtime_min = tmdb_data.get("runtime")
            new = {
                "genre": ", ".join(g["name"] for g in tmdb_data.get("genres", [])),
                "description": tmdb_data.get("overview") or "",
                "imdb_id": tmdb_data.get("imdb_id") or "",
                "poster_url": f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{poster_path}" if poster_path else "",
            }
            changes = {k: v for k, v in new.items() if v and v != (movie[k] or "")}
            if runtime_min and not movie["duration_seconds"]:
                changes["duration_seconds"] = runtime_min * 60
            sets = "".join(f"{k} = ?, " for k in changes)
            await db.execute(
                f"UPDATE movies SET {sets}tmdb_enriched = 1, tmdb_enriched_at = ? WHERE id = ?",
                (*changes.values(), datetime.now(timezone.utc).isoformat(), movie["id"]),
            )
            result["enriched"] += 1
            if changes:
                result["changed"] += 1
                if movie["activated"]:
                    rewrite.append((movie["id"], "poster_url" in changes))
            if not local:
                await asyncio.sleep(0.25)
        except Exception as e:
            logger.error(f"TMDB enrichment failed for movie {movie['id']}: {e}")
    await db.commit()
    _cancel = False

    from generator import write_strm_for_movie, get_folder_name, STRM_OUTPUT_DIR
    for movie_id, poster_changed in rewrite:
        try:
            row = await (await db.execute("SELECT * FROM movies WHERE id = ?", (movie_id,))).fetchone()
            movie = dict(row)
            if poster_changed:
                poster = os.path.join(STRM_OUTPUT_DIR, get_folder_name(movie), "poster.jpg")
                if os.path.exists(poster):
                    os.remove(poster)
            if await write_strm_for_movie(movie):
                result["nfo_rewritten"] += 1
        except Exception as e:
            logger.error(f"NFO rewrite failed for movie {movie_id}: {e}")

    await asyncio.to_thread(tmdb_store.set_meta, "catalog_applied_at", run_started)
    row = await db.execute(f"SELECT COUNT(*) AS cnt FROM movies WHERE {pending}")
    result["remaining"] = (await row.fetchone())["cnt"]
    logger.info("TMDB enrichment: %s", result)
    return result


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

    if not tmdb_local.has_credentials():
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
            search_year = int(year) if str(year or "").isdigit() else None
            results, local = await tmdb_local.search_movie(clean, search_year)
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

            if not local:
                await asyncio.sleep(0.25)

        except Exception as e:
            logger.warning("TMDB search error for movie %d (%s): %s", movie_id, raw_name, e)
            skipped += 1

    await db.commit()
    logger.info("TMDB title search complete: %d found, %d not found, %d skipped", found, not_found, skipped)
    return {"found": found, "not_found": not_found, "skipped": skipped}
