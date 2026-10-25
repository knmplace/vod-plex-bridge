"""API for the TMDB Local page: status/stats, lookup, settings, manual runs, clear."""

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import tmdb_local
import tmdb_store

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
