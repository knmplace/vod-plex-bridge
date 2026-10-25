"""
TMDB Local -- every TMDB call the bridge makes goes through here, so a title
is fetched from TMDB once and answered from the local store (tmdb_store)
afterwards. Ported from VOD Manager's tmdb_sync + tmdb_fill.

Opt-in: the store is used only once it is turned on in the TMDB Local tab.
Off, every lookup goes straight to TMDB and nothing is stored (the bridge's
behaviour before the store existed).

Background (fill_loop), each cycle as due:
  - import TMDB's daily ID export (keyless file download, once a day);
  - mark stored titles TMDB reports as changed (/movie/changes, /tv/changes);
  - retention: remove anything at the hard cache age;
  - a small burst: fetch missing, changed or aging titles the catalog
    references, then the most popular TMDB titles, under a daily budget;
  - then match catalog movies without a TMDB ID (local first).
"""

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import config
import tmdb_store

logger = logging.getLogger(__name__)

_API_BASE = "https://api.themoviedb.org/3"
_EXPORT_URL = "https://files.tmdb.org/p/exports/{kind}_ids_{date}.json.gz"
_EXPORT_KINDS = {"movie": "movie", "tv": "tv_series"}
_DAY = 86400
_POLL_SECONDS = 300
_CHANGES_MAX_DAYS = 14  # TMDB's /changes window limit
_KEEP_TOP_RESULTS = 5

# The append set is the union every caller needs, so one stored payload answers all of them.
_STORE_APPEND = {
    "movie": "credits,release_dates,alternative_titles",
    "tv": "credits,content_ratings,external_ids,alternative_titles",
}

SETTINGS_KEY = "TMDB_STORE"
SETTINGS_DEFAULTS = {
    "enabled": False,
    "burst_size": 2000,
    "bursts_per_day": 4,
    "daily_budget": 20000,
    "concurrency": 4,
    "auto_match_batch": 2000,  # no-ID movies matched per burst; 0 = off
    "popular_keep": 25000,  # also keep the N most popular TMDB titles per type; 0 = catalog only
}

# Process-wide cap on concurrent TMDB requests, shared by every caller.
_GLOBAL_TMDB_CONCURRENCY = 40
_tmdb_semaphore = asyncio.Semaphore(_GLOBAL_TMDB_CONCURRENCY)
_tmdb_client = httpx.AsyncClient(
    timeout=30.0,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=_GLOBAL_TMDB_CONCURRENCY, max_keepalive_connections=_GLOBAL_TMDB_CONCURRENCY),
)
_TMDB_BACKOFF_STATUS_CODES = {429, 503}
_TMDB_BACKOFF_SECONDS = 5.0
_tmdb_backoff_until = 0.0
_API_KEY_RE = re.compile(r"(api_key=)[^&\s'\"]+")

_lock = asyncio.Lock()
status: dict = {"running": False, "phase": None, "done": 0, "total": 0, "last_result": None, "last_error": None}
_settings_cache: dict | None = None


class TmdbNotFoundError(Exception):
    """The TMDB id no longer exists (HTTP 404)."""


def _redact(exc) -> str:
    """Never let an api_key in a request URL reach logs or API responses."""
    return _API_KEY_RE.sub(r"\1***", str(exc))


def has_credentials() -> bool:
    return bool(config.TMDB_API_KEY or config.TMDB_READ_TOKEN)


def _auth() -> tuple[dict, dict]:
    """(params, headers) for a TMDB request: the read token when set, else the API key."""
    if config.TMDB_READ_TOKEN:
        return {}, {"Authorization": f"Bearer {config.TMDB_READ_TOKEN}"}
    return {"api_key": config.TMDB_API_KEY}, {}


async def _tmdb_get(url: str, params: dict | None = None) -> httpx.Response:
    """GET through the shared client with auth, honouring a brief backoff after 429/503."""
    global _tmdb_backoff_until
    now = time.time()
    if now < _tmdb_backoff_until:
        await asyncio.sleep(_tmdb_backoff_until - now)
    auth_params, headers = _auth()
    async with _tmdb_semaphore:
        r = await _tmdb_client.get(url, params={**auth_params, **(params or {})}, headers=headers)
    if r.status_code in _TMDB_BACKOFF_STATUS_CODES:
        _tmdb_backoff_until = time.time() + _TMDB_BACKOFF_SECONDS
    return r


# --- settings (bridge app_settings table, one JSON value) ---

def _db_path() -> str:
    from database import DB_PATH
    return DB_PATH


def get_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        stored = {}
        try:
            with sqlite3.connect(_db_path()) as conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
            stored = json.loads(row[0]) if row and row[0] else {}
        except (sqlite3.Error, ValueError):
            pass
        _settings_cache = {k: stored.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}
    return dict(_settings_cache)


def save_settings(updates: dict) -> dict:
    global _settings_cache
    current = get_settings()
    for key, value in updates.items():
        if key not in SETTINGS_DEFAULTS or value is None:
            continue
        current[key] = bool(value) if key == "enabled" else max(0, int(value))
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                     (SETTINGS_KEY, json.dumps(current)))
    _settings_cache = current
    return dict(current)


def store_enabled() -> bool:
    return bool(get_settings()["enabled"])


def list_catalog_tmdb_ids() -> dict[str, set[int]]:
    """TMDB ids the bridge catalog references, by store media type."""
    out: dict[str, set[int]] = {"movie": set(), "tv": set()}
    with sqlite3.connect(_db_path()) as conn:
        for media_type, table in (("movie", "movies"), ("tv", "series")):
            try:
                rows = conn.execute(f"SELECT DISTINCT tmdb_id FROM {table} WHERE tmdb_id IS NOT NULL AND tmdb_id != ''")
            except sqlite3.Error:
                continue
            out[media_type] = {int(r[0]) for r in rows if str(r[0]).strip().isdigit()}
    return out


# --- lookups (local first) ---

async def note_lookup(local: bool) -> None:
    if not store_enabled():
        return
    try:
        await asyncio.to_thread(tmdb_store.count_lookup, local)
    except Exception as exc:
        logger.debug("[tmdb_local] lookup counter write failed: %s", exc)


async def fetch_title_payload(media_type: str, tmdb_id, *, use_store: bool = True,
                              count: bool = True) -> dict | None:
    """Full /movie/{id} or /tv/{id} payload: local store first, then TMDB
    (stored on success). Raises TmdbNotFoundError on a TMDB 404; returns None
    on any other failure or when no TMDB credentials are set."""
    payload, _ = await fetch_title(media_type, tmdb_id, use_store=use_store, count=count)
    return payload


async def fetch_title(media_type: str, tmdb_id, *, use_store: bool = True,
                      count: bool = True) -> tuple[dict | None, bool]:
    """fetch_title_payload, plus whether it was answered locally."""
    key = str(tmdb_id).strip()
    local_ok = key.isdigit() and store_enabled()
    if use_store and local_ok:
        try:
            cached = await asyncio.to_thread(tmdb_store.get_payload, media_type, int(key))
        except Exception as exc:
            logger.warning("[tmdb_local] store read failed for %s %s: %s", media_type, key, exc)
            cached = None
        if cached is not None:
            if count:
                await note_lookup(True)
            return cached, True
    if not has_credentials():
        return None, False
    if use_store and count:
        await note_lookup(False)
    try:
        r = await _tmdb_get(f"{_API_BASE}/{media_type}/{key}", {"append_to_response": _STORE_APPEND[media_type]})
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("[tmdb_local] %s %s fetch failed: %s", media_type, key, _redact(exc))
        if exc.response.status_code == 404:
            if local_ok:
                try:
                    await asyncio.to_thread(tmdb_store.mark_gone, media_type, int(key))
                except Exception:
                    pass
            raise TmdbNotFoundError(key) from exc
        return None, False
    except Exception as exc:
        logger.warning("[tmdb_local] %s %s fetch failed: %s", media_type, key, _redact(exc))
        return None, False
    data = r.json()
    if local_ok and data.get("id") is not None:
        try:
            await asyncio.to_thread(tmdb_store.upsert_payload, media_type, data)
            await asyncio.to_thread(tmdb_store.add_requests, 1)
        except Exception as exc:
            logger.warning("[tmdb_local] store write failed for %s %s: %s", media_type, key, exc)
    return data, False


async def movie_details(tmdb_id) -> tuple[dict | None, bool]:
    """(payload or None, answered_locally) for one movie; a 404 is None."""
    try:
        return await fetch_title("movie", tmdb_id)
    except TmdbNotFoundError:
        return None, False


async def store_titles(media_type: str, tmdb_ids) -> None:
    """Save full details for titles not stored yet (one TMDB call each, once)."""
    if not store_enabled():
        return

    async def _one(tmdb_id) -> None:
        try:
            await fetch_title_payload(media_type, tmdb_id, count=False)
        except Exception as exc:
            logger.debug("[tmdb_local] could not save %s %s: %s", media_type, tmdb_id, _redact(exc))

    ids = [i for i in dict.fromkeys(tmdb_ids) if i is not None]
    missing = [i for i in ids if await asyncio.to_thread(tmdb_store.get_title, media_type, int(i)) is None]
    await asyncio.gather(*[_one(i) for i in missing])


async def tmdb_search(media_type: str, query: str, year: int | None = None) -> tuple[dict, bool]:
    """TMDB /search/{movie|tv}: the kept answer, else asked and kept (with its
    top results' details saved). Returns (response, answered_locally)."""
    on = store_enabled()
    kind = f"search/{media_type}"
    if on:
        try:
            kept = await asyncio.to_thread(tmdb_store.get_search, kind, query, year)
        except Exception as exc:
            logger.warning("[tmdb_local] kept-answer read failed for %r: %s", query, exc)
            kept = None
        if kept is not None:
            await note_lookup(True)
            return kept, True
        await note_lookup(False)
    params = {"query": query}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    r = await _tmdb_get(f"{_API_BASE}/search/{media_type}", params)
    r.raise_for_status()
    data = r.json()
    if on:
        ids = [i.get("id") for i in data.get("results") or [] if i.get("id") is not None]
        try:
            await asyncio.to_thread(tmdb_store.add_requests, 1)
            await asyncio.to_thread(tmdb_store.put_search, kind, query, year, data, not ids)
        except Exception as exc:
            logger.warning("[tmdb_local] kept-answer write failed for %r: %s", query, exc)
        await store_titles(media_type, ids[:_KEEP_TOP_RESULTS])
    return data, False


async def search_movie(query: str, year: int | None = None) -> tuple[list[dict], bool]:
    """TMDB-shaped /search/movie results for title matching: the local store
    when its answer is complete (search_confident), else the kept/live search.
    Returns (results, answered_locally)."""
    if store_enabled():
        try:
            hits = await asyncio.to_thread(tmdb_store.search_confident, "movie", query, year, _KEEP_TOP_RESULTS)
        except Exception as exc:
            logger.warning("[tmdb_local] local search failed for %r: %s", query, exc)
            hits = None
        if hits:
            results = []
            for h in hits:
                payload = await asyncio.to_thread(tmdb_store.get_payload, "movie", h["tmdb_id"])
                if payload:
                    results.append(payload)
            if results:
                await note_lookup(True)
                return results, True
    data, kept = await tmdb_search("movie", query, year)
    return data.get("results") or [], kept


# --- background fill ---

def _set(**kw) -> None:
    status.update(kw)


async def import_exports() -> dict:
    """Download today's (or the most recent available) daily ID export per type."""
    out = {}
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        for media_type, kind in _EXPORT_KINDS.items():
            _set(phase=f"downloading {media_type} id export")
            out[media_type] = None
            for days_back in range(3):
                day = datetime.now(timezone.utc) - timedelta(days=days_back)
                url = _EXPORT_URL.format(kind=kind, date=day.strftime("%m_%d_%Y"))
                path = Path(tmdb_store.DB_PATH).parent / f"tmdb_export_{media_type}.json.gz"
                try:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            continue
                        with open(path, "wb") as fh:
                            async for chunk in resp.aiter_bytes(1 << 16):
                                fh.write(chunk)
                    _set(phase=f"importing {media_type} id export")
                    count = await asyncio.to_thread(tmdb_store.import_export_file, media_type, path)
                    await asyncio.to_thread(tmdb_store.set_meta, f"export_date:{media_type}", day.strftime("%Y-%m-%d"))
                    out[media_type] = count
                    logger.info("[tmdb_local] imported %d %s ids from %s export", count, media_type, day.date())
                    break
                except Exception as exc:
                    logger.warning("[tmdb_local] %s export %s failed: %s", media_type, day.date(), exc)
                finally:
                    path.unlink(missing_ok=True)
    await asyncio.to_thread(tmdb_store.set_meta, "exports_imported_at", time.time())
    return out


async def refresh_changes() -> dict:
    """Flag stored titles TMDB changed since the last check for refetch."""
    if not has_credentials():
        return {}
    now = datetime.now(timezone.utc)
    out = {}
    for media_type in tmdb_store.MEDIA_TYPES:
        _set(phase=f"checking {media_type} changes")
        last = float(await asyncio.to_thread(tmdb_store.get_meta, f"changes_checked:{media_type}", "0") or 0)
        start = now - timedelta(days=1) if not last else max(
            datetime.fromtimestamp(last, timezone.utc), now - timedelta(days=_CHANGES_MAX_DAYS)
        )
        ids: list[int] = []
        page, pages = 1, 1
        while page <= pages:
            r = await _tmdb_get(f"{_API_BASE}/{media_type}/changes", {
                "page": page, "start_date": start.strftime("%Y-%m-%d"), "end_date": now.strftime("%Y-%m-%d"),
            })
            r.raise_for_status()
            data = r.json()
            ids += [int(item["id"]) for item in data.get("results", []) if item.get("id") is not None]
            pages = min(int(data.get("total_pages") or 1), 500)
            page += 1
        await asyncio.to_thread(tmdb_store.add_requests, page - 1)
        marked = await asyncio.to_thread(tmdb_store.mark_changed, media_type, ids)
        await asyncio.to_thread(tmdb_store.set_meta, f"changes_checked:{media_type}", now.timestamp())
        out[media_type] = {"changed_on_tmdb": len(ids), "stored_marked": max(marked, 0)}
    return out


async def run_burst(size: int | None = None) -> dict:
    """Fill/refresh catalog TMDB IDs first, then the most popular TMDB titles."""
    settings = get_settings()
    size = settings["burst_size"] if size is None else size
    budget_left = max(0, settings["daily_budget"] - await asyncio.to_thread(tmdb_store.requests_today))
    size = min(size, budget_left)
    if size <= 0:
        return {"fetched": 0, "failed": 0, "gone": 0, "skipped": "daily budget reached"}
    per_type = math.ceil(size / len(tmdb_store.MEDIA_TYPES))
    work: list[tuple[str, int]] = []
    referenced = await asyncio.to_thread(list_catalog_tmdb_ids)
    for media_type in tmdb_store.MEDIA_TYPES:
        due = await asyncio.to_thread(
            tmdb_store.list_catalog_refresh_ids, media_type, referenced.get(media_type, set()), per_type,
        )
        work += [(media_type, i) for i in due]
    work = work[:size]
    popular = 0
    room = size - len(work)
    if room > 0 and settings.get("popular_keep", 0) > 0:
        per_type = math.ceil(room / len(tmdb_store.MEDIA_TYPES))
        for media_type in tmdb_store.MEDIA_TYPES:
            top = await asyncio.to_thread(tmdb_store.popular_export_ids, media_type, settings["popular_keep"])
            top -= referenced.get(media_type, set())
            due = await asyncio.to_thread(tmdb_store.list_catalog_refresh_ids, media_type, top, per_type)
            due = due[:max(0, size - len(work))]
            work += [(media_type, i) for i in due]
            popular += len(due)
    counts = {"fetched": 0, "failed": 0, "gone": 0, "popular": popular}
    _set(phase="filling", done=0, total=len(work))
    sem = asyncio.Semaphore(max(1, settings["concurrency"]))

    async def one(media_type: str, tmdb_id: int) -> None:
        async with sem:
            try:
                payload = await fetch_title_payload(media_type, tmdb_id, use_store=False)
                counts["fetched" if payload is not None else "failed"] += 1
            except TmdbNotFoundError:
                counts["gone"] += 1
            except Exception as exc:
                counts["failed"] += 1
                logger.warning("[tmdb_local] %s %s failed: %s", media_type, tmdb_id, _redact(exc))
            status["done"] += 1

    await asyncio.gather(*[one(t, i) for t, i in work])
    logger.info("[tmdb_local] burst: %s", counts)
    return counts


async def run_auto_match(limit: int | None = None) -> dict:
    """Give catalog movies without a TMDB ID one (the bridge's title search, local first)."""
    from scraper import search_tmdb_for_missing
    if limit is None:
        limit = get_settings()["auto_match_batch"]
    if limit <= 0:
        return {"found": 0, "not_found": 0, "skipped": 0}
    _set(phase="matching titles without a TMDB ID")
    return await search_tmdb_for_missing(batch_size=limit)


async def retention(dry_run: bool = True) -> dict:
    """Preview or enforce cache-age retention, then compact the store file."""
    referenced = await asyncio.to_thread(list_catalog_tmdb_ids)
    result = await asyncio.to_thread(tmdb_store.retention_cleanup, referenced, dry_run=dry_run)
    if not dry_run:
        result["vacuumed"] = await asyncio.to_thread(tmdb_store.vacuum_if_fragmented)
    if not dry_run and any(result.get(key, 0) for key in (
        "titles_removed", "seasons_removed", "searches_removed", "gone_removed",
    )):
        logger.info("[tmdb_local] retention cleanup: %s", result)
    return result


async def _run(phase: str, coro) -> dict:
    async with _lock:
        _set(running=True, phase=phase, done=0, total=0, last_error=None)
        try:
            result = await coro
            _set(last_result={"job": phase, "at": time.time(), **(result or {})})
            return result
        except Exception as exc:
            _set(last_error=f"{phase}: {_redact(exc)}")
            logger.warning("[tmdb_local] %s failed: %s", phase, _redact(exc))
            return {"error": str(status["last_error"])}
        finally:
            _set(running=False, phase=None)


async def run_job(job: str) -> dict:
    jobs = {"export": import_exports, "changes": refresh_changes, "burst": run_burst, "match": run_auto_match}
    return await _run(job, jobs[job]())


async def run_due_jobs() -> bool:
    """One scheduler tick. Returns False when the store is inactive."""
    settings = get_settings()
    if not has_credentials() or not settings["enabled"]:
        return False
    now = time.time()
    meta = lambda key: float(tmdb_store.get_meta(key, "0") or 0)  # noqa: E731
    if now - await asyncio.to_thread(meta, "exports_imported_at") >= _DAY:
        await _run("export", import_exports())
    if now - await asyncio.to_thread(meta, "retention_run_at") >= _DAY:
        retention_result = await _run("retention", retention(dry_run=False))
        if "error" not in retention_result:
            await asyncio.to_thread(tmdb_store.set_meta, "retention_run_at", now)
    if now - await asyncio.to_thread(meta, "changes_run_at") >= _DAY:
        await _run("changes", refresh_changes())
        await asyncio.to_thread(tmdb_store.set_meta, "changes_run_at", now)
    if now >= await asyncio.to_thread(meta, "next_burst_at"):
        await _run("burst", run_burst())
        if settings["auto_match_batch"]:
            await _run("match", run_auto_match())
        interval = _DAY / max(1, settings["bursts_per_day"])
        await asyncio.to_thread(tmdb_store.set_meta, "next_burst_at", time.time() + interval)
    return True


async def fill_loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await run_due_jobs()
        except Exception as exc:
            logger.warning("[tmdb_local] scheduler tick failed: %s", _redact(exc))
        await asyncio.sleep(_POLL_SECONDS)


# --- TMDB Local page lookup ---

def _present(row: dict) -> dict:
    row = dict(row)
    path = row.pop("poster_path", None)
    row["poster_url"] = f"https://image.tmdb.org/t/p/w185{path}" if path else None
    return row


def _row_from_tmdb(media_type: str, item: dict) -> dict:
    """A TMDB detail payload or /search item in the stored-row shape _present takes."""
    is_movie = media_type == "movie"
    date = item.get("release_date" if is_movie else "first_air_date") or None
    cast = [c.get("name") for c in (item.get("credits") or {}).get("cast", [])[:5] if c.get("name")]
    return {
        "media_type": media_type,
        "tmdb_id": int(item["id"]),
        "title": item.get("title" if is_movie else "name"),
        "original_title": item.get("original_title" if is_movie else "original_name"),
        "year": tmdb_store._year(date),
        "release_date": date,
        "poster_path": item.get("poster_path") or None,
        "overview": item.get("overview") or None,
        "vote_average": item.get("vote_average") or None,
        "popularity": item.get("popularity"),
        "content_rating": tmdb_store._us_rating(media_type, item),
        "top_cast": ", ".join(cast) or None,
        "genres": ", ".join(g["name"] for g in item.get("genres") or [] if g.get("name")) or None,
    }


async def _live_lookup(query: str, media_type: str, year: int | None) -> dict:
    """Store off: straight to TMDB, nothing stored."""
    if query.isdigit():
        try:
            data = await fetch_title_payload(media_type, query)
        except TmdbNotFoundError:
            data = None
        return {"source": "tmdb", "results": [_present(_row_from_tmdb(media_type, data))] if data else []}
    if not has_credentials() or not query:
        return {"source": "tmdb", "results": []}
    data, _ = await tmdb_search(media_type, query, year)
    items = [i for i in data.get("results", [])[:10] if i.get("id") is not None]
    return {"source": "tmdb", "results": [_present(_row_from_tmdb(media_type, i)) for i in items]}


async def lookup(query: str, media_type: str, year: int | None = None, limit: int = 10) -> dict:
    """Local first; on a miss, live TMDB search whose top results get stored.
    With the store off, always live and nothing is stored."""
    query = (query or "").strip()
    if not store_enabled():
        return await _live_lookup(query, media_type, year)
    if query.isdigit():
        local = await asyncio.to_thread(tmdb_store.get_title, media_type, int(query))
        if local:
            return {"source": "local", "results": [_present(local)]}
        try:
            await fetch_title_payload(media_type, query)
        except TmdbNotFoundError:
            return {"source": "tmdb", "results": []}
        row = await asyncio.to_thread(tmdb_store.get_title, media_type, int(query))
        return {"source": "tmdb", "results": [_present(row)] if row else []}

    hits = await asyncio.to_thread(tmdb_store.search, media_type, query, year, limit)
    if hits:
        return {"source": "local", "results": [_present(h) for h in hits]}
    if not has_credentials() or not query:
        return {"source": "local", "results": []}
    data, kept = await tmdb_search(media_type, query, year)
    ids = [int(item["id"]) for item in data.get("results", [])[:_KEEP_TOP_RESULTS] if item.get("id") is not None]
    await store_titles(media_type, ids)  # no-op for titles already saved
    rows = [await asyncio.to_thread(tmdb_store.get_title, media_type, i) for i in ids]
    return {"source": "local" if kept else "tmdb", "results": [_present(r) for r in rows if r]}
