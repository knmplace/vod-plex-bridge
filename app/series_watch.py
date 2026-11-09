"""Series Watch List: notice episodes that appear (or air) after a show was activated.

A show is "watched" when it has at least one activated episode. For each season with
activated episodes the watcher compares what the bridge has against TMDB's per-episode air
dates, so a season stays on the list until every AIRED episode is activated, whatever the
show's status says (an Ended show with 10 of 12 activated is still listed). Continuing shows
are always listed, with the next air date.

Nothing here is a one-time sweep: the state is recomputed from the episodes table each time,
and the watcher never opens a provider connection. It only re-reads Dispatcharr's cached
provider-info (series_scraper.fetch_series_episodes) and TMDB's season lists.

Acting on new episodes (run_watch, after each series sync and in the Daily Run):
  * an episode first seen AFTER the show was activated (episodes.discovered_at later than the
    show's first activation) and already aired is "new";
  * a new episode in a season the show already has active is queued through the series
    activation queue (mode 'auto', the default) or just reported (mode 'notify');
  * a show with no library, or a whole new season, is reported only;
  * every episode is handled once (series_watch_events UNIQUE(kind, episode_id)), so a
    deactivated episode is never re-added.
Episodes already in the catalog when this shipped have discovered_at NULL and are never "new";
the user's earlier choices (e.g. 10 of 12 activated) are left alone and only listed.
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import get_db
import tmdb_local
from activation_queue import enqueue_series_episodes, wake_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MODES = ("auto", "notify", "off")
_last_run: dict = {}
_run_lock = asyncio.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _aired(air_date: str | None, today: str) -> bool:
    """TMDB/provider dates are YYYY-MM-DD. No date at all: the provider already lists it, so
    it counts as aired."""
    if not air_date:
        return True
    return air_date[:10] <= today


async def _active_shows(db, only: int | None = None) -> list[dict]:
    sql = ("SELECT s.id, s.name, s.year, s.tmdb_id, s.tmdb_status, s.category_id, "
           "COALESCE(p.mode, 'auto') AS mode "
           "FROM series s LEFT JOIN series_watch_prefs p ON p.series_id = s.id "
           "WHERE EXISTS (SELECT 1 FROM episodes e WHERE e.series_id = s.id AND e.activated = 1)")
    args: list = []
    if only is not None:
        sql += " AND s.id = ?"
        args.append(only)
    rows = await (await db.execute(sql + " ORDER BY s.name COLLATE NOCASE", args)).fetchall()
    return [dict(r) for r in rows]


async def _tmdb_view(show: dict, season_numbers) -> tuple[dict, dict | None]:
    """({season: {episode_number: air_date}} for the given seasons, next_episode_to_air).
    Empty when TMDB cannot answer; the watcher then relies on the bridge's own dates."""
    seasons: dict[int, dict[int, str | None]] = {}
    nxt = None
    tid = show.get("tmdb_id")
    if not tid:
        return seasons, nxt
    try:
        payload = await tmdb_local.fetch_title_payload("tv", tid, count=False)
    except Exception:
        payload = None
    if payload:
        n = payload.get("next_episode_to_air")
        if n and n.get("air_date"):
            nxt = {"date": n["air_date"], "season": n.get("season_number"), "episode": n.get("episode_number")}
    for sn in season_numbers:
        try:
            eps = await tmdb_local.fetch_season(tid, sn)
        except Exception:
            eps = None
        if eps:
            seasons[sn] = {e["episode_number"]: e.get("air_date") for e in eps if e.get("episode_number") is not None}
    return seasons, nxt


async def compute_show(db, show: dict, today: str | None = None) -> dict:
    """The watch state of one show. Pure read: no events written, nothing queued."""
    today = today or _today()
    sid = show["id"]
    eps = [dict(r) for r in await (await db.execute(
        "SELECT id, season_number, episode_number, air_date, activated, activated_at, discovered_at "
        "FROM episodes WHERE series_id = ? AND COALESCE(season_number, 0) > 0", (sid,))).fetchall()]
    first_active = await (await db.execute(
        "SELECT MIN(activated_at) AS t FROM episodes WHERE series_id = ? AND activated = 1", (sid,))).fetchone()
    baseline = first_active["t"] or ""

    by_season: dict[int, list[dict]] = {}
    for e in eps:
        by_season.setdefault(e["season_number"], []).append(e)
    active_seasons = sorted(sn for sn, lst in by_season.items() if any(x["activated"] for x in lst))
    all_seasons = sorted(by_season)
    tmdb_seasons, next_air = await _tmdb_view(show, all_seasons)

    status = show.get("tmdb_status") or "unknown"
    rows: list[dict] = []
    candidates: list[dict] = []      # new aired episodes in a season the show already has
    new_seasons: list[dict] = []     # seasons with new episodes but nothing activated yet

    for sn in all_seasons:
        lst = by_season[sn]
        have_eps = {x["episode_number"]: x for x in lst if x["episode_number"] is not None}
        tm = tmdb_seasons.get(sn, {})
        # Air date: TMDB first, then what the provider lists.
        dates = {n: tm.get(n) or (have_eps[n]["air_date"] if n in have_eps else None) for n in set(tm) | set(have_eps)}
        # No date: aired if the provider lists it, not owed if only TMDB lists it (unannounced).
        aired_nums = sorted(n for n, d in dates.items() if (d[:10] <= today if d else n in have_eps))
        upcoming = sorted((d, n) for n, d in dates.items() if d and d[:10] > today)
        activated = sorted(n for n, x in have_eps.items() if x["activated"])
        missing = [n for n in aired_nums if n not in have_eps]                     # aired, provider lacks it
        available = [n for n in aired_nums if n in have_eps and not have_eps[n]["activated"]]
        new_here = [x for x in lst if not x["activated"] and x["discovered_at"] and x["discovered_at"] > baseline
                    and x["episode_number"] in aired_nums]

        if sn in active_seasons:
            for x in new_here:
                candidates.append({"episode_id": x["id"], "season": sn, "episode": x["episode_number"]})
            state = "gap" if (missing or available) else ("waiting" if upcoming else "complete")
            rows.append({
                "season": sn, "have": len(activated), "aired": len(aired_nums) or len(have_eps),
                "total": len(dates) or len(have_eps),
                "missing": missing, "available": available,
                "upcoming": [{"episode": n, "date": d} for d, n in upcoming[:3]],
                "next_air": upcoming[0][0] if upcoming else None, "state": state,
            })
        elif new_here:
            new_seasons.append({"season": sn, "episodes": sorted(x["episode_number"] for x in new_here),
                                "episode_ids": [x["id"] for x in new_here]})

    keep = [r for r in rows if r["state"] != "complete"]
    if status == "continuing" and rows and not any(r["season"] == rows[-1]["season"] for r in keep):
        keep.append(rows[-1])  # the latest season stays visible so the next air date shows
    for ns in new_seasons:
        keep.append({"season": ns["season"], "have": 0, "aired": len(ns["episodes"]), "total": len(ns["episodes"]),
                     "missing": [], "available": ns["episodes"], "upcoming": [], "next_air": None,
                     "state": "new_season"})
    keep.sort(key=lambda r: r["season"])
    listed = bool(keep)
    return {
        "series_id": sid, "name": show["name"], "year": show["year"], "status": status,
        "category_id": show["category_id"], "mode": show["mode"], "next_air": next_air,
        "rows": keep, "listed": listed, "candidates": candidates, "new_seasons": new_seasons,
    }


async def watch_list() -> dict:
    db = await get_db()
    today = _today()
    shows = []
    for s in await _active_shows(db):
        d = await compute_show(db, s, today)
        if d["listed"]:
            shows.append({k: v for k, v in d.items() if k not in ("candidates", "new_seasons")})
    ev = await (await db.execute(
        "SELECT e.id, e.series_id, s.name, e.season_number, e.episode_number, e.kind, e.note, e.created_at "
        "FROM series_watch_events e LEFT JOIN series s ON s.id = e.series_id "
        "ORDER BY e.id DESC LIMIT 20")).fetchall()
    return {"shows": shows, "events": [dict(r) for r in ev], "last_run": _last_run, "today": today}


async def _event(db, series_id, episode_id, season, episode, kind, note) -> bool:
    cur = await db.execute(
        "INSERT OR IGNORE INTO series_watch_events (series_id, episode_id, season_number, episode_number, "
        "kind, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (series_id, episode_id, season, episode, kind, note, datetime.now(timezone.utc).isoformat()))
    return cur.rowcount == 1


async def _seen(db, episode_id: int) -> bool:
    return bool(await (await db.execute(
        "SELECT 1 FROM series_watch_events WHERE episode_id = ? "
        "AND kind IN ('auto_added', 'new_available', 'no_category')", (episode_id,))).fetchone())


async def run_watch(refresh: bool = True, only: int | None = None) -> dict:
    """Re-read watched shows' episode lists (Dispatcharr cache, no provider) and act on new
    episodes. Safe to call often: handled episodes are never handled twice."""
    if _run_lock.locked():
        return {"status": "busy"}
    async with _run_lock:
        from series_scraper import fetch_series_episodes
        db = await get_db()
        out = {"checked": 0, "refreshed": 0, "auto_added": 0, "notified": 0, "new_seasons": 0, "errors": 0}
        queued_any = False
        for show in await _active_shows(db, only):
            if show["mode"] == "off":
                continue
            out["checked"] += 1
            try:
                d = await compute_show(db, show)
                if refresh and d["listed"]:
                    res = await fetch_series_episodes(show["id"])
                    if res.get("status") == "ok":
                        out["refreshed"] += 1
                        d = await compute_show(db, show)
                    else:
                        out["errors"] += 1
                ids = [c["episode_id"] for c in d["candidates"] if not await _seen(db, c["episode_id"])]
                by_id = {c["episode_id"]: c for c in d["candidates"]}
                if ids:
                    if show["mode"] == "auto" and show["category_id"]:
                        for i in ids:
                            c = by_id[i]
                            await _event(db, show["id"], i, c["season"], c["episode"], "auto_added",
                                         f"S{c['season']:02d}E{c['episode']:02d} queued for activation")
                        await enqueue_series_episodes(db, show["id"], int(show["category_id"]), ids)
                        queued_any = True
                        out["auto_added"] += len(ids)
                    else:
                        kind = "new_available" if show["category_id"] else "no_category"
                        note = ("can be activated" if show["category_id"]
                                else "can be activated once the show has a library")
                        for i in ids:
                            c = by_id[i]
                            if await _event(db, show["id"], i, c["season"], c["episode"], kind,
                                            f"S{c['season']:02d}E{c['episode']:02d} {note}"):
                                out["notified"] += 1
                for ns in d["new_seasons"]:
                    # One event per season: a synthetic negative id keeps it once-only.
                    key = -(show["id"] * 1000 + ns["season"])
                    if await _event(db, show["id"], key, ns["season"], None, "new_season",
                                    f"Season {ns['season']} has {len(ns['episodes'])} episode(s) available"):
                        out["new_seasons"] += 1
                await db.commit()
            except Exception as exc:
                out["errors"] += 1
                logger.warning("series watch failed for %s: %s", show.get("name"), exc)
        if queued_any:
            wake_worker()
        _last_run.clear()
        _last_run.update(out, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        logger.info("Series watch: %s", out)
        return {"status": "ok", **out}


@router.get("/series/watch")
async def get_watch_list():
    return await watch_list()


@router.post("/series/watch/run")
async def post_watch_run():
    return await run_watch()


@router.post("/series/watch/{series_id}/mode")
async def set_watch_mode(series_id: int, request: Request):
    body = await request.json()
    mode = (body.get("mode") or "").lower()
    if mode not in MODES:
        return JSONResponse(status_code=400, content={"error": f"mode must be one of {', '.join(MODES)}"})
    db = await get_db()
    await db.execute(
        "INSERT INTO series_watch_prefs (series_id, mode) VALUES (?, ?) "
        "ON CONFLICT(series_id) DO UPDATE SET mode = excluded.mode", (series_id, mode))
    await db.commit()
    return {"status": "ok", "series_id": series_id, "mode": mode}
