"""API for the TMDB Local page: status/stats, lookup, settings, manual runs, clear."""

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import tmdb_local
import tmdb_store
from database import get_db

router = APIRouter(prefix="/api/tmdb-store", tags=["tmdb-store"])
_tasks: set[asyncio.Task] = set()  # keep manual-run tasks referenced until done


class StoreSettings(BaseModel):
    enabled: bool | None = None
    burst_size: int | None = None
    bursts_per_day: int | None = None
    daily_budget: int | None = None
    concurrency: int | None = None
    auto_match_batch: int | None = None
    popular_keep: int | None = None


@router.get("/status")
async def get_status():
    settings = tmdb_local.get_settings()
    has_key = tmdb_local.has_credentials()
    stats = await asyncio.to_thread(tmdb_store.stats)
    next_burst = await asyncio.to_thread(tmdb_store.get_meta, "next_burst_at")
    return {
        "has_api_key": has_key,
        "active": has_key and settings["enabled"],
        "settings": settings,
        "stats": stats,
        "requests_today": await asyncio.to_thread(tmdb_store.requests_today),
        "lookups_today": await asyncio.to_thread(tmdb_store.lookups_today),
        "next_burst_at": float(next_burst) if next_burst else None,
        "db_size_bytes": await asyncio.to_thread(tmdb_store.db_size_bytes),
        "retention_policy": {
            "maximum_days": tmdb_store.MAX_CACHE_AGE_SECONDS // 86400,
            "refresh_after_days": tmdb_store.REFRESH_CACHE_AGE_SECONDS // 86400,
            "last_run_at": float(await asyncio.to_thread(tmdb_store.get_meta, "retention_run_at", "0") or 0) or None,
        },
        "job": tmdb_local.status,
    }


@router.get("/search")
async def search(q: str, type: str = "movie", year: int | None = None):
    if type not in tmdb_store.MEDIA_TYPES:
        raise HTTPException(400, detail="type must be movie or tv")
    try:
        return await tmdb_local.lookup(q, type, year)
    except Exception as exc:
        raise HTTPException(502, detail=f"TMDB lookup failed: {tmdb_local._redact(exc)}")


@router.put("/settings")
async def put_settings(body: StoreSettings):
    return await asyncio.to_thread(tmdb_local.save_settings, body.model_dump(exclude_none=True))


@router.post("/run/{job}")
async def run(job: str):
    if job not in ("export", "changes", "burst", "match"):
        raise HTTPException(400, detail="job must be export, changes, burst or match")
    if not tmdb_local.has_credentials():
        raise HTTPException(400, detail="Save a TMDB API key in Settings first")
    if not tmdb_local.store_enabled():
        raise HTTPException(400, detail="The local TMDB library is turned off")
    if tmdb_local.status["running"]:
        raise HTTPException(409, detail="A TMDB store job is already running")
    task = asyncio.create_task(tmdb_local.run_job(job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"started": job}


@router.post("/clear")
async def clear():
    """Delete all stored data to free disk space. Only while the library is
    off, so nothing refills it straight away."""
    if tmdb_local.store_enabled():
        raise HTTPException(400, detail="Turn the local library off first")
    if tmdb_local.status["running"]:
        raise HTTPException(409, detail="A TMDB store job is still running -- try again when it finishes")
    await asyncio.to_thread(tmdb_store.clear)
    return {"cleared": True}


@router.post("/retention")
async def retention(dry_run: bool = True):
    """Preview by default; explicitly apply catalog-first retention."""
    if tmdb_local.status["running"]:
        raise HTTPException(409, detail="A TMDB store job is already running")
    result = await tmdb_local.retention(dry_run=dry_run)
    if not dry_run:
        await asyncio.to_thread(tmdb_store.set_meta, "retention_run_at", time.time())
    return result


# Titles without a TMDB ID (mostly adult content) can be locked to provider data:
# no title search, TMDB check or language lookup ever touches them.
_PROVIDER_ONLY = {
    "movie": ("movies", "movie_categories", "movie_id", "vod_categories"),
    "series": ("series", "series_provider_category_map", "series_id", "series_provider_categories"),
}


class LockRequest(BaseModel):
    kind: str = "movie"
    locked: bool = True
    ids: list[int] | None = None
    all_matching: bool = False
    state: str = "unlocked"
    category_id: int | None = None
    q: str = ""


def _provider_only_where(kind: str, state: str, category_id: int | None, q: str) -> tuple[str, list]:
    if kind not in _PROVIDER_ONLY:
        raise HTTPException(400, detail="kind must be movie or series")
    _, link, fk, _ = _PROVIDER_ONLY[kind]
    where, args = [], []
    if state == "locked":
        where.append("COALESCE(t.tmdb_locked, 0) = 1")
    else:
        where.append("(t.tmdb_id IS NULL OR t.tmdb_id = '')")
        if state == "unlocked":
            where.append("COALESCE(t.tmdb_locked, 0) = 0")
    if category_id:
        where.append(f"t.id IN (SELECT {fk} FROM {link} WHERE category_id = ?)")
        args.append(category_id)
    if q.strip():
        where.append("t.name LIKE ?")
        args.append(f"%{q.strip()}%")
    return " AND ".join(where), args


@router.get("/provider-only")
async def provider_only(kind: str = "movie", state: str = "unlocked", category_id: int | None = None,
                        q: str = "", limit: int = 200, offset: int = 0):
    """Titles with no TMDB ID (or already locked), with per-category counts for the filter."""
    table, link, fk, cats = _PROVIDER_ONLY.get(kind, _PROVIDER_ONLY["movie"])
    where, args = _provider_only_where(kind, state, category_id, q)
    base_where, base_args = _provider_only_where(kind, state, None, "")
    db = await get_db()
    total = (await (await db.execute(f"SELECT COUNT(*) FROM {table} t WHERE {where}", args)).fetchone())[0]
    rows = await (await db.execute(
        f"SELECT t.id, t.name, t.year, t.poster_url, COALESCE(t.tmdb_locked, 0) AS locked, "
        f"(SELECT GROUP_CONCAT(c.name, ', ') FROM {link} l JOIN {cats} c ON c.id = l.category_id "
        f" WHERE l.{fk} = t.id) AS categories "
        f"FROM {table} t WHERE {where} ORDER BY t.name LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, 500)), max(0, offset)),
    )).fetchall()
    categories = await (await db.execute(
        f"SELECT c.id, c.name, COUNT(*) AS count FROM {table} t JOIN {link} l ON l.{fk} = t.id "
        f"JOIN {cats} c ON c.id = l.category_id WHERE {base_where} GROUP BY c.id ORDER BY count DESC, c.name",
        base_args,
    )).fetchall()
    return {
        "total": total,
        "items": [dict(r) for r in rows],
        "categories": [dict(r) for r in categories],
    }


@router.post("/provider-only/lock")
async def provider_only_lock(req: LockRequest):
    """Lock (or unlock) the given ids, or every title matching the current filter."""
    table = _PROVIDER_ONLY.get(req.kind, (None,))[0]
    if not table:
        raise HTTPException(400, detail="kind must be movie or series")
    db = await get_db()
    if req.all_matching:
        where, args = _provider_only_where(req.kind, req.state, req.category_id, req.q)
        cur = await db.execute(
            f"UPDATE {table} SET tmdb_locked = ? WHERE id IN (SELECT t.id FROM {table} t WHERE {where})",
            (int(req.locked), *args))
        updated = cur.rowcount
    else:
        ids = req.ids or []
        updated = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            cur = await db.execute(
                f"UPDATE {table} SET tmdb_locked = ? WHERE id IN ({','.join('?' * len(chunk))})",
                (int(req.locked), *chunk))
            updated += cur.rowcount
    await db.commit()
    return {"updated": updated}
