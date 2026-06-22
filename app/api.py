import asyncio
import logging
import os
import re
import shutil
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, STRM_OUTPUT_DIR, PLEX_URL, PLEX_TOKEN, PLEX_LIBRARY_ID
from database import get_db
from scraper import scrape_catalog, enrich_from_tmdb, request_cancel, is_cancelled
from generator import generate_strm_files, sanitize_filename
from stream_mapper import apply_stream_mapping_to_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


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


@router.post("/categories/load")
async def load_categories():
    asyncio.create_task(_load_categories())
    return {"status": "started", "message": "Loading categories from Dispatcharr..."}


CATEGORY_MAPPING_FILE = os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json")


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
        for cat in api_categories:
            cat_type = cat.get("category_type", "movie")
            if cat_type != "movie":
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

            for acct in cat.get("m3u_accounts", []):
                acct_id = acct.get("m3u_account") if isinstance(acct, dict) else acct
                if acct_id:
                    account_ids.add(acct_id)
                    await db.execute(
                        "INSERT OR IGNORE INTO vod_category_accounts (category_id, account_id) VALUES (?, ?)",
                        (cat["id"], acct_id),
                    )

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
):
    account_ids = [int(x) for x in request.query_params.getlist("account_id") if x]

    db = await get_db()
    try:
        sort_col = {"rating": "rating", "year": "year", "name": "name"}.get(sort_by, "rating")
        order = "DESC" if sort_order == "desc" else "ASC"
        offset = (page - 1) * page_size

        conditions = ["m.name != ''"]
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

        where = " AND ".join(conditions)

        count_row = await db.execute(
            f"SELECT COUNT(*) as cnt FROM movies m WHERE {where}", params
        )
        count = (await count_row.fetchone())["cnt"]

        rows = await db.execute(
            f"SELECT m.id, m.name, m.year, m.rating, m.genre, m.tmdb_id, m.poster_url, "
            f"m.activated, m.account_id, m.account_name, m.trailer_key "
            f"FROM movies m WHERE {where} ORDER BY m.{sort_col} {order} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        movies = [dict(r) for r in await rows.fetchall()]
        return {"count": count, "page": page, "page_size": page_size, "results": movies}
    finally:
        await db.close()


@router.post("/movies/activate")
async def activate_movies(request: Request):
    data = await request.json()
    movie_ids = data.get("movie_ids", [])
    if not movie_ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})

    db = await get_db()
    try:
        placeholders = ",".join("?" for _ in movie_ids)
        await db.execute(
            f"UPDATE movies SET activated = 1 WHERE id IN ({placeholders})",
            movie_ids,
        )
        await db.commit()
        count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE activated = 1")
        total = (await count_row.fetchone())["cnt"]
        return {"status": "ok", "activated": len(movie_ids), "total_activated": total}
    finally:
        await db.close()


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


async def _run_catalog_sync(max_movies: int = 0, category_ids: list = None, account_ids: list = None):
    try:
        await scrape_catalog(max_movies=max_movies, category_ids=category_ids, account_ids=account_ids)
        if not is_cancelled():
            await apply_stream_mapping_to_db()
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
