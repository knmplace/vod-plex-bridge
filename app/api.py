import asyncio
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from scraper import scrape_catalog, enrich_from_tmdb, request_cancel, is_cancelled
from generator import generate_strm_files
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


@router.get("/movies")
async def list_movies(genre: str = "", page: int = 1, page_size: int = 50, sort_by: str = "rating", sort_order: str = "desc"):
    db = await get_db()
    try:
        sort_col = {"rating": "rating", "year": "year", "name": "name"}.get(sort_by, "rating")
        order = "DESC" if sort_order == "desc" else "ASC"
        offset = (page - 1) * page_size

        if genre:
            count_row = await db.execute(
                "SELECT COUNT(*) as cnt FROM movies WHERE genre LIKE ? AND name != ''",
                (f"%{genre}%",),
            )
            rows = await db.execute(
                f"SELECT id, name, year, rating, genre, tmdb_id, poster_url FROM movies WHERE genre LIKE ? AND name != '' ORDER BY {sort_col} {order} LIMIT ? OFFSET ?",
                (f"%{genre}%", page_size, offset),
            )
        else:
            count_row = await db.execute("SELECT COUNT(*) as cnt FROM movies WHERE name != ''")
            rows = await db.execute(
                f"SELECT id, name, year, rating, genre, tmdb_id, poster_url FROM movies WHERE name != '' ORDER BY {sort_col} {order} LIMIT ? OFFSET ?",
                (page_size, offset),
            )

        count = (await count_row.fetchone())["cnt"]
        movies = [dict(r) for r in await rows.fetchall()]
        return {"count": count, "page": page, "page_size": page_size, "results": movies}
    finally:
        await db.close()


@router.get("/genres")
async def list_genres():
    db = await get_db()
    try:
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


@router.post("/sync/catalog")
async def trigger_catalog_sync(request: Request):
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass
    max_movies = data.get("max_movies", 0)
    asyncio.create_task(_run_catalog_sync(max_movies))
    return {"status": "started", "message": f"Catalog sync started (limit: {max_movies or 'none'})"}


@router.post("/sync/stop")
async def stop_sync():
    request_cancel()
    return {"status": "ok", "message": "Stop requested"}


async def _run_catalog_sync(max_movies: int = 0):
    try:
        await scrape_catalog(max_movies=max_movies)
        if not is_cancelled():
            while True:
                enriched = await enrich_from_tmdb(batch_size=500)
                if enriched == 0 or is_cancelled():
                    break
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
        await scrape_catalog()
        if is_cancelled():
            return
        await apply_stream_mapping_to_db()
        while True:
            enriched = await enrich_from_tmdb(batch_size=500)
            if enriched == 0 or is_cancelled():
                break
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
