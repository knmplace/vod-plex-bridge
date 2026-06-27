import asyncio
import logging
import os
import re
import shutil
import httpx
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, STRM_OUTPUT_DIR, PLEX_URL, PLEX_TOKEN, PLEX_LIBRARY_ID, TMDB_API_KEY, TMDB_READ_TOKEN
from database import get_db
from scraper import scrape_catalog, enrich_from_tmdb, request_cancel, is_cancelled, search_tmdb_for_missing
from generator import generate_strm_files, sanitize_filename
from stream_mapper import apply_stream_mapping_to_db, load_stream_mapping, pick_stream_for_account
from proxy import probe_file_size, get_proxy_log, get_all_pipes, _log_event, archive_proxy_log, cleanup_old_archives, list_log_archives, get_log_archive
from health import run_health_checks, get_health_status, get_health_log, health_check_scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
_refresh_running = False


async def _probe_stream_status(url: str, headers: dict) -> httpx.Response:
    """Probe a stream URL, following only the first 301 if it stays on Dispatcharr's domain."""
    disp_host = urlparse(DISPATCHARR_URL).netloc.split(":")[0]
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10),
        follow_redirects=False,
    ) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code in (301, 302):
            location = resp.headers.get("location", "")
            if location.startswith("/"):
                base = urlparse(DISPATCHARR_URL)
                location = f"{base.scheme}://{base.netloc}{location}"
            redirect_host = urlparse(location).netloc.split(":")[0]
            if redirect_host != disp_host:
                logger.warning("Probe blocked redirect to external host %s (expected %s)", redirect_host, disp_host)
                return resp
            resp = await client.get(location, headers=headers)
        return resp

MAINTENANCE_INTERVAL_HOURS = 24


@router.get("/status")
async def get_status():
    db = await get_db()
    try:
        row = await db.execute("SELECT * FROM sync_state WHERE id = 1")
        state = await row.fetchone()
        return dict(state)
    finally:
        pass


@router.get("/vpn-ip")
async def get_vpn_ip():
    try:
        headers = {}
        if DISPATCHARR_API_KEY:
            headers["X-API-Key"] = DISPATCHARR_API_KEY
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{DISPATCHARR_URL}/api/core/settings/env/", headers=headers)
            data = r.json()
            return {"ip": data.get("public_ip", ""), "country": data.get("country_name", ""), "city": data.get("city", "")}
    except Exception:
        return {"ip": "", "country": "", "city": ""}


@router.get("/providers")
async def list_providers(browse: bool = False):
    db = await get_db()
    try:
        if browse:
            rows = await db.execute(
                "SELECT DISTINCT a.id, a.name FROM m3u_accounts a "
                "JOIN movies m ON m.account_id = a.id WHERE m.name != '' ORDER BY a.name"
            )
        else:
            rows = await db.execute("SELECT id, name FROM m3u_accounts ORDER BY name")
        accounts = [dict(r) for r in await rows.fetchall()]

        sel_rows = await db.execute("SELECT account_id FROM selected_accounts WHERE enabled = 1")
        selected = {r["account_id"] for r in await sel_rows.fetchall()}

        for a in accounts:
            a["selected"] = a["id"] in selected

        return accounts
    finally:
        pass


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
        pass


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
        pass


@router.get("/categories")
async def list_categories(request: Request, browse: bool = False):
    import json as _json
    account_ids = [int(x) for x in request.query_params.getlist("account_id") if x.isdigit()]
    db = await get_db()
    try:
        if browse:
            rows = await db.execute(
                "SELECT c.id, c.name, c.category_type, COALESCE(c.hidden, 0) as hidden, "
                "COUNT(mc.movie_id) as movie_count "
                "FROM vod_categories c "
                "JOIN movie_categories mc ON c.id = mc.category_id "
                "JOIN movies m ON mc.movie_id = m.id AND m.name != '' "
                "WHERE COALESCE(c.hidden, 0) = 0 "
                "GROUP BY c.id ORDER BY c.name"
            )
            cats = [dict(r) for r in await rows.fetchall()]
        elif account_ids:
            acct_set = set(account_ids)
            placeholders = ",".join("?" for _ in account_ids)
            rows = await db.execute(
                "SELECT DISTINCT c.id, c.name, c.category_type, c.movie_count, COALESCE(c.hidden, 0) as hidden "
                "FROM vod_categories c "
                "JOIN vod_category_accounts ca ON c.id = ca.category_id "
                f"WHERE ca.account_id IN ({placeholders}) ORDER BY c.name",
                account_ids,
            )
            cats = [dict(r) for r in await rows.fetchall()]

            provider_movie_ids = None
            all_acct_rows = await db.execute("SELECT id FROM m3u_accounts")
            all_acct_ids = {r["id"] for r in await all_acct_rows.fetchall()}
            if acct_set < all_acct_ids and os.path.exists(STREAM_MAPPING_FILE):
                with open(STREAM_MAPPING_FILE) as f:
                    stream_map = _json.load(f)
                provider_movie_ids = set()
                for mid_str, info in stream_map.items():
                    entries = info if isinstance(info, list) else [info]
                    if any(e.get("account_id") in acct_set for e in entries):
                        provider_movie_ids.add(int(mid_str))

            if provider_movie_ids is not None and os.path.exists(CATEGORY_MAPPING_FILE):
                with open(CATEGORY_MAPPING_FILE) as f:
                    dump_cats = _json.load(f)
                cat_movie_map = {dc["id"]: set(dc.get("movie_ids", [])) for dc in dump_cats}
                for c in cats:
                    cat_movies = cat_movie_map.get(c["id"], set())
                    c["movie_count"] = len(cat_movies & provider_movie_ids)
        else:
            rows = await db.execute(
                "SELECT id, name, category_type, movie_count, COALESCE(hidden, 0) as hidden FROM vod_categories ORDER BY name"
            )
            cats = [dict(r) for r in await rows.fetchall()]

        sel_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
        selected = {r["category_id"] for r in await sel_rows.fetchall()}

        for c in cats:
            c["selected"] = c["id"] in selected

        return cats
    finally:
        pass


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
        pass


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
        pass


@router.post("/categories/hide")
async def hide_category(request: Request):
    data = await request.json()
    category_id = data.get("category_id")
    hidden = data.get("hidden", 1)
    if not category_id:
        return JSONResponse(status_code=400, content={"error": "category_id required"})

    db = await get_db()
    try:
        await db.execute("UPDATE vod_categories SET hidden = ? WHERE id = ?", (hidden, category_id))
        await db.commit()
        return {"status": "ok", "hidden": hidden}
    finally:
        pass


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
        pass

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

        if not _refresh_running:
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
                (f"Loaded {cat_count} categories, {map_count} mappings, {acct_count} providers",),
            )
            await db.commit()
        logger.info(f"Loaded {cat_count} categories, {map_count} movie mappings, {acct_count} providers")
        return {"categories": cat_count, "mappings": map_count, "providers": acct_count}
    except Exception as e:
        logger.error(f"Failed to load categories: {e}")
        await db.execute(
            "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
            (str(e)[:500],),
        )
        await db.commit()
        raise
    finally:
        pass


async def _load_categories_counted() -> dict:
    return await _load_categories()


@router.get("/movies")
async def list_movies(
    request: Request,
    genre: str = "",
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "rating",
    sort_order: str = "desc",
    search: str = "",
    activated_only: bool = False,
):
    account_ids = [int(x) for x in request.query_params.getlist("account_id") if x]
    category_ids = [int(x) for x in request.query_params.getlist("category_id") if x]
    languages = [x for x in request.query_params.getlist("language") if x]

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

        if category_ids:
            ph = ",".join("?" for _ in category_ids)
            conditions.append(f"m.id IN (SELECT movie_id FROM movie_categories WHERE category_id IN ({ph}))")
            params.extend(category_ids)
        else:
            conditions.append(
                "m.id NOT IN ("
                "SELECT mc2.movie_id FROM movie_categories mc2 "
                "GROUP BY mc2.movie_id "
                "HAVING SUM(CASE WHEN mc2.category_id IN (SELECT id FROM vod_categories WHERE hidden = 1) THEN 1 ELSE 0 END) = COUNT(*)"
                ")"
            )

        if genre:
            conditions.append("m.genre LIKE ?")
            params.append(f"%{genre}%")

        if search:
            conditions.append("m.name LIKE ?")
            params.append(f"%{search}%")

        if languages:
            ph = ",".join("?" for _ in languages)
            conditions.append(f"m.language IN ({ph})")
            params.extend(languages)

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
        pass


HEADER_FETCH_SIZE = 8 * 1024 * 1024   # 8MB head — gives pipe time to build buffer ahead of Plex
TAIL_FETCH_SIZE = 256 * 1024          # 256KB tail — moov atom / seek table


@router.get("/proxy-log")
async def proxy_log():
    return get_proxy_log()


@router.get("/proxy-log/archives")
async def proxy_log_archives():
    return list_log_archives()


@router.get("/proxy-log/archives/{filename}")
async def proxy_log_archive_detail(filename: str):
    data = get_log_archive(filename)
    if data is None:
        return JSONResponse(status_code=404, content={"error": "Archive not found"})
    return data


@router.post("/proxy-log/archive-now")
async def proxy_log_archive_now():
    fname = archive_proxy_log()
    if not fname:
        return {"status": "empty", "message": "No log entries to archive"}
    return {"status": "ok", "filename": fname}


@router.get("/debug/connections")
async def debug_connections():
    pipes = get_all_pipes()
    active = sum(1 for p in pipes.values() if p["started"] and not p["finished"])
    return {
        "total_pipes": len(pipes),
        "active_downloading": active,
        "pipes": pipes,
    }


@router.get("/streams/active")
async def active_streams():
    pipes = get_all_pipes()
    if not pipes:
        return {"streams": []}
    movie_ids = list(pipes.keys())
    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, name, year, account_name, content_type FROM movies WHERE id IN ({placeholders})",
            movie_ids,
        )
        movies = {r["id"]: dict(r) for r in await rows.fetchall()}
    finally:
        pass
    streams = []
    for mid, pipe in pipes.items():
        movie = movies.get(mid, {})
        entry = {**pipe}
        entry["movie_name"] = movie.get("name", f"Movie {mid}")
        entry["movie_year"] = movie.get("year")
        entry["account_name"] = movie.get("account_name", "Unknown")
        entry["content_type"] = movie.get("content_type", "video/x-matroska")
        if pipe["started"] and not pipe["finished"] and not pipe["error"]:
            entry["status"] = "streaming"
        elif pipe["finished"]:
            entry["status"] = "complete"
        elif pipe["error"]:
            entry["status"] = "error"
        else:
            entry["status"] = "starting"
        streams.append(entry)
    return {"streams": streams}


@router.post("/movies/activate")
async def activate_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
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
        needs_header = await db.execute(
            f"SELECT id, uuid, stream_id, account_id, content_type, name, year FROM movies WHERE id IN ({placeholders}) AND stream_id IS NOT NULL",
            movie_ids,
        )
        to_validate = [dict(r) for r in await needs_header.fetchall()]

        activated_ids = []
        failed_ids = []

        if to_validate:
            activated_ids, failed_ids = await _fetch_headers_validated(to_validate)

        no_stream = [mid for mid in movie_ids if mid not in {m["id"] for m in to_validate}]
        if no_stream:
            failed_ids.extend(no_stream)

        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total = (await count_row.fetchone())["cnt"]

        return {
            "status": "ok",
            "activated": len(activated_ids),
            "failed": len(failed_ids),
            "activated_ids": activated_ids,
            "failed_ids": failed_ids,
            "total_activated": total,
            "stream_ids_refreshed": updated_ids,
        }
    finally:
        pass


async def _refresh_activated_stream_ids():
    """Refresh stream_ids for activated movies. If stream_id changed, re-fetch head/tail sequentially."""
    mapping = load_stream_mapping()
    if not mapping:
        logger.warning("Cannot refresh stream_ids: no mapping file")
        return 0

    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT id, account_id, stream_id, uuid, content_type, name, year FROM movies WHERE activated = 1"
        )
        activated = [dict(r) for r in await rows.fetchall()]

        updated = 0
        needs_refetch = []
        for movie in activated:
            movie_id = movie["id"]
            old_stream_id = movie["stream_id"]
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

                if old_stream_id and str(old_stream_id) != str(stream_id):
                    await db.execute(
                        "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ? WHERE id = ?",
                        (stream_id, content_type, account_id, account_name, movie_id),
                    )
                    updated += 1
                    needs_refetch.append({
                        "id": movie_id,
                        "uuid": movie["uuid"],
                        "stream_id": stream_id,
                        "content_type": content_type,
                        "name": movie["name"],
                        "year": movie["year"],
                        "account_id": account_id,
                    })
                    logger.info("Stream_id changed for activated movie %d (%s): %s -> %s",
                                movie_id, movie["name"], old_stream_id, stream_id)
                else:
                    result = await db.execute(
                        "UPDATE movies SET stream_id = ?, content_type = ?, account_id = ?, account_name = ? "
                        "WHERE id = ? AND (stream_id IS NULL OR stream_id != ?)",
                        (stream_id, content_type, account_id, account_name, movie_id, stream_id),
                    )
                    if result.rowcount > 0:
                        updated += 1

        if updated > 0:
            await db.commit()
            logger.info("Refreshed stream_ids for %d activated movies", updated)

        if needs_refetch:
            logger.info("Re-fetching head/tail for %d activated movies with changed stream_ids (sequential)...",
                        len(needs_refetch))
            activated_ids, failed_ids = await _fetch_headers_validated(needs_refetch)
            logger.info("Stream_id refresh re-fetch: %d ok, %d failed", len(activated_ids), len(failed_ids))

        missing_rows = await db.execute(
            "SELECT id FROM movies WHERE activated = 1 AND stream_bitrate_kbps IS NULL LIMIT 50"
        )
        missing = [r["id"] for r in await missing_rows.fetchall()]
        if missing:
            logger.info("Backfilling provider info for %d activated movies", len(missing))
            for mid in missing:
                await _fetch_provider_info(mid)
                await asyncio.sleep(0.1)

        return updated
    finally:
        pass


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
    finally:
        pass

    plex_removed = await _plex_remove_movies([movie_id])

    movie_info = {"name": name, "year": year}
    _remove_strm_folders([movie_info])

    strm_count = _count_strm_folders()
    db = await get_db()
    try:
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (strm_count,))
        await db.commit()
    finally:
        pass

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
        pass

    if not batch:
        return

    logger.info("Catalog validation: checking %d activated movies", len(batch))
    now = datetime.now(timezone.utc).isoformat()

    for movie in batch:
        movie_id = movie["id"]
        uuid = movie["uuid"]
        stream_id = movie["stream_id"]

        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
        req_headers = {"Range": "bytes=0-0"}

        try:
            resp = await _probe_stream_status(upstream_url, req_headers)

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
                pass
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
        pass

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
            pass

        if not batch:
            break

        batch_num += 1
        now = datetime.now(timezone.utc).isoformat()

        for movie in batch:
            movie_id = movie["id"]
            uuid = movie["uuid"]
            stream_id = movie["stream_id"]

            upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
            req_headers = {"Range": "bytes=0-0"}

            try:
                resp = await _probe_stream_status(upstream_url, req_headers)

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
                    pass
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
        pass

    if not batch:
        return

    logger.info("Resurrection check: testing %d dead streams", len(batch))
    now = datetime.now(timezone.utc).isoformat()
    resurrected = 0

    for movie in batch:
        movie_id = movie["id"]
        uuid = movie["uuid"]
        stream_id = movie["stream_id"]

        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
        req_headers = {"Range": "bytes=0-0"}

        try:
            resp = await _probe_stream_status(upstream_url, req_headers)

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
                pass
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


async def _fetch_provider_info(movie_id: int):
    """Fetch bitrate and duration from Dispatcharr's provider-info endpoint."""
    try:
        url = f"{DISPATCHARR_URL}/api/vod/movies/{movie_id}/provider-info/"
        headers = {}
        if DISPATCHARR_API_KEY:
            headers["X-API-Key"] = DISPATCHARR_API_KEY
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            bitrate = data.get("bitrate")
            duration_str = data.get("duration_secs")
            duration = int(duration_str) if duration_str and str(duration_str).isdigit() else None
            if bitrate or duration:
                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE movies SET stream_bitrate_kbps = COALESCE(?, stream_bitrate_kbps), "
                        "duration_seconds = COALESCE(?, duration_seconds) WHERE id = ?",
                        (bitrate, duration, movie_id),
                    )
                    await db.commit()
                    logger.info("Provider info for movie %d: bitrate=%s kbps, duration=%ss", movie_id, bitrate, duration)
                finally:
                    pass
            return {"bitrate": bitrate, "duration": duration}
    except Exception as e:
        logger.warning("Failed to fetch provider info for movie %d: %s", movie_id, e)
        return None


async def _fetch_headers_validated(movies: list[dict]) -> tuple[list[int], list[int]]:
    """Fetch head+tail for each movie. Returns (activated_ids, failed_ids).
    Only marks movie activated=1 if head bytes are successfully retrieved.
    Marks stream_dead=1 on failure."""
    from proxy import _resolve_session as resolve_session
    activated_ids = []
    failed_ids = []

    for i, movie in enumerate(movies):
        if i > 0:
            await asyncio.sleep(5)
        movie_id = movie["id"]
        movie_name = movie.get("name", "")
        try:
            uuid = movie["uuid"]
            stream_id = movie["stream_id"]
            content_type = movie.get("content_type", "video/x-matroska")

            await _fetch_provider_info(movie_id)

            base_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"

            result = await resolve_session(base_url, movie_id)
            if not result:
                logger.warning("Activate: session resolve failed for movie %d (%s)", movie_id, movie_name)
                _log_event("error", movie_id, "Activate failed: session resolve failed", movie_name=movie_name)
                await _mark_activation_dead(movie_id, movie_name, movie.get("year"), "session resolve failed")
                failed_ids.append(movie_id)
                continue

            session_url, session_id = result

            disp_host = urlparse(DISPATCHARR_URL).netloc.split(":")[0]
            session_host = urlparse(session_url).netloc.split(":")[0]
            if session_host != disp_host:
                logger.error("Activate: movie %d resolved to external host %s — BLOCKED", movie_id, session_host)
                _log_event("error", movie_id, f"Activate blocked: external host {session_host}", movie_name=movie_name)
                failed_ids.append(movie_id)
                continue

            req_headers = {"Range": f"bytes=0-{HEADER_FETCH_SIZE - 1}"}

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
                follow_redirects=False,
            ) as client:
                resp = await client.get(session_url, headers=req_headers)
                if resp.status_code >= 400:
                    logger.warning("Activate: header fetch failed for movie %d (%s): HTTP %d", movie_id, movie_name, resp.status_code)
                    _log_event("error", movie_id, f"Activate failed: HTTP {resp.status_code}", movie_name=movie_name)
                    await _mark_activation_dead(movie_id, movie_name, movie.get("year"), f"header fetch HTTP {resp.status_code}")
                    failed_ids.append(movie_id)
                    continue

                header_bytes = resp.content
                if not header_bytes or len(header_bytes) == 0:
                    logger.warning("Activate: empty header response for movie %d (%s)", movie_id, movie_name)
                    _log_event("error", movie_id, "Activate failed: empty response", movie_name=movie_name)
                    await _mark_activation_dead(movie_id, movie_name, movie.get("year"), "empty header response")
                    failed_ids.append(movie_id)
                    continue

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
                    try:
                        tail_resp = await client.get(session_url, headers=tail_headers)
                        if tail_resp.status_code < 400:
                            tail_bytes = tail_resp.content
                        else:
                            logger.warning("Activate: tail fetch failed for movie %d: HTTP %d", movie_id, tail_resp.status_code)
                    except Exception as te:
                        logger.warning("Activate: tail fetch error for movie %d: %s", movie_id, te)

                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE movies SET activated = 1, stream_dead = 0, stream_dead_count = 0, "
                        "header_data = ?, header_size = ?, "
                        "tail_data = ?, tail_size = ?, tail_offset = ?, "
                        "file_size = CASE WHEN file_size IS NULL OR file_size = 0 THEN ? ELSE file_size END "
                        "WHERE id = ?",
                        (header_bytes, len(header_bytes), tail_bytes, len(tail_bytes) if tail_bytes else 0,
                         tail_offset, file_size, movie_id),
                    )
                    await db.commit()
                    activated_ids.append(movie_id)
                    logger.info("Activated movie %d (%s): head=%d bytes, tail=%d bytes, file_size=%s",
                                movie_id, movie_name, len(header_bytes), len(tail_bytes) if tail_bytes else 0, file_size)
                    _log_event("info", movie_id, "Activated — head+tail cached",
                               movie_name=movie_name,
                               head_bytes=len(header_bytes), tail_bytes=len(tail_bytes) if tail_bytes else 0, file_size=file_size)
                finally:
                    pass

        except Exception as e:
            logger.error("Activate error for movie %d (%s): %s", movie_id, movie_name, e)
            _log_event("error", movie_id, f"Activate exception: {e}", movie_name=movie_name)
            await _mark_activation_dead(movie_id, movie_name, movie.get("year"), str(e))
            failed_ids.append(movie_id)

    return activated_ids, failed_ids


async def _mark_activation_dead(movie_id: int, name: str, year: int | None, reason: str):
    """Mark a movie as dead during activation — deactivate and clean up STRM/Plex if it was previously activated."""
    db = await get_db()
    try:
        row = await db.execute("SELECT activated FROM movies WHERE id = ?", (movie_id,))
        prev = await row.fetchone()
        was_activated = prev and prev["activated"] == 1

        await db.execute(
            "UPDATE movies SET stream_dead = 1, stream_dead_count = 1, activated = 0, "
            "header_data = NULL, header_size = 0, tail_data = NULL, tail_size = 0, "
            "tail_offset = 0, stream_dead_checked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), movie_id),
        )
        await db.commit()
    finally:
        pass

    if was_activated:
        await _plex_remove_movies([movie_id])
        movie_info = {"name": name, "year": year}
        folder_name = _movie_folder_name(movie_info)
        live_folder = os.path.join(STRM_OUTPUT_DIR, folder_name)
        dead_folder = os.path.join(DEAD_DIR, folder_name)
        if os.path.isdir(live_folder):
            os.makedirs(DEAD_DIR, exist_ok=True)
            shutil.move(live_folder, dead_folder)
            logger.info("Moved STRM to dead: %s", folder_name)

    logger.warning("Activation failed — marked dead: movie %d (%s) — %s", movie_id, name, reason)


async def _fetch_headers(movies: list[dict]):
    """Legacy background header fetch — kept for non-activation code paths."""
    from proxy import _resolve_session as resolve_session
    for movie in movies:
        try:
            uuid = movie["uuid"]
            stream_id = movie["stream_id"]
            movie_id = movie["id"]
            content_type = movie.get("content_type", "video/x-matroska")

            await _fetch_provider_info(movie_id)

            base_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"

            result = await resolve_session(base_url, movie_id)
            if not result:
                logger.warning("Header fetch: could not resolve session for movie %d", movie_id)
                _log_event("error", movie_id, "Header fetch: session resolve failed")
                continue

            session_url, session_id = result

            disp_host = urlparse(DISPATCHARR_URL).netloc.split(":")[0]
            session_host = urlparse(session_url).netloc.split(":")[0]
            if session_host != disp_host:
                logger.error("Header fetch: session URL for movie %d resolved to external host %s — BLOCKED", movie_id, session_host)
                _log_event("error", movie_id, f"Header fetch blocked: external host {session_host}")
                continue

            logger.info("Header fetch: using session %s for movie %d", session_id, movie_id)

            req_headers = {"Range": f"bytes=0-{HEADER_FETCH_SIZE - 1}"}

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
                follow_redirects=False,
            ) as client:
                resp = await client.get(session_url, headers=req_headers)
                if resp.status_code >= 400:
                    logger.warning("Header fetch failed for movie %d: HTTP %d", movie_id, resp.status_code)
                    _log_event("error", movie_id, f"Header fetch failed: HTTP {resp.status_code}")
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
                    try:
                        tail_resp = await client.get(session_url, headers=tail_headers)
                        if tail_resp.status_code < 400:
                            tail_bytes = tail_resp.content
                            logger.info("Tail fetched: movie %d, %d bytes from offset %d (same session)", movie_id, len(tail_bytes), tail_offset)
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
                    pass

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

        plex_removed = await _plex_remove_movies(movie_ids)

        removed = _remove_strm_folders(movies)

        strm_count = _count_strm_folders()
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (strm_count,))
        await db.commit()

        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total = (await count_row.fetchone())["cnt"]

        return {"status": "ok", "deactivated": len(movie_ids), "total_activated": total, "strm_removed": removed, "plex_removed": plex_removed}
    finally:
        pass


@router.post("/movies/deactivate-all")
async def deactivate_all():
    db = await get_db()
    try:
        rows = await db.execute("SELECT id FROM movies WHERE activated = 1")
        active_ids = [r["id"] for r in await rows.fetchall()]

        await db.execute("UPDATE movies SET activated = 0")
        await db.commit()

        plex_removed = await _plex_remove_movies(active_ids) if active_ids else 0

        removed = 0
        if os.path.exists(STRM_OUTPUT_DIR):
            for item in os.listdir(STRM_OUTPUT_DIR):
                full = os.path.join(STRM_OUTPUT_DIR, item)
                if os.path.isdir(full):
                    shutil.rmtree(full)
                    removed += 1
        await db.execute("UPDATE sync_state SET active_strm_count = 0 WHERE id = 1")
        await db.commit()

        return {"status": "ok", "message": f"All movies deactivated, {removed} STRM folders removed, {plex_removed} removed from Plex"}
    finally:
        pass


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
        pass


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
        pass


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
        pass


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
        pass


@router.post("/movies/detect-language-all")
async def detect_language_all():
    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        return JSONResponse(status_code=400, content={"error": "TMDB API key not configured"})
    asyncio.create_task(_bulk_detect_languages())
    return {"status": "started", "message": "Bulk language detection started in background"}


_lang_detect_running = False

async def _bulk_detect_languages():
    global _lang_detect_running
    if _lang_detect_running:
        logger.info("Language detection already running, skipping")
        return
    _lang_detect_running = True
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

        est_minutes = max(1, round(total * 0.5 / 60))
        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Detecting languages in background: 0/{total} (~{est_minutes} min remaining)...",),
        )
        await db.commit()
        logger.info(f"Language detection started: {total} movies, estimated {est_minutes} min")

        detected = 0
        skipped = 0
        no_tmdb = 0
        import time
        start_time = time.time()

        async def fetch_lang(client, movie):
            nonlocal detected, skipped, no_tmdb
            if is_cancelled():
                return
            tmdb_id = movie['tmdb_id']
            if not tmdb_id:
                no_tmdb += 1
                return
            url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
            headers = {}
            params = {}
            if TMDB_READ_TOKEN:
                headers["Authorization"] = f"Bearer {TMDB_READ_TOKEN}"
            else:
                params["api_key"] = TMDB_API_KEY
            for attempt in range(5):
                try:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "4"))
                        logger.debug(f"TMDB 429 for movie {movie['id']}, retry after {retry_after}s (attempt {attempt+1})")
                        await asyncio.sleep(retry_after + 1)
                        continue
                    if resp.status_code == 404:
                        skipped += 1
                        return
                    if resp.status_code != 200:
                        logger.warning(f"TMDB {resp.status_code} for tmdb_id={tmdb_id} movie={movie['id']}")
                        skipped += 1
                        return
                    lang = resp.json().get("original_language", "")
                    if lang:
                        await db.execute("UPDATE movies SET language = ? WHERE id = ?", (lang, movie["id"]))
                        detected += 1
                    else:
                        skipped += 1
                    return
                except Exception as e:
                    if attempt < 4:
                        await asyncio.sleep(2)
                        continue
                    logger.warning(f"TMDB error for movie {movie['id']}: {e}")
                    skipped += 1
                    return
            skipped += 1

        async with httpx.AsyncClient(timeout=15.0) as client:
            for i in range(total):
                if is_cancelled():
                    break
                await fetch_lang(client, movies[i])
                await asyncio.sleep(0.5)
                processed = detected + skipped + no_tmdb
                if processed % 25 == 0 and processed > 0:
                    await db.commit()
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 2
                    remaining = total - processed
                    est_min = max(1, round(remaining / rate / 60))
                    await db.execute(
                        "UPDATE sync_state SET lang_status = ? WHERE id = 1",
                        (f"Detecting languages in background: {processed}/{total} ({detected} detected, ~{est_min} min remaining)...",),
                    )
                    await db.commit()

        await db.commit()
        elapsed_min = round((time.time() - start_time) / 60, 1)
        status_msg = "cancelled" if is_cancelled() else "complete"
        skip_detail = f"{skipped} not found"
        if no_tmdb:
            skip_detail += f", {no_tmdb} no TMDB ID"
        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Language detection {status_msg}: {detected} detected, {skip_detail} ({elapsed_min} min)",),
        )
        await db.commit()
        logger.info(f"Bulk language detection {status_msg}: {detected} detected, {skip_detail} out of {total} in {elapsed_min} min")
    except Exception as e:
        logger.error(f"Bulk language detection failed: {e}")
        await db.execute(
            "UPDATE sync_state SET lang_status = ? WHERE id = 1",
            (f"Error: {str(e)[:200]}",),
        )
        await db.commit()
    finally:
        _lang_detect_running = False


@router.get("/catalog/summary")
async def catalog_summary():
    db = await get_db()
    try:
        cat_rows = await db.execute(
            "SELECT c.id, c.name, COALESCE(c.hidden, 0) as hidden, COUNT(mc.movie_id) as movie_count, "
            "SUM(CASE WHEN m.activated = 1 THEN 1 ELSE 0 END) as activated_count "
            "FROM vod_categories c "
            "JOIN movie_categories mc ON c.id = mc.category_id "
            "JOIN movies m ON mc.movie_id = m.id AND m.name != '' "
            "WHERE COALESCE(c.hidden, 0) = 0 "
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
        pass


@router.get("/filters")
async def get_filters():
    db = await get_db()
    try:
        rows = await db.execute("SELECT * FROM filter_configs ORDER BY genre")
        return [dict(r) for r in await rows.fetchall()]
    finally:
        pass


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
        pass


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
        pass


@router.delete("/filters/{filter_id}")
async def delete_filter(filter_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM filter_configs WHERE id = ?", (filter_id,))
        await db.commit()
        return {"status": "ok"}
    finally:
        pass


@router.post("/sync/clear-catalog")
async def clear_catalog():
    db = await get_db()
    try:
        await db.execute("DELETE FROM movies")
        await db.execute(
            "UPDATE sync_state SET total_movies = 0, message = 'Catalog cleared', lang_status = '' WHERE id = 1"
        )
        await db.commit()
        return {"status": "ok", "message": "Catalog cleared (category mappings preserved)"}
    finally:
        pass


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

    db = await get_db()

    if not category_ids and not account_ids:
        sel_acct_rows = await db.execute("SELECT account_id FROM selected_accounts WHERE enabled = 1")
        account_ids = [r["account_id"] for r in await sel_acct_rows.fetchall()]
        sel_cat_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
        category_ids = [r["category_id"] for r in await sel_cat_rows.fetchall()]

    if not category_ids:
        return JSONResponse(status_code=400, content={"error": "No categories selected. Select at least one category before syncing."})

    # If all providers are selected, skip provider filter (it's redundant)
    if account_ids:
        all_acct_rows = await db.execute("SELECT id FROM m3u_accounts")
        all_acct_ids = {r["id"] for r in await all_acct_rows.fetchall()}
        if set(account_ids) >= all_acct_ids:
            account_ids = []
            logger.info("All providers selected — skipping provider filter")

    asyncio.create_task(_run_catalog_sync(max_movies, category_ids, account_ids))
    return {"status": "started", "message": f"Catalog sync started ({len(category_ids)} categories, {len(account_ids) if account_ids else 'all'} providers)"}


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
    """Get current health status of bridge, Dispatcharr, and Plex VOD library."""
    return get_health_status()


@router.get("/health/log")
async def health_log():
    """Get timestamped health check log."""
    return {"logs": get_health_log()}


@router.post("/health/check-now")
async def trigger_health_check():
    """Manually trigger a health check (normally runs every 2 hours)."""
    result = await run_health_checks()
    return {"status": result}


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
        pass


async def _run_catalog_sync(max_movies: int = 0, category_ids: list = None, account_ids: list = None):
    try:
        await scrape_catalog(max_movies=max_movies, category_ids=category_ids, account_ids=account_ids)
        if not is_cancelled():
            await apply_stream_mapping_to_db()
        if not is_cancelled():
            await search_tmdb_for_missing()

        if not is_cancelled() and (TMDB_API_KEY or TMDB_READ_TOKEN):
            asyncio.create_task(_bulk_detect_languages())
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
            pass


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
        pass


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
            pass


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


@router.get("/schedule")
async def get_schedule():
    db = await get_db()
    try:
        row = await db.execute(
            "SELECT refresh_interval_hours, last_scheduled_refresh, last_refresh_report FROM sync_state WHERE id = 1"
        )
        result = await row.fetchone()
        return {
            "refresh_interval_hours": result["refresh_interval_hours"] if result else 0,
            "last_scheduled_refresh": result["last_scheduled_refresh"] if result else None,
            "last_refresh_report": result["last_refresh_report"] if result else "",
        }
    finally:
        pass


@router.post("/schedule")
async def set_schedule(request: Request):
    data = await request.json()
    hours = data.get("refresh_interval_hours", 0)
    if hours not in (0, 4, 6, 8, 12):
        return JSONResponse(status_code=400, content={"error": "interval must be 0, 4, 6, 8, or 12"})
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET refresh_interval_hours = ? WHERE id = 1", (hours,)
        )
        await db.commit()
        label = "Off" if hours == 0 else f"every {hours}h"
        logger.info("Catalog refresh schedule set to %s", label)
        return {"status": "ok", "refresh_interval_hours": hours}
    finally:
        pass


@router.post("/sync/refresh")
async def trigger_manual_refresh():
    global _refresh_running
    if _refresh_running:
        return JSONResponse(status_code=409, content={"error": "Refresh already running"})
    _refresh_running = True
    asyncio.create_task(_run_scheduled_refresh_guarded())
    return {"status": "started", "message": "Manual catalog refresh started in background"}


async def _run_scheduled_refresh_guarded():
    global _refresh_running
    try:
        await _run_scheduled_refresh()
    finally:
        _refresh_running = False


async def _run_full_sync():
    try:
        db = await get_db()
        try:
            sel_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
            category_ids = [r["category_id"] for r in await sel_rows.fetchall()]
        finally:
            pass

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
            pass


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
        pass


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
        conditions = ["(m.dead = 1 OR m.stream_dead = 1)", "m.name != ''"]
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
        pass


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
        pass


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
        pass


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
        pass


@router.post("/movies/mark-dead")
async def mark_movies_dead(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        rows = await db.execute(
            f"SELECT id, name, year, activated FROM movies WHERE id IN ({placeholders})",
            movie_ids,
        )
        movies = [dict(r) for r in await rows.fetchall()]

        for m in movies:
            if m["activated"]:
                await _plex_remove_movies([m["id"]])
                folder_name = _movie_folder_name(m)
                live_folder = os.path.join(STRM_OUTPUT_DIR, folder_name)
                dead_folder = os.path.join(DEAD_DIR, folder_name)
                if os.path.isdir(live_folder):
                    os.makedirs(DEAD_DIR, exist_ok=True)
                    shutil.move(live_folder, dead_folder)

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            f"UPDATE movies SET dead = 1, dead_at = ?, activated = 0 WHERE id IN ({placeholders})",
            [now] + movie_ids,
        )
        await db.commit()
        return {"status": "ok", "marked": len(movies)}
    finally:
        pass


@router.post("/movies/tmdb-search")
async def trigger_tmdb_search():
    """Manually trigger TMDB title search for movies missing TMDB IDs."""
    if not TMDB_API_KEY and not TMDB_READ_TOKEN:
        return JSONResponse(status_code=400, content={"error": "TMDB API key not configured"})
    asyncio.create_task(_run_tmdb_search())
    return {"status": "started", "message": "TMDB title search started in background"}


@router.post("/movies/tmdb-search/reset")
async def reset_tmdb_search():
    """Reset tmdb_searched flag so failed lookups can be retried."""
    db = await get_db()
    result = await db.execute(
        "UPDATE movies SET tmdb_searched = 0 WHERE tmdb_searched = 1 AND (tmdb_id IS NULL OR tmdb_id = '')"
    )
    await db.commit()
    return {"status": "ok", "reset": result.rowcount}


async def _run_tmdb_search():
    db = await get_db()
    try:
        count_row = await db.execute(
            "SELECT COUNT(*) as cnt FROM movies WHERE (tmdb_id IS NULL OR tmdb_id = '') AND tmdb_searched = 0 AND name != ''"
        )
        total = (await count_row.fetchone())["cnt"]
        await db.execute(
            "UPDATE sync_state SET status = 'searching', message = ? WHERE id = 1",
            (f"TMDB search: looking up {total} movies...",),
        )
        await db.commit()
    finally:
        pass

    try:
        result = await search_tmdb_for_missing(batch_size=500)
        db = await get_db()
        await db.execute(
            "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
            (f"TMDB search complete: {result['found']} found, {result['not_found']} not found, {result['skipped']} skipped",),
        )
        await db.commit()
    except Exception as e:
        logger.error("TMDB search task failed: %s", e)
        db = await get_db()
        await db.execute(
            "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
            (f"TMDB search failed: {str(e)[:200]}",),
        )
        await db.commit()


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
                if is_cancelled():
                    break
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

        if is_cancelled():
            if not _refresh_running:
                await db.execute(
                    "UPDATE sync_state SET status = 'idle', message = 'Dead scan cancelled' WHERE id = 1"
                )
                await db.commit()
            logger.info("Dead scan cancelled by user")
            return

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
            if not _refresh_running:
                await db.execute(
                    "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
                    (f"Dead scan complete: all {len(catalog_movies)} catalog movies still live",),
                )
                await db.commit()
            logger.info(f"Dead scan: 0 dead out of {len(catalog_movies)}")
            return {"newly_dead": 0, "strm_moved": 0, "plex_removed": 0}

        dead_ids = [m["id"] for m in newly_dead]
        for did in dead_ids:
            await db.execute(
                "UPDATE movies SET dead = 1, dead_at = ?, activated = 0 WHERE id = ?",
                (now, did),
            )
        await db.commit()

        plex_removed = 0
        if dead_activated:
            plex_removed = await _plex_remove_movies([m["id"] for m in dead_activated])

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

        msg = f"Dead scan: {len(newly_dead)} dead ({moved} STRM moved, {plex_removed} removed from Plex)"
        if not _refresh_running:
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = ? WHERE id = 1",
                (msg,),
            )
            await db.commit()
        logger.info(msg)
        return {"newly_dead": len(newly_dead), "strm_moved": moved, "plex_removed": plex_removed}
    except Exception as e:
        logger.error(f"Dead scan failed: {e}")
        await db.execute(
            "UPDATE sync_state SET status = 'error', message = ? WHERE id = 1",
            (str(e)[:500],),
        )
        await db.commit()
        raise
    finally:
        pass


async def _run_dead_scan_counted() -> dict:
    result = await _run_dead_scan()
    return result or {"newly_dead": 0, "strm_moved": 0, "plex_removed": 0}


async def _get_refresh_interval_hours() -> int:
    db = await get_db()
    try:
        row = await db.execute("SELECT refresh_interval_hours FROM sync_state WHERE id = 1")
        result = await row.fetchone()
        return result["refresh_interval_hours"] if result and result["refresh_interval_hours"] else 0
    except Exception:
        return 0
    finally:
        pass


async def start_dead_scan_scheduler():
    while True:
        interval = await _get_refresh_interval_hours()
        if interval <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(interval * 3600)
        if _refresh_running:
            logger.info("Scheduled refresh skipped — manual refresh already running")
            continue
        logger.info("Scheduled catalog refresh starting (interval: %dh)...", interval)
        await _run_scheduled_refresh_guarded()


LOG_ARCHIVE_INTERVAL = int(os.environ.get("LOG_ARCHIVE_INTERVAL", 4 * 3600))


async def start_log_archive_scheduler():
    while True:
        await asyncio.sleep(LOG_ARCHIVE_INTERVAL)
        try:
            archive_proxy_log()
            cleanup_old_archives()
        except Exception as e:
            logger.error(f"Log archive failed: {e}")


async def _set_status(status: str, message: str):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = ?, message = ? WHERE id = 1",
            (status, message),
        )
        await db.commit()
    finally:
        pass


async def _run_scheduled_refresh():
    """Full refresh cycle: dump regen → categories → catalog refresh → apply mapping → refresh activated → dead scan."""
    import time
    start = time.time()
    report = []

    try:
        # 1. Dump regen
        await _set_status("refreshing", "Refresh: regenerating stream dumps...")
        try:
            ok = await _trigger_dump_regen()
            report.append(f"Dump regen: {'OK' if ok else 'timed out'}")
        except Exception as e:
            report.append(f"Dump regen: FAILED ({e})")

        # 2. Categories
        await _set_status("refreshing", "Refresh: reloading categories...")
        try:
            cat_result = await _load_categories_counted()
            report.append(f"Categories: {cat_result['categories']} categories, {cat_result['mappings']} mappings, {cat_result['providers']} providers")
        except Exception as e:
            report.append(f"Categories: FAILED ({e})")

        # 3. Catalog refresh
        await _set_status("refreshing", "Refresh: syncing catalog from Dispatcharr...")
        try:
            synced, total, uuid_changes = await _run_catalog_refresh_counted()
            parts = [f"{synced} processed, {total} total"]
            if uuid_changes:
                parts.append(f"{uuid_changes} UUID changes")
            report.append(f"Catalog: {', '.join(parts)}")
        except Exception as e:
            report.append(f"Catalog: FAILED ({e})")

        # 4. Stream mapping
        await _set_status("refreshing", "Refresh: applying stream mapping...")
        try:
            mapped = await apply_stream_mapping_to_db()
            report.append(f"Stream mapping: {mapped} updated")
        except Exception as e:
            report.append(f"Stream mapping: FAILED ({e})")

        # 5. TMDB title search for missing metadata
        await _set_status("refreshing", "Refresh: searching TMDB for unmatched movies...")
        try:
            tmdb_result = await search_tmdb_for_missing()
            report.append(f"TMDB search: {tmdb_result['found']} found, {tmdb_result['not_found']} not found, {tmdb_result['skipped']} skipped")
        except Exception as e:
            report.append(f"TMDB search: FAILED ({e})")

        # 6. Activated refresh
        await _set_status("refreshing", "Refresh: checking activated movies for stream_id changes...")
        try:
            updated = await _refresh_activated_stream_ids()
            report.append(f"Activated refresh: {updated} stream_ids changed")
        except Exception as e:
            report.append(f"Activated refresh: FAILED ({e})")

        # 7. Dead scan
        await _set_status("refreshing", "Refresh: scanning for dead movies...")
        try:
            dead_result = await _run_dead_scan_counted()
            report.append(f"Dead scan: {dead_result['newly_dead']} newly dead, {dead_result['plex_removed']} removed from Plex")
        except Exception as e:
            report.append(f"Dead scan: FAILED ({e})")

        # 8. Language detection (background)
        if TMDB_API_KEY or TMDB_READ_TOKEN:
            asyncio.create_task(_bulk_detect_languages())
            report.append("Language detection: started in background")

        import json as _json
        elapsed = int(time.time() - start)
        now_local = datetime.now().strftime("%b %d, %Y %I:%M %p")
        new_entry = {"date": now_local, "elapsed": elapsed, "steps": report}

        db = await get_db()
        try:
            row = await db.execute("SELECT last_refresh_report FROM sync_state WHERE id = 1")
            result = await row.fetchone()
            raw = result["last_refresh_report"] if result else ""
            try:
                archive = _json.loads(raw) if raw and raw.startswith("[") else []
            except Exception:
                archive = []
            archive.insert(0, new_entry)
            archive = archive[:10]
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = 'Refresh complete', "
                "last_scheduled_refresh = ?, last_refresh_report = ? WHERE id = 1",
                (datetime.now(timezone.utc).isoformat(), _json.dumps(archive)),
            )
            await db.commit()
        finally:
            pass

        logger.info("Scheduled catalog refresh complete (%ds): %s", elapsed, "; ".join(report))

    except Exception as e:
        logger.error("Scheduled refresh crashed: %s", e)
        await _set_status("idle", f"Refresh failed: {e}")


_uuid_change_counter = 0


async def _run_catalog_refresh():
    """Re-scrape full VOD catalog from Dispatcharr to pick up new/changed movies."""
    synced, total, uuid_changes = await _run_catalog_refresh_counted()
    return synced


async def _run_catalog_refresh_counted() -> tuple[int, int, int]:
    """Two-phase catalog refresh: update existing movies, then discover new ones in selected categories."""
    import json as _json
    global _uuid_change_counter
    _uuid_change_counter = 0

    db = await get_db()

    # --- Phase 1: Refresh existing catalog movies ---
    rows = await db.execute("SELECT id FROM movies WHERE name != ''")
    existing_ids = [r["id"] for r in await rows.fetchall()]
    existing_id_set = set(existing_ids)
    total_target = len(existing_ids)

    await db.execute(
        "UPDATE sync_state SET status = 'scraping', message = ? WHERE id = 1",
        (f"Catalog refresh: updating 0/{total_target} existing movies...",),
    )
    await db.commit()

    req_headers = {}
    if DISPATCHARR_API_KEY:
        req_headers["X-API-Key"] = DISPATCHARR_API_KEY

    total_updated = 0
    failed = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, movie_id in enumerate(existing_ids):
            if is_cancelled():
                break
            try:
                resp = await client.get(
                    f"{DISPATCHARR_URL}/api/vod/movies/{movie_id}/",
                    headers=req_headers,
                )
                if resp.status_code == 200:
                    movie = resp.json()
                    await _upsert_movie_from_api(db, movie)
                    total_updated += 1
                elif resp.status_code == 404:
                    failed += 1
                    logger.info(f"Catalog refresh: movie {movie_id} no longer in Dispatcharr")
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Catalog refresh: failed to fetch movie {movie_id}: {e}")

            if (i + 1) % 50 == 0:
                await db.commit()
                await db.execute(
                    "UPDATE sync_state SET message = ? WHERE id = 1",
                    (f"Catalog refresh: updated {total_updated}/{total_target} existing movies...",),
                )
                await db.commit()
            await asyncio.sleep(0.02)

    await db.commit()

    # --- Phase 2: Discover new movies in selected categories/providers ---
    new_added = 0
    if not is_cancelled():
        sel_cat_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
        selected_cats = [r["category_id"] for r in await sel_cat_rows.fetchall()]
        sel_acct_rows = await db.execute("SELECT account_id FROM selected_accounts WHERE enabled = 1")
        selected_accts = set(r["account_id"] for r in await sel_acct_rows.fetchall())

        if selected_cats:
            # Get movie IDs in selected categories from category_mapping dump
            cat_movie_ids = set()
            if os.path.exists(CATEGORY_MAPPING_FILE):
                with open(CATEGORY_MAPPING_FILE) as f:
                    dump_cats = _json.load(f)
                selected_cat_set = set(selected_cats)
                for dc in dump_cats:
                    if dc["id"] in selected_cat_set:
                        cat_movie_ids.update(dc.get("movie_ids", []))

            # Filter by selected providers (skip if all providers selected)
            if selected_accts and cat_movie_ids:
                all_acct_rows = await db.execute("SELECT id FROM m3u_accounts")
                all_acct_ids = {r["id"] for r in await all_acct_rows.fetchall()}
                if selected_accts >= all_acct_ids:
                    logger.info("Catalog refresh: all providers selected, skipping provider filter")
                else:
                    stream_map_file = os.environ.get("STREAM_MAPPING_FILE", "/data/stream_mapping.json")
                    if os.path.exists(stream_map_file):
                        with open(stream_map_file) as f:
                            stream_map = _json.load(f)
                        provider_movie_ids = set()
                        for mid_str, info in stream_map.items():
                            entries = info if isinstance(info, list) else [info]
                            if any(e.get("account_id") in selected_accts for e in entries):
                                provider_movie_ids.add(int(mid_str))
                        before = len(cat_movie_ids)
                        cat_movie_ids = cat_movie_ids & provider_movie_ids
                        logger.info(f"Catalog refresh: provider filter {before} -> {len(cat_movie_ids)} movies")

            # Find movies in selected categories that aren't in our catalog yet
            new_movie_ids = cat_movie_ids - existing_id_set
            if new_movie_ids:
                logger.info(f"Catalog refresh: {len(new_movie_ids)} new movies found in selected categories")
                await db.execute(
                    "UPDATE sync_state SET message = ? WHERE id = 1",
                    (f"Catalog refresh: adding {len(new_movie_ids)} new movies from selected categories...",),
                )
                await db.commit()

                async with httpx.AsyncClient(timeout=30.0) as client:
                    for i, movie_id in enumerate(sorted(new_movie_ids)):
                        if is_cancelled():
                            break
                        try:
                            resp = await client.get(
                                f"{DISPATCHARR_URL}/api/vod/movies/{movie_id}/",
                                headers=req_headers,
                            )
                            if resp.status_code == 200:
                                movie = resp.json()
                                await _upsert_movie_from_api(db, movie)
                                new_added += 1
                        except Exception as e:
                            logger.warning(f"Catalog refresh: failed to add new movie {movie_id}: {e}")

                        if (i + 1) % 50 == 0:
                            await db.commit()
                            await db.execute(
                                "UPDATE sync_state SET message = ? WHERE id = 1",
                                (f"Catalog refresh: added {new_added}/{len(new_movie_ids)} new movies...",),
                            )
                            await db.commit()
                        await asyncio.sleep(0.02)
                await db.commit()
                logger.info(f"Catalog refresh: added {new_added} new movies")
            else:
                logger.info("Catalog refresh: no new movies in selected categories")
        else:
            logger.info("Catalog refresh: no selected categories, skipping new movie discovery")

    # --- Final counts ---
    count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE name != ''")
    count = (await count_row.fetchone())["cnt"]
    status = "cancelled" if is_cancelled() else "complete"
    parts = [f"{total_updated}/{total_target} updated"]
    if failed:
        parts.append(f"{failed} failed")
    if new_added:
        parts.append(f"{new_added} new")
    parts.append(f"{count} total")
    msg = f"Catalog refresh {status}: {', '.join(parts)}"
    if _refresh_running:
        await db.execute(
            "UPDATE sync_state SET total_movies = ?, message = ? WHERE id = 1",
            (count, msg),
        )
    else:
        await db.execute(
            "UPDATE sync_state SET total_movies = ?, status = 'idle', message = ? WHERE id = 1",
            (count, msg),
        )
    await db.commit()
    logger.info(f"Catalog refresh {status}: {', '.join(parts)}")

    return total_updated + new_added, count, _uuid_change_counter


async def _upsert_movie_from_api(db, movie):
    """Insert or update a movie from Dispatcharr API response."""
    import json
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

    new_uuid = movie["uuid"]
    movie_id = movie["id"]

    row = await db.execute("SELECT uuid FROM movies WHERE id = ?", (movie_id,))
    existing = await row.fetchone()
    if existing and existing["uuid"] and existing["uuid"] != new_uuid:
        global _uuid_change_counter
        await db.execute(
            "UPDATE movies SET header_data = NULL, header_size = 0, "
            "tail_data = NULL, tail_size = 0, tail_offset = 0, file_size = NULL "
            "WHERE id = ?",
            (movie_id,),
        )
        _uuid_change_counter += 1
        logger.info("UUID changed for movie %d (%s -> %s), cleared caches", movie_id, existing["uuid"], new_uuid)

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
        custom.get("cast", ""),
        trailer_key,
        datetime.now(timezone.utc).isoformat(),
    ))
