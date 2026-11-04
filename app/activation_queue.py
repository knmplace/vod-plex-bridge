"""Movie activation queue.

Activation opens one provider connection per movie (head+tail fetch), so movies are
activated one at a time, 5 seconds apart. Before each one, the provider accounts that can
serve it (primary stream, then alternates) are checked for a free connection slot. When
none has one, the movie waits and is checked again every RECHECK_SECS, instead of failing
the fetch and being marked dead.

The queue is the activation_queue table, so it survives a restart. One worker task
(start_worker, from main.py's lifespan) works through it.

Series use the same worker through series_activation_queue: one row per show with the
episodes picked in the Activate wizard. Adding episodes opens no provider connection (the
account is picked when an episode is played), so shows are not spaced or slot-checked; they
are queued so a large batch runs in the background and reports progress in the same pill.
"""
import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
from redirect import account_slots, slots_free
import cache_fill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

RECHECK_SECS = 600   # how long a movie waits for a free slot before it is checked again
SPACING_SECS = 5     # pause between two activations (matches the old bulk-activate pacing)
FILL_BEFORE_SCAN_SECS = 900  # longest a series Plex scan waits for new episodes to be cached
IDLE_POLL_SECS = 60  # worker wakes at least this often to look for waiting movies that are due

_wake = asyncio.Event()
_running: dict | None = None   # {"movie_id", "name"} while one is being activated
_seq = 0
_recent: list[dict] = []        # last results, newest last; the UI polls them by seq
_batch = {"activated": 0, "failed": 0}  # since the last enqueue into an empty queue
_next_at = 0.0                  # when the pause between two activations ends

_series_running: dict | None = None   # {"series_id", "name"} while its episodes are added
_series_recent: list[dict] = []
_series_batch = {"shows": 0, "episodes": 0, "failed": 0}
_series_scan: set[str] = set()        # Plex sections to scan once the series queue is empty


def _remember(movie_id: int, name: str, ok: bool, category_id=None, reason: str = ""):
    global _seq
    _seq += 1
    _recent.append({"seq": _seq, "movie_id": movie_id, "name": name, "ok": ok,
                    "category_id": category_id, "reason": reason})
    del _recent[:-100]
    _batch["activated" if ok else "failed"] += 1


def _streams(movie: dict) -> list[dict]:
    """Primary stream followed by the alternates, in the alt_streams JSON shape."""
    out = []
    if movie.get("stream_id"):
        out.append({"stream_id": movie["stream_id"], "ext": movie.get("container_extension") or "mkv",
                    "account_id": movie.get("account_id"), "account_name": movie.get("account_name") or ""})
    try:
        out.extend(json.loads(movie.get("alt_streams") or "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    return out


async def enqueue(movie_ids: list[int], category) -> int:
    """Add movies that are not active yet. Returns how many were added."""
    db = await get_db()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = await db.execute(f"SELECT id FROM movies WHERE id IN ({placeholders}) AND activated = 0", movie_ids)
    ids = [r["id"] for r in await rows.fetchall()]
    left = await db.execute("SELECT COUNT(*) AS n FROM activation_queue")
    if ids and (await left.fetchone())["n"] == 0 and _running is None:
        _batch.update(activated=0, failed=0)  # a new batch starts
    now = time.time()
    for i, mid in enumerate(ids):
        # A movie already queued keeps its place but takes the new category.
        await db.execute(
            "INSERT INTO activation_queue (movie_id, category, status, queued_at) VALUES (?, ?, 'pending', ?) "
            "ON CONFLICT(movie_id) DO UPDATE SET category = excluded.category, "
            "status = CASE WHEN status = 'running' THEN status ELSE 'pending' END, next_check_at = 0",
            (mid, None if category is None else str(category), now + i * 1e-3),
        )
    await db.commit()
    if ids:
        _wake.set()
    return len(ids)


async def _next_item(db) -> dict | None:
    row = await db.execute(
        "SELECT * FROM activation_queue WHERE status != 'running' AND next_check_at <= ? "
        "ORDER BY queued_at LIMIT 1", (time.time(),))
    r = await row.fetchone()
    return dict(r) if r else None


async def _process(item: dict) -> bool:
    """Activate one queued movie. Returns True when a provider connection was used."""
    global _running
    from api import _fetch_headers_validated, _set_movie_category

    db = await get_db()
    movie_id = item["movie_id"]
    row = await db.execute("SELECT * FROM movies WHERE id = ?", (movie_id,))
    movie = await row.fetchone()
    if not movie or movie["activated"]:
        await db.execute("DELETE FROM activation_queue WHERE movie_id = ?", (movie_id,))
        await db.commit()
        return False
    movie = dict(movie)
    name = movie.get("name") or f"Movie {movie_id}"

    streams = _streams(movie)
    if not streams:
        await db.execute("DELETE FROM activation_queue WHERE movie_id = ?", (movie_id,))
        await db.commit()
        _remember(movie_id, name, False, reason="no provider stream mapped")
        return False

    # Accounts with a free slot go first; full ones stay in the list as a last resort,
    # so a movie is only marked dead after every stream really failed.
    async with httpx.AsyncClient() as client:
        free = {}
        for s in streams:
            acct = s.get("account_id")
            if acct not in free:
                free[acct] = await account_slots(client, acct)
    if not any(slots_free(free[s.get("account_id")]) for s in streams):
        names = sorted({free[s.get("account_id")]["name"] or s.get("account_name") or f"account {s.get('account_id')}"
                        for s in streams})
        await db.execute(
            "UPDATE activation_queue SET status = 'waiting', next_check_at = ?, attempts = attempts + 1, note = ? "
            "WHERE movie_id = ?",
            (time.time() + RECHECK_SECS, "No free slot on " + ", ".join(names), movie_id))
        await db.commit()
        logger.info("Activation queue: %s waiting for a free slot (%s)", name, ", ".join(names))
        return False

    ordered = ([s for s in streams if slots_free(free[s.get("account_id")])]
               + [s for s in streams if not slots_free(free[s.get("account_id")])])
    first = ordered[0]
    ext = first.get("ext") or "mkv"
    movie.update({"stream_id": first.get("stream_id"), "account_id": first.get("account_id"),
                  "account_name": first.get("account_name", ""), "container_extension": ext,
                  "content_type": "video/mp4" if ext == "mp4" else "video/x-matroska",
                  "alt_streams": json.dumps(ordered[1:])})

    await db.execute("UPDATE activation_queue SET status = 'running', note = NULL WHERE movie_id = ?", (movie_id,))
    await db.commit()
    _running = {"movie_id": movie_id, "name": name}
    try:
        category = item["category"]
        if category is None:
            err = await _set_movie_category(db, [movie_id], "auto", "activated = 0 AND category_id IS NULL")
        else:
            err = await _set_movie_category(db, [movie_id], category, "activated = 0")
        if err:
            await db.execute("DELETE FROM activation_queue WHERE movie_id = ?", (movie_id,))
            await db.commit()
            _remember(movie_id, name, False, reason=err)
            return False

        activated, _failed = await _fetch_headers_validated([movie], plex_scan=False)
        ok = movie_id in activated
        cat = await db.execute("SELECT category_id FROM movies WHERE id = ?", (movie_id,))
        cat_row = await cat.fetchone()
        await db.execute("DELETE FROM activation_queue WHERE movie_id = ?", (movie_id,))
        await db.commit()
        _remember(movie_id, name, ok, cat_row["category_id"] if cat_row else None,
                  "" if ok else "could not be played, marked dead")
        if ok:
            _to_scan.append(movie_id)
        return True
    finally:
        _running = None


_to_scan: list[int] = []


async def _process_series(item: dict):
    """Add one queued show's episodes. No provider connection is used."""
    global _series_running, _seq
    from api import activate_episode_rows, write_series_files, series_scan_section

    db = await get_db()
    sid = item["series_id"]
    row = await (await db.execute("SELECT name FROM series WHERE id = ?", (sid,))).fetchone()
    name = (row["name"] if row else "") or f"Series {sid}"
    cat = await (await db.execute("SELECT * FROM series_categories WHERE id = ?", (item["category_id"],))).fetchone()
    await db.execute("UPDATE series_activation_queue SET status = 'running' WHERE series_id = ?", (sid,))
    await db.commit()
    _series_running = {"series_id": sid, "name": name}
    activated, failed = [], []
    try:
        if not row or not cat:
            reason = "show no longer in the catalog" if not row else "library no longer exists"
            failed = [{"reason": reason}]
        else:
            category = dict(cat)
            try:
                ids = [int(i) for i in json.loads(item["episode_ids"] or "[]")]
            except (ValueError, TypeError):
                ids = []
            activated, failed, series_ids = await activate_episode_rows(db, ids)
            if activated:
                await write_series_files(db, series_ids, category)
                section = series_scan_section(category)
                if section:
                    _series_scan.add(section)
        await db.execute("DELETE FROM series_activation_queue WHERE series_id = ?", (sid,))
        await db.commit()
        total = await (await db.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE series_id = ? AND activated = 1", (sid,))).fetchone()
        _seq += 1
        _series_recent.append({"seq": _seq, "series_id": sid, "name": name, "activated": len(activated),
                               "failed": len(failed), "active_total": total["n"],
                               "reason": failed[0]["reason"] if failed else ""})
        del _series_recent[:-100]
        _series_batch["shows"] += 1
        _series_batch["episodes"] += len(activated)
        _series_batch["failed"] += len(failed)
        logger.info("Series queue: %s, %d episodes added, %d failed", name, len(activated), len(failed))
    finally:
        _series_running = None


async def _next_series(db) -> dict | None:
    row = await db.execute(
        "SELECT * FROM series_activation_queue WHERE status != 'running' ORDER BY queued_at LIMIT 1")
    r = await row.fetchone()
    return dict(r) if r else None


async def _fill_one(kind: str) -> bool:
    """One cache-fill step. True when the loop should come straight back for more."""
    global _next_at
    try:
        used = await cache_fill.fill_next(kind)
    except Exception as e:
        logger.warning("Cache fill (%s) error: %s", kind, e)
        return False
    if used:
        _next_at = time.time() + SPACING_SECS
        await asyncio.sleep(SPACING_SECS)
    return used is not None


async def _worker():
    global _next_at
    _scan_waiting_since = 0.0
    db = await get_db()
    await db.execute("UPDATE activation_queue SET status = 'pending' WHERE status = 'running'")
    await db.execute("UPDATE series_activation_queue SET status = 'pending' WHERE status = 'running'")
    await db.commit()
    while True:
        try:
            _wake.clear()  # before looking, so an enqueue during this pass is not missed
            show = await _next_series(db)
            if show:
                try:
                    await _process_series(show)
                except Exception as e:
                    # Drop the row so one bad show cannot block the queue forever.
                    logger.error("Series queue: series %s failed: %s", show["series_id"], e)
                    await db.execute("DELETE FROM series_activation_queue WHERE series_id = ?", (show["series_id"],))
                    await db.commit()
                continue
            # Cache new episodes before Plex scans them, so its analysis reads the cache.
            # Waits at most FILL_BEFORE_SCAN_SECS, so a big show doesn't hold the scan back.
            if _series_scan and not _scan_waiting_since:
                _scan_waiting_since = time.time()
            if not _series_scan or time.time() - _scan_waiting_since < FILL_BEFORE_SCAN_SECS:
                if await _fill_one("episode"):
                    continue
            if _series_scan:
                _scan_waiting_since = 0.0
                from api import _plex_scan_library
                sections = sorted(_series_scan)
                _series_scan.clear()
                for section in sections:
                    try:
                        await _plex_scan_library(section)
                    except Exception as e:
                        logger.warning("Series queue: Plex scan of section %s failed: %s", section, e)
            item = await _next_item(db)
            if item:
                used_provider = await _process(item)
                if used_provider:
                    _next_at = time.time() + SPACING_SECS
                    await asyncio.sleep(SPACING_SECS)
                continue
            if await _fill_one("movie"):
                continue
            if _to_scan:
                from api import _plex_scan_movie_sections
                ids = _to_scan[:]
                _to_scan.clear()
                await _plex_scan_movie_sections(ids)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=IDLE_POLL_SECS)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Activation queue worker error: %s", e)
            await asyncio.sleep(10)


def start_worker() -> asyncio.Task:
    return asyncio.create_task(_worker())


@router.get("/activation-queue")
async def queue_status(since: int = 0):
    db = await get_db()
    rows = await db.execute(
        "SELECT q.movie_id, q.status, q.next_check_at, q.attempts, q.note, m.name, m.year "
        "FROM activation_queue q LEFT JOIN movies m ON m.id = q.movie_id ORDER BY q.queued_at LIMIT 500")
    items = [dict(r) for r in await rows.fetchall()]
    waiting = [i for i in items if i["status"] == "waiting"]
    return {
        "running": _running,
        "pending": sum(1 for i in items if i["status"] == "pending"),
        "waiting": len(waiting),
        "next_check_at": min((i["next_check_at"] for i in waiting), default=None),
        # Between two activations nothing is running: name the next movie and when it starts.
        "next": next(({"movie_id": i["movie_id"], "name": i["name"], "at": max(_next_at, time.time())}
                      for i in items if i["status"] == "pending"), None),
        "batch": dict(_batch),
        "seq": _seq,
        "recent": [r for r in _recent if r["seq"] > since],
        "items": items,
        "series": await _series_status(db, since),
    }


async def _series_status(db, since: int) -> dict:
    rows = await db.execute(
        "SELECT q.series_id, q.status, q.episode_ids, s.name FROM series_activation_queue q "
        "LEFT JOIN series s ON s.id = q.series_id ORDER BY q.queued_at LIMIT 500")
    items = []
    for r in await rows.fetchall():
        try:
            n = len(json.loads(r["episode_ids"] or "[]"))
        except (ValueError, TypeError):
            n = 0
        items.append({"series_id": r["series_id"], "status": r["status"], "name": r["name"], "episodes": n})
    return {"running": _series_running, "items": items, "batch": dict(_series_batch),
            "recent": [r for r in _series_recent if r["seq"] > since]}


@router.post("/activation-queue/cancel")
async def queue_cancel(request: Request):
    """Remove queued movies (all when movie_ids is missing). The one being activated finishes."""
    body = await request.json()
    ids = body.get("movie_ids")
    db = await get_db()
    series_ids = body.get("series_ids")
    if series_ids is not None or body.get("all_series"):
        if series_ids:
            placeholders = ",".join("?" for _ in series_ids)
            cur = await db.execute(
                f"DELETE FROM series_activation_queue WHERE status != 'running' AND series_id IN ({placeholders})",
                series_ids)
        else:
            cur = await db.execute("DELETE FROM series_activation_queue WHERE status != 'running'")
        await db.commit()
        return {"status": "ok", "removed": cur.rowcount}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        cur = await db.execute(
            f"DELETE FROM activation_queue WHERE status != 'running' AND movie_id IN ({placeholders})", ids)
    else:
        cur = await db.execute("DELETE FROM activation_queue WHERE status != 'running'")
    await db.commit()
    return {"status": "ok", "removed": cur.rowcount}


@router.post("/activation-queue/check-now")
async def queue_check_now():
    """Check waiting movies for a free slot now instead of at their next check."""
    db = await get_db()
    await db.execute("UPDATE activation_queue SET next_check_at = 0 WHERE status = 'waiting'")
    await db.commit()
    _wake.set()
    return {"status": "ok"}


@router.post("/movies/slots")
async def movie_slots(request: Request):
    """Free provider slots for the accounts that would serve these movies (confirm popup)."""
    body = await request.json()
    ids = body.get("movie_ids") or []
    if not ids:
        return JSONResponse(status_code=400, content={"error": "movie_ids required"})
    db = await get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = await db.execute(
        f"SELECT id, stream_id, account_id, account_name, alt_streams, container_extension, activated "
        f"FROM movies WHERE id IN ({placeholders})", ids)
    movies = [dict(r) for r in await rows.fetchall()]
    per_movie = {m["id"]: {s.get("account_id") for s in _streams(m)} for m in movies if not m["activated"]}
    accounts = {a for accts in per_movie.values() for a in accts}
    async with httpx.AsyncClient() as client:
        slots = {a: await account_slots(client, a) for a in accounts}
    names = {}
    for m in movies:
        for s in _streams(m):
            names.setdefault(s.get("account_id"), s.get("account_name") or "")
    for a, s in slots.items():
        s["name"] = s["name"] or names.get(a) or f"account {a}"
    blocked = sum(1 for accts in per_movie.values() if accts and not any(slots_free(slots[a]) for a in accts))
    return {
        "to_activate": len(per_movie),
        "already_active": sum(1 for m in movies if m["activated"]),
        "no_stream": sum(1 for accts in per_movie.values() if not accts),
        "will_wait": blocked,
        "accounts": sorted(slots.values(), key=lambda s: s["name"].lower()),
        "spacing_secs": SPACING_SECS,
        "recheck_mins": RECHECK_SECS // 60,
    }

@router.get("/series/{series_id}/activation-info")
async def series_activation_info(series_id: int):
    """Everything the Activate wizard needs for one show. Loads the episode list from
    Dispatcharr's cached copy when it has never been loaded (no forced refresh)."""
    db = await get_db()
    s = await (await db.execute(
        "SELECT id, name, year, poster_url, category_id FROM series WHERE id = ?", (series_id,))).fetchone()
    if not s:
        return JSONResponse(status_code=404, content={"error": "Series not found"})
    sql = ("SELECT id, season_number, episode_number, name, activated, stream_id, account_id "
           "FROM episodes WHERE series_id = ? ORDER BY season_number, episode_number")
    rows = await (await db.execute(sql, (series_id,))).fetchall()
    load_error = ""
    if not rows:
        from series_scraper import fetch_series_episodes
        res = await fetch_series_episodes(series_id)
        if res.get("status") != "ok":
            load_error = res.get("message") or "could not load the episode list"
        rows = await (await db.execute(sql, (series_id,))).fetchall()
    seasons: dict[int, list] = {}
    for r in rows:
        seasons.setdefault(r["season_number"] or 0, []).append({
            "id": r["id"], "episode_number": r["episode_number"], "name": r["name"] or "",
            "activated": bool(r["activated"]), "mapped": bool(r["stream_id"] or r["account_id"])})
    active = sum(1 for r in rows if r["activated"])
    queued = await (await db.execute(
        "SELECT 1 FROM series_activation_queue WHERE series_id = ?", (series_id,))).fetchone()
    return {
        "series_id": series_id, "name": s["name"], "year": s["year"], "poster_url": s["poster_url"],
        # A show with active episodes stays in its library; the wizard locks the picker.
        "category_id": s["category_id"], "locked": bool(active and s["category_id"]),
        "episodes_total": len(rows), "active_total": active, "queued": bool(queued), "error": load_error,
        "seasons": [{"season_number": k, "episodes": v} for k, v in sorted(seasons.items())],
    }


@router.post("/series/activate-batch")
async def series_activate_batch(request: Request):
    """Queue shows from the Activate wizard: {shows: [{series_id, category_id, episode_ids}]}."""
    body = await request.json()
    shows = body.get("shows") or []
    if not shows:
        return JSONResponse(status_code=400, content={"error": "shows is required"})
    db = await get_db()
    cats = {r["id"] for r in await (await db.execute("SELECT id FROM series_categories")).fetchall()}
    plan = []
    for show in shows:
        try:
            sid = int(show["series_id"])
            ep_ids = sorted({int(e) for e in show.get("episode_ids") or []})
        except (KeyError, ValueError, TypeError):
            continue
        s = await (await db.execute("SELECT category_id FROM series WHERE id = ?", (sid,))).fetchone()
        if not s or not ep_ids:
            continue
        has_active = await (await db.execute(
            "SELECT 1 FROM episodes WHERE series_id = ? AND activated = 1 LIMIT 1", (sid,))).fetchone()
        category_id = s["category_id"] if has_active and s["category_id"] else show.get("category_id")
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"error": f"No library picked for series {sid}"})
        if category_id not in cats:
            return JSONResponse(status_code=400, content={"error": f"Library {category_id} not found"})
        # Only episodes of this show that are not active yet.
        placeholders = ",".join("?" for _ in ep_ids)
        rows = await db.execute(
            f"SELECT id FROM episodes WHERE series_id = ? AND activated = 0 AND id IN ({placeholders})",
            [sid, *ep_ids])
        ep_ids = [r["id"] for r in await rows.fetchall()]
        if ep_ids:
            plan.append((sid, category_id, ep_ids))

    busy = (await (await db.execute("SELECT COUNT(*) AS n FROM series_activation_queue")).fetchone())["n"]
    if plan and not busy and _series_running is None:
        _series_batch.update(shows=0, episodes=0, failed=0)  # a new batch starts
    now = time.time()
    episodes = 0
    for i, (sid, category_id, ep_ids) in enumerate(plan):
        old = await (await db.execute(
            "SELECT episode_ids FROM series_activation_queue WHERE series_id = ? AND status != 'running'",
            (sid,))).fetchone()
        if old:
            try:
                ep_ids = sorted(set(ep_ids) | {int(e) for e in json.loads(old["episode_ids"] or "[]")})
            except (ValueError, TypeError):
                pass
        await db.execute(
            "INSERT INTO series_activation_queue (series_id, category_id, episode_ids, status, queued_at) "
            "VALUES (?, ?, ?, 'pending', ?) ON CONFLICT(series_id) DO UPDATE SET "
            "category_id = excluded.category_id, episode_ids = excluded.episode_ids",
            (sid, category_id, json.dumps(ep_ids), now + i * 1e-3))
        episodes += len(ep_ids)
    await db.commit()
    if plan:
        _wake.set()
    return {"status": "ok", "queued_shows": len(plan), "queued_episodes": episodes}
