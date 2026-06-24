import asyncio
import logging
import os
import re
import shutil
import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, STRM_OUTPUT_DIR, PLEX_URL, PLEX_TOKEN, PLEX_LIBRARY_ID, TMDB_API_KEY, TMDB_READ_TOKEN
from database import get_db
from scraper import scrape_catalog, enrich_from_tmdb, request_cancel, is_cancelled
from generator import generate_strm_files, sanitize_filename
from stream_mapper import apply_stream_mapping_to_db, load_stream_mapping, pick_stream_for_account, get_xc_url
from proxy import probe_file_size, get_proxy_log, _log_event
from health import run_health_checks, get_health_status, get_health_log, health_check_scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

DEAD_SCAN_INTERVAL_HOURS = 12


@router.get("/status")
async def get_status():
    db = await get_db()
    try:
        row = await db.execute("SELECT * FROM sync_state WHERE id = 1")
        state = await row.fetchone()
        return dict(state)
    finally:
        await db.close()


@router.get("/providers")
async def list_providers():
    db = await get_db()
    try:
        rows = await db.execute("SELECT id, name FROM m3u_accounts ORDER BY name")
        accounts = [dict(r) for r in await rows.fetchall()]

        sel_rows = await db.execute("SELECT account_id FROM selected_accounts WHERE enabled = 1")
        selected = {r["account_id"] for r in await sel_rows.fetchall()}

        for a in accounts:
            a["selected"] = a["id"] in selected

        return accounts
    finally:
        await db.close()


@router.post("/providers/select")
async def select_provider(request: Request):
    data = await request.json()
    account_id = data.get("account_id")
    if not account_id:
        return JSONResponse(status_code=400, content={"error": "account_id required"})
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO selected_accounts (account_id, enabled) VALUES (?, 1)",
            (account_id,),
        )
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/providers/deselect")
async def deselect_provider(request: Request):
    data = await request.json()
    account_id = data.get("account_id")
    if not account_id:
        return JSONResponse(status_code=400, content={"error": "account_id required"})
    db = await get_db()
    try:
        await db.execute("DELETE FROM selected_accounts WHERE account_id = ?", (account_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.get("/categories")
async def list_categories(account_id: int = 0):
    db = await get_db()
    try:
        if account_id:
            rows = await db.execute(
                "SELECT c.id, c.name, c.category_type, c.movie_count "
                "FROM vod_categories c "
                "JOIN vod_category_accounts ca ON c.id = ca.category_id "
                "WHERE ca.account_id = ? ORDER BY c.name",
                (account_id,),
            )
        else:
            rows = await db.execute(
                "SELECT id, name, category_type, movie_count FROM vod_categories ORDER BY name"
            )
        cats = [dict(r) for r in await rows.fetchall()]

        sel_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
        selected = {r["category_id"] for r in await sel_rows.fetchall()}

        for c in cats:
            c["selected"] = c["id"] in selected

        return cats
    finally:
        await db.close()


@router.post("/categories/select")
async def select_category(request: Request):
    data = await request.json()
    category_id = data.get("category_id")
    if not category_id:
        return JSONResponse(status_code=400, content={"error": "category_id required"})

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO selected_categories (category_id, enabled) VALUES (?, 1)",
            (category_id,),
        )
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/categories/deselect")
async def deselect_category(request: Request):
    data = await request.json()
    category_id = data.get("category_id")
    if not category_id:
        return JSONResponse(status_code=400, content={"error": "category_id required"})

    db = await get_db()
    try:
        await db.execute("DELETE FROM selected_categories WHERE category_id = ?", (category_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


DUMP_TRIGGER_FILE = "/data/.dump-trigger"


@router.post("/categories/load")
async def load_categories():
    asyncio.create_task(_load_categories_with_dump())
    return {"status": "started", "message": "Regenerating dumps and loading categories..."}


CATEGORY_MAPPING_FILE = os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json")
STREAM_MAPPING_FILE = os.environ.get("STREAM_MAPPING_FILE", "/data/stream_mapping.json")


async def _trigger_dump_regen():
    try:
        cat_mtime_before = os.path.getmtime(CATEGORY_MAPPING_FILE) if os.path.exists(CATEGORY_MAPPING_FILE) else 0
        with open(DUMP_TRIGGER_FILE, "w") as f:
            f.write("1")
        logger.info("Dump trigger file written, waiting for host cron to regenerate...")
        for _ in range(90):
            await asyncio.sleep(2)
            if os.path.exists(CATEGORY_MAPPING_FILE):
                cat_mtime_now = os.path.getmtime(CATEGORY_MAPPING_FILE)
                if cat_mtime_now > cat_mtime_before:
                    logger.info("Dump files regenerated successfully")
                    return True
        logger.warning("Dump regeneration timed out (3 min)")
        return False
    except Exception as e:
        logger.error(f"Dump trigger failed: {e}")
        return False


async def _load_categories_with_dump():
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = 'loading', message = 'Regenerating category/stream dumps...' WHERE id = 1"
        )
        await db.commit()
    finally:
        await db.close()

    await _trigger_dump_regen()
    await _load_categories()


async def _load_categories():
    import json as _json

    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = 'loading', message = 'Fetching categories from Dispatcharr...' WHERE id = 1"
        )
        await db.commit()

        req_headers = {}
        if DISPATCHARR_API_KEY:
            req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch categories with account associations from live API
            cat_resp = await client.get(
                f"{DISPATCHARR_URL}/api/vod/categories/?category_type=movie",
                headers=req_headers,
            )
            if cat_resp.status_code != 200:
                raise Exception(f"Categories API returned {cat_resp.status_code}")

            api_categories = cat_resp.json()
            if isinstance(api_categories, dict) and "results" in api_categories:
                api_categories = api_categories["results"]

            # Fetch M3U account names
            acct_resp = await client.get(
                f"{DISPATCHARR_URL}/api/m3u/accounts/",
                headers=req_headers,
            )
            account_name_map = {}
            if acct_resp.status_code == 200:
                acct_data = acct_resp.json()
                acct_list = acct_data.get("results", acct_data) if isinstance(acct_data, dict) else acct_data
                if isinstance(acct_list, list):
                    for a in acct_list:
                        account_name_map[a["id"]] = a["name"]

        # Load movie-category mappings from dump file (if available)
        cat_movie_map = {}
        if os.path.exists(CATEGORY_MAPPING_FILE):
            with open(CATEGORY_MAPPING_FILE) as f:
                dump_cats = _json.load(f)
            for dc in dump_cats:
                cat_movie_map[dc["id"]] = dc.get("movie_ids", [])
            logger.info(f"Loaded movie mappings from {CATEGORY_MAPPING_FILE}: {len(cat_movie_map)} categories")

        await db.execute("DELETE FROM vod_category_accounts")
        await db.execute("DELETE FROM movie_categories")
        await db.execute("DELETE FROM vod_categories")
        await db.execute("DELETE FROM m3u_accounts")

        account_ids = set()
        skipped_disabled = 0
        for cat in api_categories:
            cat_type = cat.get("category_type", "movie")
            if cat_type != "movie":
                continue

            acct_entries = cat.get("m3u_accounts", [])
            enabled_accts = [
                a for a in acct_entries
                if isinstance(a, dict) and a.get("enabled", True)
            ]
            if acct_entries and not enabled_accts:
                skipped_disabled += 1
                continue

            movie_ids = cat_movie_map.get(cat["id"], [])
            await db.execute(
                "INSERT OR REPLACE INTO vod_categories (id, name, category_type, movie_count) VALUES (?, ?, 'movie', ?)",
                (cat["id"], cat["name"], len(movie_ids)),
            )
            for mid in movie_ids:
                await db.execute(
                    "INSERT OR IGNORE INTO movie_categories (movie_id, category_id) VALUES (?, ?)",
                    (mid, cat["id"]),
                )

            for acct in enabled_accts:
                acct_id = acct.get("m3u_account") if isinstance(acct, dict) else acct
                if acct_id:
                    account_ids.add(acct_id)
                    await db.execute(
                        "INSERT OR IGNORE INTO vod_category_accounts (category_id, account_id) VALUES (?, ?)",
                        (cat["id"], acct_id),
                    )

        if skipped_disabled:
            logger.info(f"Skipped {skipped_disabled} categories disabled on all accounts")

        for acct_id in account_ids:
            name = account_name_map.get(acct_id, f"Account {acct_id}")
            await db.execute(
                "INSERT OR REPLACE INTO m3u_accounts (id, name) VALUES (?, ?)",
                (acct_id, name),
            )

        await db.commit()

        count_row = await db.execute("SELECT COUNT(*) as cnt FROM vod_categories")
        cat_count = (await count_row.fetchone())["cnt"]
        map_row = await db.execute("SELECT COUNT(*) as cnt FROM movie_categories")
        map_count = (await map_row.fetchone())["cnt"]
        acct_row = await db.execute("SELECT COUNT(*) as cnt FROM m3u_accounts")
        acct_count = (await acct_row.fetchone())["cnt"]

        await db.execute(
            "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
            (f"Loaded {cat_count} categories, {map_count} mappings, {acct_count} providers",),
        )
        await db.commit()
        logger.info(f"Loaded {cat_count} categories, {map_count} movie mappings, {acct_count} providers")
    except Exception as e:
        logger.error(f"Failed to load categories: {e}")
        await db.execute(
            "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
            (str(e)[:500],),
        )
        await db.commit()
    finally:
        await db.close()


@router.get("/movies")
async def list_movies(
    request: Request,
    genre: str = "",
    category_id: int = 0,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "rating",
    sort_order: str = "desc",
    search: str = "",
    activated_only: bool = False,
    language: str = "",
):
    account_ids = [int(x) for x in request.query_params.getlist("account_id") if x]

    db = await get_db()
    try:
        sort_col = {"rating": "rating", "year": "year", "name": "name"}.get(sort_by, "rating")
        order = "DESC" if sort_order == "desc" else "ASC"
        offset = (page - 1) * page_size

        conditions = ["m.name != ''", "m.dead = 0", "m.stream_dead = 0"]
        params = []

        if activated_only:
            conditions.append("m.activated = 1")

        if account_ids:
            ph = ",".join("?" for _ in account_ids)
            conditions.append(f"m.account_id IN ({ph})")
            params.extend(account_ids)

        if category_id:
            conditions.append("m.id IN (SELECT movie_id FROM movie_categories WHERE category_id = ?)")
            params.append(category_id)

        if genre:
            conditions.append("m.genre LIKE ?")
            params.append(f"%{genre}%")

        if search:
            conditions.append("m.name LIKE ?")
            params.append(f"%{search}%")

        if language:
            conditions.append("m.language = ?")
            params.append(language)

        where = " AND ".join(conditions)

        count_row = await db.execute(
            f"SELECT COUNT(*) as cnt FROM movies m WHERE {where}", params
        )
        count = (await count_row.fetchone())["cnt"]

        rows = await db.execute(
            f"SELECT m.id, m.name, m.year, m.rating, m.genre, m.tmdb_id, m.poster_url, "
            f"m.activated, m.account_id, m.account_name, m.trailer_key, m.language "
            f"FROM movies m WHERE {where} ORDER BY m.{sort_col} {order} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        movies = [dict(r) for r in await rows.fetchall()]
        return {"count": count, "page": page, "page_size": page_size, "results": movies}
    finally:
        await db.close()


HEADER_FETCH_SIZE = 20 * 1024 * 1024  # 20MB head cache
TAIL_FETCH_SIZE = 20 * 1024 * 1024   # 20MB tail cache


@router.get("/proxy-log")
async def proxy_log():
    return get_proxy_log()


@router.post("/movies/activate")
async def activate_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        # Refresh stream_ids from current mapping file before activation
        # Picks the stream_id matching the movie's current provider (account_id)
        mapping = load_stream_mapping()
        updated_ids = 0
        for movie_id in movie_ids:
            if movie_id in mapping:
                entries = mapping[movie_id]
                row = await db.execute("SELECT account_id FROM movies WHERE id = ?", (movie_id,))
                movie = await row.fetchone()
                current_account_id = movie["account_id"] if movie else None

                info = pick_stream_for_account(entries, current_account_id)
                if not info:
                    continue

                stream_id = info.get("stream_id")
                ext = info.get("ext", "mkv")
                content_type = "video/x-matroska" if ext == "mkv" else "video/mp4"
                account_id = info.get("account_id")
                account_name = info.get("account_name", "")

                result = await db.execute(
                    "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ?, "
                    "header_data = NULL, header_size = 0, tail_data = NULL, tail_size = 0, tail_offset = 0 "
                    "WHERE id = ? AND (stream_id IS NULL OR stream_id != ?)",
                    (stream_id, content_type, account_id, account_name, movie_id, stream_id),
                )
                if result.rowcount > 0:
                    updated_ids += 1

        if updated_ids > 0:
            await db.commit()
            logger.info(f"Refreshed stream_ids for {updated_ids} movies before activation")

        placeholders = ",".join("?" for _ in movie_ids)
        await db.execute(
            f"UPDATE movies SET activated = 1, stream_dead = 0, stream_dead_count = 0 WHERE id IN ({placeholders})",
            movie_ids,
        )
        await db.commit()
        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total = (await count_row.fetchone())["cnt"]

        needs_header = await db.execute(
            f"SELECT id, uuid, stream_id, account_id, content_type, name, year FROM movies WHERE id IN ({placeholders}) AND (header_data IS NULL OR header_size = 0) AND stream_id IS NOT NULL",
            movie_ids,
        )
        to_fetch = [dict(r) for r in await needs_header.fetchall()]
        if to_fetch:
            asyncio.create_task(_fetch_headers(to_fetch))

        return {"status": "ok", "activated": len(movie_ids), "total_activated": total, "headers_queued": len(to_fetch), "stream_ids_refreshed": updated_ids}
    finally:
        await db.close()


async def _refresh_activated_stream_ids():
    """Periodically refresh stream_ids for activated movies against current mapping file."""
    mapping = load_stream_mapping()
    if not mapping:
        logger.warning("Cannot refresh stream_ids: no mapping file")
        return 0

    db = await get_db()
    try:
        rows = await db.execute("SELECT id, account_id FROM movies WHERE activated = 1")
        activated = [dict(r) for r in await rows.fetchall()]

        updated = 0
        for movie in activated:
            movie_id = movie["id"]
            if movie_id in mapping:
                entries = mapping[movie_id]
                info = pick_stream_for_account(entries, movie["account_id"])
                if not info:
                    continue

                stream_id = info.get("stream_id")
                ext = info.get("ext", "mkv")
                content_type = "video/x-matroska" if ext == "mkv" else "video/mp4"
                account_id = info.get("account_id")
                account_name = info.get("account_name", "")

                result = await db.execute(
                    "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ? "
                    "WHERE id = ? AND stream_id != ?",
                    (stream_id, content_type, account_id, account_name, movie_id, stream_id),
                )
                if result.rowcount > 0:
                    updated += 1
                    logger.info(f"Refreshed stream_id for activated movie {movie_id}")

        if updated > 0:
            await db.commit()
            logger.info(f"Refreshed stream_ids for {updated} activated movies")
        return updated
    finally:
        await db.close()


CATALOG_VALIDATE_BATCH = 500
CATALOG_VALIDATE_INTERVAL = 4 * 3600  # every 4 hours
CATALOG_VALIDATE_DELAY = 0.2  # seconds between probes
RESURRECTION_INTERVAL = 8 * 3600  # every 8 hours
RESURRECTION_BATCH = 50


async def _deactivate_dead_movie(movie_id: int, name: str, year: int | None, reason: str):
    """Immediately deactivate a movie and remove from Plex when stream is dead."""
    db = await get_db()
    try:
        await db.execute("UPDATE movies SET activated = 0 WHERE id = ?", (movie_id,))
        await db.commit()

        movie_info = {"name": name, "year": year}
        _remove_strm_folders([movie_info])

        strm_count = _count_strm_folders()
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (strm_count,))
        await db.commit()
    finally:
        await db.close()

    plex_removed = await _plex_remove_movies([movie_id])
    logger.warning("Auto-deactivated movie %d (%s) — %s. Plex removed: %d", movie_id, name, reason, plex_removed)


async def _validate_catalog_batch():
    """Validate a batch of activated movies by probing their streams.
    Cycles through the entire activated catalog over time without hammering the provider."""
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT id, uuid, stream_id, account_id, content_type, name, year FROM movies "
            "WHERE activated = 1 AND stream_dead = 0 AND stream_id IS NOT NULL "
            "ORDER BY COALESCE(stream_dead_checked_at, '2000-01-01') ASC "
            "LIMIT ?",
            (CATALOG_VALIDATE_BATCH,),
        )
        batch = [dict(r) for r in await rows.fetchall()]
    finally:
        await db.close()

    if not batch:
        return

    logger.info("Catalog validation: checking %d activated movies", len(batch))
    now = datetime.now(timezone.utc).isoformat()

    for movie in batch:
        movie_id = movie["id"]
        uuid = movie["uuid"]
        stream_id = movie["stream_id"]

        account_id = movie.get("account_id")
        content_type = movie.get("content_type", "video/x-matroska")
        ext = "mp4" if content_type == "video/mp4" else "mkv"
        xc_path = get_xc_url(movie_id, ext)

        if xc_path:
            upstream_url = f"{DISPATCHARR_URL}{xc_path}"
            req_headers = {"Range": "bytes=0-0"}
        else:
            upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
            req_headers = {"Range": "bytes=0-0"}
            if DISPATCHARR_API_KEY:
                req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10), follow_redirects=True) as client:
                resp = await client.get(upstream_url, headers=req_headers)

            db2 = await get_db()
            try:
                if resp.status_code >= 500:
                    await db2.execute(
                        "UPDATE movies SET stream_dead_count = COALESCE(stream_dead_count, 0) + 1, stream_dead_checked_at = ? WHERE id = ?",
                        (now, movie_id),
                    )
                    row = await db2.execute("SELECT stream_dead_count FROM movies WHERE id = ?", (movie_id,))
                    result = await row.fetchone()
                    count = result["stream_dead_count"] if result else 1

                    if count >= 3:
                        await _deactivate_dead_movie(movie_id, movie["name"], movie.get("year"), f"HTTP {resp.status_code}, strike {count}/3")
                        await db2.execute(
                            "UPDATE movies SET stream_dead = 1, stream_dead_checked_at = ? WHERE id = ?",
                            (now, movie_id),
                        )
                        logger.warning("Catalog validation: movie %d marked dead after %d strikes (hidden from browse)", movie_id, count)
                    else:
                        logger.info("Catalog validation: movie %d strike %d/3 (HTTP %d)", movie_id, count, resp.status_code)
                    await db2.commit()
                else:
                    await db2.execute(
                        "UPDATE movies SET stream_dead_count = 0, stream_dead_checked_at = ? WHERE id = ?",
                        (now, movie_id),
                    )
                    await db2.commit()
            finally:
                await db2.close()
        except Exception as e:
            logger.warning("Catalog validation: error checking movie %d: %s", movie_id, e)

        await asyncio.sleep(CATALOG_VALIDATE_DELAY)

    logger.info("Catalog validation batch complete: %d movies checked", len(batch))


async def _validate_catalog_full():
    """Full sweep of all activated movies. Runs in batches of 500 with short pauses between batches."""
    db = await get_db()
    try:
        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1 AND stream_dead = 0 AND stream_id IS NOT NULL")
        total = (await count_row.fetchone())["cnt"]
    finally:
        await db.close()

    logger.info("Full catalog validation started: %d activated movies to check", total)
    checked = 0
    deactivated = 0
    batch_num = 0

    while True:
        db = await get_db()
        try:
            rows = await db.execute(
                "SELECT id, uuid, stream_id, account_id, content_type, name, year FROM movies "
                "WHERE activated = 1 AND stream_dead = 0 AND stream_id IS NOT NULL "
                "ORDER BY COALESCE(stream_dead_checked_at, '2000-01-01') ASC "
                "LIMIT 500",
            )
            batch = [dict(r) for r in await rows.fetchall()]
        finally:
            await db.close()

        if not batch:
            break

        batch_num += 1
        now = datetime.now(timezone.utc).isoformat()

        for movie in batch:
            movie_id = movie["id"]
            uuid = movie["uuid"]
            stream_id = movie["stream_id"]

            account_id = movie.get("account_id")
            content_type = movie.get("content_type", "video/x-matroska")
            ext = "mp4" if content_type == "video/mp4" else "mkv"
            xc_path = get_xc_url(movie_id, ext)

            if xc_path:
                upstream_url = f"{DISPATCHARR_URL}{xc_path}"
                req_headers = {"Range": "bytes=0-0"}
            else:
                upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
                req_headers = {"Range": "bytes=0-0"}
                if DISPATCHARR_API_KEY:
                    req_headers["X-API-Key"] = DISPATCHARR_API_KEY

            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10), follow_redirects=True) as client:
                    resp = await client.get(upstream_url, headers=req_headers)

                db2 = await get_db()
                try:
                    if resp.status_code >= 500:
                        await db2.execute(
                            "UPDATE movies SET stream_dead_count = COALESCE(stream_dead_count, 0) + 1, stream_dead_checked_at = ? WHERE id = ?",
                            (now, movie_id),
                        )
                        row = await db2.execute("SELECT stream_dead_count FROM movies WHERE id = ?", (movie_id,))
                        result = await row.fetchone()
                        count = result["stream_dead_count"] if result else 1

                        if count >= 3:
                            await _deactivate_dead_movie(movie_id, movie["name"], movie.get("year"), f"HTTP {resp.status_code}, strike {count}/3")
                            deactivated += 1
                            await db2.execute(
                                "UPDATE movies SET stream_dead = 1, stream_dead_checked_at = ? WHERE id = ?",
                                (now, movie_id),
                            )
                            logger.warning("Full validation: movie %d marked dead after %d strikes (hidden from browse)", movie_id, count)
                        else:
                            logger.info("Full validation: movie %d strike %d/3 (HTTP %d)", movie_id, count, resp.status_code)
                    else:
                        await db2.execute(
                            "UPDATE movies SET stream_dead_count = 0, stream_dead_checked_at = ? WHERE id = ?",
                            (now, movie_id),
                        )
                    await db2.commit()
                finally:
                    await db2.close()
            except Exception as e:
                logger.warning("Full validation: error checking movie %d: %s", movie_id, e)

            await asyncio.sleep(CATALOG_VALIDATE_DELAY)

        checked += len(batch)
        logger.info("Full validation progress: %d/%d checked, %d deactivated (batch %d)", checked, total, deactivated, batch_num)
        await asyncio.sleep(5)

    logger.info("Full catalog validation complete: %d checked, %d deactivated", checked, deactivated)


async def _resurrect_dead_streams():
    """Re-check movies marked stream_dead to see if the provider restored them."""
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT id, uuid, stream_id, account_id, content_type FROM movies "
            "WHERE stream_dead = 1 AND dead = 0 AND stream_id IS NOT NULL "
            "ORDER BY COALESCE(stream_dead_checked_at, '2000-01-01') ASC "
            "LIMIT ?",
            (RESURRECTION_BATCH,),
        )
        batch = [dict(r) for r in await rows.fetchall()]
    finally:
        await db.close()

    if not batch:
        return

    logger.info("Resurrection check: testing %d dead streams", len(batch))
    now = datetime.now(timezone.utc).isoformat()
    resurrected = 0

    for movie in batch:
        movie_id = movie["id"]
        uuid = movie["uuid"]
        stream_id = movie["stream_id"]

        account_id = movie.get("account_id")
        content_type = movie.get("content_type", "video/x-matroska")
        ext = "mp4" if content_type == "video/mp4" else "mkv"
        xc_path = get_xc_url(movie_id, ext)

        if xc_path:
            upstream_url = f"{DISPATCHARR_URL}{xc_path}"
            req_headers = {"Range": "bytes=0-0"}
        else:
            upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
            req_headers = {"Range": "bytes=0-0"}
            if DISPATCHARR_API_KEY:
                req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10), follow_redirects=True) as client:
                resp = await client.get(upstream_url, headers=req_headers)

            db2 = await get_db()
            try:
                if resp.status_code < 400:
                    await db2.execute(
                        "UPDATE movies SET stream_dead = 0, stream_dead_count = 0, stream_dead_checked_at = ? WHERE id = ?",
                        (now, movie_id),
                    )
                    resurrected += 1
                    logger.info("Resurrection: movie %d stream is alive again!", movie_id)
                else:
                    await db2.execute(
                        "UPDATE movies SET stream_dead_checked_at = ? WHERE id = ?",
                        (now, movie_id),
                    )
                await db2.commit()
            finally:
                await db2.close()
        except Exception as e:
            logger.warning("Resurrection check: error checking movie %d: %s", movie_id, e)

        await asyncio.sleep(CATALOG_VALIDATE_DELAY)

    logger.info("Resurrection check complete: %d/%d streams came back", resurrected, len(batch))


async def catalog_validation_scheduler():
    """Background task: validate activated streams every 4 hours."""
    logger.info("Catalog validation scheduler started (interval: 4h, batch: %d)", CATALOG_VALIDATE_BATCH)
    while True:
        await asyncio.sleep(CATALOG_VALIDATE_INTERVAL)
        try:
            await _validate_catalog_batch()
        except Exception as e:
            logger.error("Catalog validation scheduler error: %s", e)


async def resurrection_scheduler():
    """Background task: re-check dead streams every 8 hours."""
    logger.info("Resurrection scheduler started (interval: 8h, batch: %d)", RESURRECTION_BATCH)
    while True:
        await asyncio.sleep(RESURRECTION_INTERVAL)
        try:
            await _resurrect_dead_streams()
        except Exception as e:
            logger.error("Resurrection scheduler error: %s", e)


async def _fetch_headers(movies: list[dict]):
    for movie in movies:
        try:
            uuid = movie["uuid"]
            stream_id = movie["stream_id"]
            movie_id = movie["id"]
            account_id = movie.get("account_id")
            content_type = movie.get("content_type", "video/x-matroska")
            ext = "mp4" if content_type == "video/mp4" else "mkv"

            xc_path = get_xc_url(movie_id, ext)
            if xc_path:
                from proxy import _get_cached_session, _resolve_session, _cache_session, clear_movie_session
                from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
                base_xc_url = f"{DISPATCHARR_URL}{xc_path}"
                cached = _get_cached_session(movie_id)
                if cached:
                    session_id, resolved_url = cached
                    parsed = urlparse(resolved_url)
                    qs = parse_qs(parsed.query)
                    qs["session_id"] = [session_id]
                    new_query = urlencode(qs, doseq=True)
                    upstream_url = urlunparse(parsed._replace(query=new_query))
                else:
                    result = await _resolve_session(base_xc_url, movie_id)
                    if result:
                        _, resolved_url = result
                        upstream_url = resolved_url
                    else:
                        upstream_url = base_xc_url
                req_headers = {"Range": f"bytes=0-{HEADER_FETCH_SIZE - 1}"}
            else:
                upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
                req_headers = {"Range": f"bytes=0-{HEADER_FETCH_SIZE - 1}"}
                if DISPATCHARR_API_KEY:
                    req_headers["X-API-Key"] = DISPATCHARR_API_KEY

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
                follow_redirects=True,
            ) as client:
                resp = await client.get(upstream_url, headers=req_headers)
                if resp.status_code >= 400:
                    logger.warning("Header fetch failed for movie %d: HTTP %d", movie_id, resp.status_code)
                    _log_event("error", movie_id, f"Header fetch failed: HTTP {resp.status_code}")

                    if resp.status_code >= 500:
                        db_dead = await get_db()
                        try:
                            await db_dead.execute(
                                "UPDATE movies SET stream_dead_count = COALESCE(stream_dead_count, 0) + 1 WHERE id = ?",
                                (movie_id,),
                            )
                            row = await db_dead.execute("SELECT stream_dead_count FROM movies WHERE id = ?", (movie_id,))
                            result = await row.fetchone()
                            count = result["stream_dead_count"] if result else 1

                            if count >= 3:
                                await _deactivate_dead_movie(movie_id, movie.get("name", ""), movie.get("year"), f"header fetch HTTP {resp.status_code}, strike {count}/3")
                                await db_dead.execute(
                                    "UPDATE movies SET stream_dead = 1, stream_dead_checked_at = ? WHERE id = ?",
                                    (datetime.now(timezone.utc).isoformat(), movie_id),
                                )
                                logger.warning("Marked movie %d stream as dead after %d strikes (HTTP %d)", movie_id, count, resp.status_code)
                            else:
                                _log_event("warn", movie_id, f"Header fetch error: HTTP {resp.status_code}, strike {count}/3")
                            await db_dead.commit()
                        finally:
                            await db_dead.close()
                    continue

                header_bytes = resp.content

                file_size = None
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    total = cr.split("/")[-1]
                    if total.isdigit():
                        file_size = int(total)

                tail_bytes = None
                tail_offset = 0
                if file_size and file_size > TAIL_FETCH_SIZE:
                    tail_offset = file_size - TAIL_FETCH_SIZE
                    tail_headers = {"Range": f"bytes={tail_offset}-{file_size - 1}"}
                    if not xc_path and DISPATCHARR_API_KEY:
                        tail_headers["X-API-Key"] = DISPATCHARR_API_KEY
                    try:
                        tail_resp = await client.get(upstream_url, headers=tail_headers)
                        if tail_resp.status_code < 400:
                            tail_bytes = tail_resp.content
                            logger.info("Tail fetched: movie %d, %d bytes from offset %d", movie_id, len(tail_bytes), tail_offset)
                        else:
                            logger.warning("Tail fetch failed for movie %d: HTTP %d", movie_id, tail_resp.status_code)
                    except Exception as te:
                        logger.warning("Tail fetch error for movie %d: %s", movie_id, te)

                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE movies SET header_data = ?, header_size = ?, "
                        "tail_data = ?, tail_size = ?, tail_offset = ?, "
                        "file_size = CASE WHEN file_size IS NULL OR file_size = 0 THEN ? ELSE file_size END WHERE id = ?",
                        (header_bytes, len(header_bytes), tail_bytes, len(tail_bytes) if tail_bytes else 0, tail_offset, file_size, movie_id),
                    )
                    await db.commit()
                    logger.info("Cached: movie %d, head=%d bytes, tail=%d bytes, file_size=%s",
                                movie_id, len(header_bytes), len(tail_bytes) if tail_bytes else 0, file_size)
                    _log_event("info", movie_id, "Header+tail cached",
                               head_bytes=len(header_bytes), tail_bytes=len(tail_bytes) if tail_bytes else 0, file_size=file_size)
                finally:
                    await db.close()

        except Exception as e:
            logger.error("Header fetch error for movie %d: %s", movie.get("id"), e)
            _log_event("error", movie.get("id"), f"Header fetch exception: {e}")


@router.post("/movies/deactivate")
async def deactivate_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, name, year FROM movies WHERE id IN ({placeholders})",
            movie_ids,
        )
        movies = await rows.fetchall()
        await db.execute(
            f"UPDATE movies SET activated = 0 WHERE id IN ({placeholders})",
            movie_ids,
        )
        await db.commit()

        removed = _remove_strm_folders(movies)

        strm_count = _count_strm_folders()
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (strm_count,))
        await db.commit()

        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total = (await count_row.fetchone())["cnt"]

        plex_removed = await _plex_remove_movies(movie_ids)

        return {"status": "ok", "deactivated": len(movie_ids), "total_activated": total, "strm_removed": removed, "plex_removed": plex_removed}
    finally:
        await db.close()


@router.post("/movies/deactivate-all")
async def deactivate_all():
    db = await get_db()
    try:
        rows = await db.execute("SELECT id FROM movies WHERE activated = 1")
        active_ids = [r["id"] for r in await rows.fetchall()]

        await db.execute("UPDATE movies SET activated = 0")
        await db.commit()

        removed = 0
        if os.path.exists(STRM_OUTPUT_DIR):
            for item in os.listdir(STRM_OUTPUT_DIR):
                full = os.path.join(STRM_OUTPUT_DIR, item)
                if os.path.isdir(full):
                    shutil.rmtree(full)
                    removed += 1
        await db.execute("UPDATE sync_state SET active_strm_count = 0 WHERE id = 1")
        await db.commit()

        plex_removed = await _plex_remove_movies(active_ids) if active_ids else 0

        return {"status": "ok", "message": f"All movies deactivated, {removed} STRM folders removed, {plex_removed} removed from Plex"}
    finally:
        await db.close()


async def _plex_remove_movies(movie_ids: list[int]):
    if not PLEX_URL or not PLEX_TOKEN:
        logger.info("Plex URL/token not configured, skipping removal")
        return 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{PLEX_URL}/library/sections/{PLEX_LIBRARY_ID}/all",
                params={"X-Plex-Token": PLEX_TOKEN},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning("Plex library query failed: %d", resp.status_code)
                return 0

            items = resp.json().get("MediaContainer", {}).get("Metadata", [])
            id_set = set(movie_ids)
            removed = 0

            for item in items:
                parts = item.get("Media", [{}])[0].get("Part", [])
                for part in parts:
                    filename = part.get("file", "")
                    m = re.search(r'\[(\d+)\]\.mp4$', filename)
                    if m and int(m.group(1)) in id_set:
                        rating_key = item["ratingKey"]
                        del_resp = await client.delete(
                            f"{PLEX_URL}/library/metadata/{rating_key}",
                            params={"X-Plex-Token": PLEX_TOKEN},
                        )
                        if del_resp.status_code == 200:
                            removed += 1
                            logger.info("Plex: deleted %s (key %s)", item.get("title"), rating_key)
                        break

            logger.info("Plex cleanup: removed %d items", removed)
            return removed
    except Exception as e:
        logger.warning("Plex removal failed: %s", e)
        return 0


def _movie_folder_name(movie) -> str:
    name = movie["name"]
    year = movie["year"]
    clean = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', name).strip()
    clean = re.sub(r'\s*\(\d{4}\)\s*$', '', clean).strip()
    if year:
        return sanitize_filename(f"{clean} ({year})")
    return sanitize_filename(clean)


def _remove_strm_folders(movies) -> int:
    removed = 0
    for m in movies:
        folder = os.path.join(STRM_OUTPUT_DIR, _movie_folder_name(m))
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            removed += 1
    return removed


def _count_strm_folders() -> int:
    if not os.path.exists(STRM_OUTPUT_DIR):
        return 0
    return sum(1 for d in os.listdir(STRM_OUTPUT_DIR) if os.path.isdir(os.path.join(STRM_OUTPUT_DIR, d)))


@router.get("/movies/activated-count")
async def activated_count():
    db = await get_db()
    try:
        row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        return {"count": (await row.fetchone())["cnt"]}
    finally:
        await db.close()


@router.get("/genres")
async def list_genres(category_id: int = 0):
    db = await get_db()
    try:
        if category_id:
            rows = await db.execute(
                "SELECT m.genre FROM movies m "
                "JOIN movie_categories mc ON m.id = mc.movie_id "
                "WHERE mc.category_id = ? AND m.genre != '' AND m.genre IS NOT NULL AND m.name != ''",
                (category_id,),
            )
        else:
            rows = await db.execute(
                "SELECT genre FROM movies WHERE genre != '' AND genre IS NOT NULL AND name != ''"
            )
        all_genres = await rows.fetchall()
        genre_counts = {}
        for row in all_genres:
            for g in row["genre"].split(","):
                g = g.strip()
                if g:
                    genre_counts[g] = genre_counts.get(g, 0) + 1

        sorted_genres = sorted(genre_counts.items(), key=lambda x: -x[1])
        return [{"name": g, "count": c} for g, c in sorted_genres]
    finally:
        await db.close()


@router.get("/languages")
async def list_languages():
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT language, COUNT(*) as cnt FROM movies WHERE language != '' AND name != '' GROUP BY language ORDER BY cnt DESC"
        )
        langs = [dict(r) for r in await rows.fetchall()]
        unknown_row = await db.execute(
            "SELECT COUNT(*) as cnt FROM movies WHERE (language = '' OR language IS NULL) AND name != ''"
        )
        unknown = (await unknown_row.fetchone())["cnt"]
        return {"languages": langs, "unknown": unknown}
    finally:
        await db.close()


LANG_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "hi": "Hindi",
    "ar": "Arabic", "tr": "Turkish", "pl": "Polish", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "el": "Greek",
    "he": "Hebrew", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
    "ms": "Malay", "tl": "Tagalog", "ro": "Romanian", "hu": "Hungarian",
    "cs": "Czech", "sk": "Slovak", "bg": "Bulgarian", "uk": "Ukrainian",
    "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian", "lt": "Lithuanian",
    "lv": "Latvian", "et": "Estonian", "ka": "Georgian", "hy": "Armenian",
    "fa": "Persian", "ur": "Urdu", "bn": "Bengali", "ta": "Tamil",
    "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "mr": "Marathi",
    "gu": "Gujarati", "pa": "Punjabi", "cn": "Cantonese",
}


@router.post("/movies/detect-language")
async def detect_language(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        return JSONResponse(status_code=400, content={"error": "TMDB API key not configured"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, tmdb_id FROM movies WHERE id IN ({placeholders}) AND tmdb_id IS NOT NULL AND tmdb_id != ''",
            movie_ids,
        )
        movies = await rows.fetchall()

        if not movies:
            return {"detected": 0, "skipped": len(movie_ids), "message": "No movies with TMDB IDs in selection"}

        detected = 0
        skipped = 0
        results = []

        async with httpx.AsyncClient(timeout=10.0) as client:
            for movie in movies:
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
                        skipped += 1
                        continue

                    tmdb_data = resp.json()
                    lang = tmdb_data.get("original_language", "")
                    lang_name = LANG_NAMES.get(lang, lang)

                    await db.execute(
                        "UPDATE movies SET language = ? WHERE id = ?",
                        (lang, movie["id"]),
                    )
                    detected += 1
                    results.append({"id": movie["id"], "language": lang, "language_name": lang_name})
                    await asyncio.sleep(0.15)
                except Exception as e:
                    logger.warning(f"TMDB language detect failed for movie {movie['id']}: {e}")
                    skipped += 1

        await db.commit()
        no_tmdb = len(movie_ids) - len(movies)
        return {
            "detected": detected,
            "skipped": skipped,
            "no_tmdb_id": no_tmdb,
            "results": results,
        }
    finally:
        await db.close()


@router.post("/movies/detect-language-all")
async def detect_language_all():
    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        return JSONResponse(status_code=400, content={"error": "TMDB API key not configured"})
    asyncio.create_task(_bulk_detect_languages())
    return {"status": "started", "message": "Bulk language detection started in background"}


async def _bulk_detect_languages():
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT id, tmdb_id FROM movies WHERE tmdb_id IS NOT NULL AND tmdb_id != '' "
            "AND (language = '' OR language IS NULL) AND name != ''"
        )
        movies = await rows.fetchall()
        total = len(movies)
        if not total:
            await db.execute(
                "UPDATE sync_state SET lang_status = 'All languages detected' WHERE id = 1"
            )
            await db.commit()
            return

        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Detecting languages: 0/{total}...",),
        )
        await db.commit()

        detected = 0
        skipped = 0
        sem = asyncio.Semaphore(4)

        async def fetch_lang(client, movie):
            nonlocal detected, skipped
            async with sem:
                if is_cancelled():
                    return
                try:
                    url = f"https://api.themoviedb.org/3/movie/{movie['tmdb_id']}"
                    headers = {}
                    params = {}
                    if TMDB_READ_TOKEN:
                        headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
                    else:
                        params["api_key"] = TMDB_API_KEY
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "2"))
                        await asyncio.sleep(retry_after)
                        resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code != 200:
                        skipped += 1
                        return
                    lang = resp.json().get("original_language", "")
                    await db.execute("UPDATE movies SET language = ? WHERE id = ?", (lang, movie["id"]))
                    detected += 1
                    await asyncio.sleep(0.25)
                except Exception:
                    skipped += 1

        async with httpx.AsyncClient(timeout=10.0) as client:
            batch_size = 40
            for i in range(0, total, batch_size):
                if is_cancelled():
                    break
                batch = movies[i:i + batch_size]
                await asyncio.gather(*[fetch_lang(client, m) for m in batch])
                await db.commit()
                await db.execute(
                    "UPDATE sync_state SET lang_status = ? WHERE id = 1",
                    (f"Detecting languages: {detected + skipped}/{total} ({detected} detected)...",),
                )
                await db.commit()

        status_msg = "cancelled" if is_cancelled() else "complete"
        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Language detection {status_msg}: {detected} detected, {skipped} skipped",),
        )
        await db.commit()
        logger.info(f"Bulk language detection {status_msg}: {detected} detected, {skipped} skipped out of {total}")
    except Exception as e:
        logger.error(f"Bulk language detection failed: {e}")
        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Error: {str(e)[:200]}",),
        )
        await db.commit()
    finally:
        await db.close()


@router.get("/catalog/summary")
async def catalog_summary():
    db = await get_db()
    try:
        cat_rows = await db.execute(
            "SELECT c.id, c.name, COUNT(mc.movie_id) as movie_count, "
            "SUM(CASE WHEN m.activated = 1 THEN 1 ELSE 0 END) as activated_count "
            "FROM vod_categories c "
            "LEFT JOIN movie_categories mc ON c.id = mc.category_id "
            "LEFT JOIN movies m ON mc.movie_id = m.id AND m.name != '' "
            "GROUP BY c.id ORDER BY c.name"
        )
        categories = [dict(r) for r in await cat_rows.fetchall()]

        prov_rows = await db.execute(
            "SELECT account_name, COUNT(*) as movie_count, "
            "SUM(CASE WHEN activated = 1 THEN 1 ELSE 0 END) as activated_count "
            "FROM movies WHERE name != '' AND account_name != '' "
            "GROUP BY account_name ORDER BY movie_count DESC"
        )
        providers = [dict(r) for r in await prov_rows.fetchall()]

        lang_rows = await db.execute(
            "SELECT language, COUNT(*) as cnt FROM movies "
            "WHERE name != '' AND language != '' AND language IS NOT NULL GROUP BY language ORDER BY cnt DESC"
        )
        languages = [dict(r) for r in await lang_rows.fetchall()]

        unknown_row = await db.execute(
            "SELECT COUNT(*) as cnt FROM movies WHERE name != '' AND (language = '' OR language IS NULL)"
        )
        lang_unknown = (await unknown_row.fetchone())["cnt"]

        total_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE name != ''")
        total = (await total_row.fetchone())["cnt"]
        active_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        activated = (await active_row.fetchone())["cnt"]

        return {
            "total": total,
            "activated": activated,
            "categories": categories,
            "providers": providers,
            "languages": languages,
            "language_unknown": lang_unknown,
        }
    finally:
        await db.close()


@router.get("/filters")
async def get_filters():
    db = await get_db()
    try:
        rows = await db.execute("SELECT * FROM filter_configs ORDER BY genre")
        return [dict(r) for r in await rows.fetchall()]
    finally:
        await db.close()


@router.post("/filters")
async def add_filter(request: Request):
    data = await request.json()
    genre = data.get("genre", "").strip()
    if not genre:
        return JSONResponse(status_code=400, content={"error": "genre is required"})

    db = await get_db()
    try:
        existing = await db.execute("SELECT id FROM filter_configs WHERE genre = ?", (genre,))
        if await existing.fetchone():
            return JSONResponse(status_code=409, content={"error": f"Filter for '{genre}' already exists"})

        await db.execute(
            "INSERT INTO filter_configs (genre, limit_count, sort_by, sort_order) VALUES (?, ?, ?, ?)",
            (genre, data.get("limit_count", 30), data.get("sort_by", "rating"), data.get("sort_order", "desc")),
        )
        await db.commit()
        return {"status": "ok", "genre": genre}
    finally:
        await db.close()


@router.put("/filters/{filter_id}")
async def update_filter(filter_id: int, request: Request):
    data = await request.json()
    db = await get_db()
    try:
        sets = []
        params = []
        for key in ("genre", "limit_count", "sort_by", "sort_order", "enabled"):
            if key in data:
                sets.append(f"{key} = ?")
                params.append(data[key])

        if not sets:
            return JSONResponse(status_code=400, content={"error": "No fields to update"})

        sets.append("updated_at = datetime('now')")
        params.append(filter_id)
        await db.execute(f"UPDATE filter_configs SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.delete("/filters/{filter_id}")
async def delete_filter(filter_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM filter_configs WHERE id = ?", (filter_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@router.post("/sync/clear-catalog")
async def clear_catalog():
    db = await get_db()
    try:
        await db.execute("DELETE FROM movies")
        await db.execute(
            "UPDATE sync_state SET total_movies = 0, message = 'Catalog cleared' WHERE id = 1"
        )
        await db.commit()
        return {"status": "ok", "message": "Catalog cleared (category mappings preserved)"}
    finally:
        await db.close()


@router.post("/sync/catalog")
async def trigger_catalog_sync(request: Request):
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass
    max_movies = data.get("max_movies", 0)
    category_ids = data.get("category_ids", [])
    account_ids = data.get("account_ids", [])

    if not category_ids and not account_ids:
        db = await get_db()
        try:
            sel_acct_rows = await db.execute("SELECT account_id FROM selected_accounts WHERE enabled = 1")
            account_ids = [r["account_id"] for r in await sel_acct_rows.fetchall()]
            sel_cat_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
            category_ids = [r["category_id"] for r in await sel_cat_rows.fetchall()]
        finally:
            await db.close()

    if account_ids and not category_ids:
        db = await get_db()
        try:
            placeholders = ",".join("?" for _ in account_ids)
            rows = await db.execute(
                f"SELECT DISTINCT category_id FROM vod_category_accounts WHERE account_id IN ({placeholders})",
                account_ids,
            )
            category_ids = [r["category_id"] for r in await rows.fetchall()]
        finally:
            await db.close()

    asyncio.create_task(_run_catalog_sync(max_movies, category_ids, account_ids))
    return {"status": "started", "message": f"Catalog sync started ({len(category_ids)} categories, {len(account_ids)} providers)"}


@router.post("/sync/stop")
async def stop_sync():
    request_cancel()
    return {"status": "ok", "message": "Stop requested"}


@router.post("/sync/refresh-activated-ids")
async def refresh_activated_ids():
    """Refresh stream_ids for all activated movies against current mapping file."""
    updated = await _refresh_activated_stream_ids()
    return {"status": "ok", "refreshed": updated, "message": f"Refreshed stream_ids for {updated} activated movies"}


@router.get("/health/status")
async def health_status():
    """Get current health status of bridge, Dispatcharr, and rclone."""
    return get_health_status()


@router.get("/health/log")
async def health_log():
    """Get timestamped health check log."""
    return {"logs": get_health_log()}


@router.post("/health/check-now")
async def trigger_health_check():
    """Manually trigger a health check (normally runs every 2 hours)."""
    result = await run_health_checks()
    return {"status": "ok", "result": result}


@router.post("/movies/validate-catalog")
async def trigger_catalog_validation(request: Request):
    """Manually trigger a catalog validation batch. Pass {full: true} for full sweep."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    full = data.get("full", False)
    if full:
        asyncio.create_task(_validate_catalog_full())
        return {"status": "started", "message": "Full catalog validation sweep started"}
    asyncio.create_task(_validate_catalog_batch())
    return {"status": "started", "message": f"Validating next {CATALOG_VALIDATE_BATCH} activated movies"}


@router.post("/movies/resurrect-check")
async def trigger_resurrection():
    """Manually trigger a resurrection check on dead streams."""
    asyncio.create_task(_resurrect_dead_streams())
    return {"status": "started", "message": f"Checking up to {RESURRECTION_BATCH} dead streams for resurrection"}


@router.get("/movies/stream-health")
async def stream_health_stats():
    """Get stream health statistics."""
    db = await get_db()
    try:
        total = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total_count = (await total.fetchone())["cnt"]

        healthy = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1 AND stream_dead = 0")
        healthy_count = (await healthy.fetchone())["cnt"]

        striking = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1 AND stream_dead = 0 AND stream_dead_count > 0")
        striking_count = (await striking.fetchone())["cnt"]

        dead = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE stream_dead = 1 AND dead = 0")
        dead_count = (await dead.fetchone())["cnt"]

        never_checked = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1 AND stream_dead_checked_at IS NULL")
        never_count = (await never_checked.fetchone())["cnt"]

        return {
            "total_activated": total_count,
            "healthy": healthy_count,
            "striking": striking_count,
            "stream_dead": dead_count,
            "never_validated": never_count,
        }
    finally:
        await db.close()


async def _run_catalog_sync(max_movies: int = 0, category_ids: list = None, account_ids: list = None):
    try:
        if not is_cancelled() and (TMDB_API_KEY or TMDB_READ_TOKEN):
            lang_task = asyncio.create_task(_bulk_detect_languages())
        else:
            lang_task = None

        await scrape_catalog(max_movies=max_movies, category_ids=category_ids, account_ids=account_ids)
        if not is_cancelled():
            await apply_stream_mapping_to_db()

        if lang_task:
            await lang_task
            if not is_cancelled():
                await _bulk_detect_languages()
    except Exception as e:
        logger.error(f"Catalog sync failed: {e}")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
                (str(e)[:500],),
            )
            await db.commit()
        finally:
            await db.close()


@router.post("/sync/clear-strm")
async def clear_strm():
    import shutil
    from config import STRM_OUTPUT_DIR
    db = await get_db()
    try:
        if os.path.exists(STRM_OUTPUT_DIR):
            for item in os.listdir(STRM_OUTPUT_DIR):
                full = os.path.join(STRM_OUTPUT_DIR, item)
                if os.path.isdir(full):
                    shutil.rmtree(full)
            count_removed = len(os.listdir(STRM_OUTPUT_DIR))
        await db.execute(
            "UPDATE sync_state SET active_strm_count = 0, message = 'All STRM files cleared' WHERE id = 1"
        )
        await db.commit()
        return {"status": "ok", "message": "All STRM files cleared"}
    finally:
        await db.close()


@router.post("/sync/generate")
async def trigger_generate():
    asyncio.create_task(_run_generate())
    return {"status": "started", "message": "STRM generation started in background"}


async def _run_generate():
    try:
        await generate_strm_files()
    except Exception as e:
        logger.error(f"STRM generation failed: {e}")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
                (str(e)[:500],),
            )
            await db.commit()
        finally:
            await db.close()


@router.post("/sync/full")
async def trigger_full_sync():
    asyncio.create_task(_run_full_sync())
    return {"status": "started", "message": "Full sync started in background"}


@router.post("/sync/mapping")
async def trigger_mapping_sync():
    asyncio.create_task(_run_mapping_sync())
    return {"status": "started", "message": "Stream mapping sync started"}


async def _run_mapping_sync():
    try:
        updated = await apply_stream_mapping_to_db()
        logger.info(f"Stream mapping applied: {updated} movies updated")
    except Exception as e:
        logger.error(f"Stream mapping sync failed: {e}")


async def _run_full_sync():
    try:
        db = await get_db()
        try:
            sel_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
            category_ids = [r["category_id"] for r in await sel_rows.fetchall()]
        finally:
            await db.close()

        await scrape_catalog(category_ids=category_ids)
        if is_cancelled():
            return
        await apply_stream_mapping_to_db()
        if is_cancelled():
            return
        await generate_strm_files()
    except Exception as e:
        logger.error(f"Full sync failed: {e}")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
                (str(e)[:500],),
            )
            await db.commit()
        finally:
            await db.close()


@router.post("/movies/{movie_id}/detect-language")
async def detect_single_language(movie_id: int):
    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        return JSONResponse(status_code=400, content={"error": "TMDB API key not configured"})

    db = await get_db()
    try:
        row = await db.execute(
            "SELECT id, tmdb_id FROM movies WHERE id = ?", (movie_id,)
        )
        movie = await row.fetchone()
        if not movie:
            return JSONResponse(status_code=404, content={"error": "Movie not found"})
        if not movie["tmdb_id"]:
            return {"id": movie_id, "language": None, "message": "No TMDB ID"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"https://api.themoviedb.org/3/movie/{movie['tmdb_id']}"
            headers = {}
            params = {}
            if TMDB_READ_TOKEN:
                headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
            else:
                params["api_key"] = TMDB_API_KEY
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                return {"id": movie_id, "language": None, "message": f"TMDB returned {resp.status_code}"}
            lang = resp.json().get("original_language", "")

        await db.execute("UPDATE movies SET language = ? WHERE id = ?", (lang, movie_id))
        await db.commit()
        lang_name = LANG_NAMES.get(lang, lang)
        return {"id": movie_id, "language": lang, "language_name": lang_name}
    finally:
        await db.close()


# --- Dead Movie System ---

DEAD_DIR = os.path.join(STRM_OUTPUT_DIR, ".dead") if STRM_OUTPUT_DIR else "/plex-vod/Movies/.dead"


@router.get("/movies/dead")
async def list_dead_movies(
    page: int = 1,
    page_size: int = 100,
    search: str = "",
):
    db = await get_db()
    try:
        conditions = ["m.dead = 1", "m.name != ''"]
        params = []
        if search:
            conditions.append("m.name LIKE ?")
            params.append(f"%{search}%")
        where = " AND ".join(conditions)

        count_row = await db.execute(f"SELECT COUNT(*) as cnt FROM movies m WHERE {where}", params)
        count = (await count_row.fetchone())["cnt"]

        offset = (page - 1) * page_size
        rows = await db.execute(
            f"SELECT m.id, m.name, m.year, m.rating, m.poster_url, m.account_name, m.language, m.dead_at "
            f"FROM movies m WHERE {where} ORDER BY m.dead_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        movies = [dict(r) for r in await rows.fetchall()]
        return {"count": count, "page": page, "page_size": page_size, "results": movies}
    finally:
        await db.close()


@router.post("/movies/dead/delete")
async def delete_dead_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, name, year FROM movies WHERE id IN ({placeholders}) AND dead = 1",
            movie_ids,
        )
        movies = await rows.fetchall()

        removed = 0
        for m in movies:
            dead_folder = os.path.join(DEAD_DIR, _movie_folder_name(m))
            if os.path.isdir(dead_folder):
                shutil.rmtree(dead_folder)
                removed += 1

        await db.execute(
            f"DELETE FROM movies WHERE id IN ({placeholders}) AND dead = 1",
            movie_ids,
        )
        await db.execute(
            f"DELETE FROM movie_categories WHERE movie_id IN ({placeholders})",
            movie_ids,
        )
        await db.commit()

        return {"status": "ok", "deleted": len(movies), "strm_removed": removed}
    finally:
        await db.close()


@router.post("/movies/dead/delete-all")
async def delete_all_dead():
    db = await get_db()
    try:
        rows = await db.execute("SELECT id, name, year FROM movies WHERE dead = 1")
        movies = await rows.fetchall()
        movie_ids = [m["id"] for m in movies]

        if os.path.isdir(DEAD_DIR):
            shutil.rmtree(DEAD_DIR)
            os.makedirs(DEAD_DIR, exist_ok=True)

        if movie_ids:
            placeholders = ",".join("?" for _ in movie_ids)
            await db.execute(f"DELETE FROM movies WHERE id IN ({placeholders})", movie_ids)
            await db.execute(f"DELETE FROM movie_categories WHERE movie_id IN ({placeholders})", movie_ids)

        await db.commit()
        return {"status": "ok", "deleted": len(movies)}
    finally:
        await db.close()


@router.post("/movies/dead/restore")
async def restore_dead_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, name, year FROM movies WHERE id IN ({placeholders}) AND dead = 1",
            movie_ids,
        )
        movies = await rows.fetchall()

        restored_strm = 0
        for m in movies:
            folder_name = _movie_folder_name(m)
            dead_folder = os.path.join(DEAD_DIR, folder_name)
            live_folder = os.path.join(STRM_OUTPUT_DIR, folder_name)
            if os.path.isdir(dead_folder):
                shutil.move(dead_folder, live_folder)
                restored_strm += 1

        await db.execute(
            f"UPDATE movies SET dead = 0, dead_at = NULL WHERE id IN ({placeholders})",
            movie_ids,
        )
        await db.commit()
        return {"status": "ok", "restored": len(movies), "strm_restored": restored_strm}
    finally:
        await db.close()


@router.post("/movies/dead/scan")
async def trigger_dead_scan():
    asyncio.create_task(_run_dead_scan())
    return {"status": "started", "message": "Dead movie scan started"}


async def _run_dead_scan():
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = 'detecting', message = 'Scanning for dead movies...' WHERE id = 1"
        )
        await db.commit()

        req_headers = {}
        if DISPATCHARR_API_KEY:
            req_headers["X-API-Key"] = DISPATCHARR_API_KEY

        live_ids = set()
        page = 1
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                resp = await client.get(
                    f"{DISPATCHARR_URL}/api/vod/movies/?page={page}&page_size=500",
                    headers=req_headers,
                )
                if resp.status_code != 200:
                    logger.error(f"Dead scan: Dispatcharr API error {resp.status_code}")
                    break
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                for m in results:
                    live_ids.add(m["id"])
                if not data.get("next"):
                    break
                page += 1
                await asyncio.sleep(0.05)

        if not live_ids:
            await db.execute(
                "UPDATE sync_state SET status = 'error', message = 'Dead scan: could not fetch live VOD list' WHERE id = 1"
            )
            await db.commit()
            return

        await db.execute(
            "UPDATE sync_state SET message = ? WHERE id = 1",
            (f"Checking {len(live_ids)} live movies against catalog...",),
        )
        await db.commit()

        rows = await db.execute(
            "SELECT id, name, year, activated FROM movies WHERE dead = 0 AND name != ''"
        )
        catalog_movies = await rows.fetchall()

        now = datetime.now(timezone.utc).isoformat()
        newly_dead = []
        dead_activated = []
        for m in catalog_movies:
            # Mark as dead if: (1) not in live catalog OR (2) stream_dead flag set
            is_dead = m["id"] not in live_ids
            if not is_dead:
                # Check if stream was marked as dead (provider errors)
                stream_check = await db.execute(
                    "SELECT stream_dead FROM movies WHERE id = ?",
                    (m["id"],),
                )
                result = await stream_check.fetchone()
                is_dead = result and result["stream_dead"] == 1

            if is_dead:
                newly_dead.append(m)
                if m["activated"]:
                    dead_activated.append(m)

        if not newly_dead:
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
                (f"Dead scan complete: all {len(catalog_movies)} catalog movies still live",),
            )
            await db.commit()
            logger.info(f"Dead scan: 0 dead out of {len(catalog_movies)}")
            return

        dead_ids = [m["id"] for m in newly_dead]
        for did in dead_ids:
            await db.execute(
                "UPDATE movies SET dead = 1, dead_at = ?, activated = 0 WHERE id = ?",
                (now, did),
            )
        await db.commit()

        os.makedirs(DEAD_DIR, exist_ok=True)
        moved = 0
        for m in dead_activated:
            folder_name = _movie_folder_name(m)
            live_folder = os.path.join(STRM_OUTPUT_DIR, folder_name)
            dead_folder = os.path.join(DEAD_DIR, folder_name)
            if os.path.isdir(live_folder):
                shutil.move(live_folder, dead_folder)
                moved += 1

        strm_count = _count_strm_folders()
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (strm_count,))
        await db.commit()

        plex_removed = 0
        if dead_activated:
            plex_removed = await _plex_remove_movies([m["id"] for m in dead_activated])

        msg = f"Dead scan: {len(newly_dead)} dead ({moved} STRM moved, {plex_removed} removed from Plex)"
        await db.execute(
            "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
            (msg,),
        )
        await db.commit()
        logger.info(msg)
    except Exception as e:
        logger.error(f"Dead scan failed: {e}")
        await db.execute(
            "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
            (str(e)[:500],),
        )
        await db.commit()
    finally:
        await db.close()


async def start_dead_scan_scheduler():
    while True:
        await asyncio.sleep(DEAD_SCAN_INTERVAL_HOURS * 3600)
        logger.info("Scheduled maintenance: regenerating dumps + dead scan...")
        await _trigger_dump_regen()
        await _run_dead_scan()
