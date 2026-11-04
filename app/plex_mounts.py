"""Check that Plex can still read the bridge folders it has mounted.

The bridge can't see the Plex host's rclone mounts, so it asks Plex. For each folder the bridge
serves (movies, movie categories, series) that has active titles:
  - find the Plex libraries whose locations are in that folder;
  - take the newest Plex item stored there and ask Plex whether its file is still readable
    (checkFiles). A stopped mount leaves an empty folder, so the file is missing at once;
  - if Plex lists nothing there while the bridge has active titles, report it after two checks
    in a row (a new library may not have been scanned yet).
Only the Plex API and the bridge's own database are used. The file check makes Plex stat one
file on the mount, which reaches the bridge as a HEAD request answered from the database; titles
whose size isn't stored yet are skipped so no provider is contacted.
"""
import asyncio
import logging
import re
import time

import httpx

import config
from database import get_db
from plex_libraries import join_mount, describe_error

logger = logging.getLogger(__name__)

CHECK_EVERY_SECS = 30 * 60
FIRST_CHECK_SECS = 20   # soon after start, so a broken mount shows without waiting
RECHECK_SECS = 60
_ID_RE = re.compile(r"\[(\d+)\]\.\w+$")
_DEFAULT_SQL = "(category_id IS NULL OR category_id NOT IN (SELECT id FROM user_movie_categories))"

_last: dict = {"checked_at": None, "error": None, "mounts": []}
_empty_strikes: dict = {}
_lock = asyncio.Lock()


def _roots() -> list[dict]:
    """The bridge folders as the Plex host spells them. Read at call time so Settings apply."""
    out = []
    for key, label, root, kind in (
        ("movies", "Movies", config.PLEX_MOVIE_MOUNT_ROOT, "movie"),
        ("movie_categories", "Movie categories", config.PLEX_MOVIE_CATEGORY_MOUNT_ROOT, "movie"),
        ("series", "Series", config.PLEX_SERIES_MOUNT_ROOT, "show"),
    ):
        if root:
            out.append({"key": key, "label": label, "root": root, "kind": kind,
                        "prefix": join_mount(root, "")})
    return out


def _under(path: str, mount: dict) -> bool:
    p = (path or "").rstrip("/\\")
    return p == mount["root"].rstrip("/\\") or (path or "").startswith(mount["prefix"])


async def _bridge_counts() -> dict:
    db = await get_db()  # shared connection; never closed here

    async def one(sql):
        return (await (await db.execute(sql)).fetchone())[0]
    return {
        "movies": await one(f"SELECT COUNT(*) FROM movies WHERE activated = 1 AND dead = 0 AND {_DEFAULT_SQL}"),
        "movie_categories": await one(
            f"SELECT COUNT(*) FROM movies WHERE activated = 1 AND dead = 0 AND NOT {_DEFAULT_SQL}"),
        "series": await one("SELECT COUNT(*) FROM episodes WHERE activated = 1"),
    }


async def _safe_ids(kind: str, ids: list[int]) -> set:
    """Ids whose HEAD request the bridge answers from the database (no provider probe)."""
    if not ids:
        return set()
    if kind == "show":
        return set(ids)
    db = await get_db()
    marks = ",".join("?" * len(ids))
    rows = await (await db.execute(
        f"SELECT id FROM movies WHERE id IN ({marks}) AND file_size IS NOT NULL AND file_size > 0", ids)).fetchall()
    return {r[0] for r in rows}


def _files(md: dict) -> list[str]:
    return [p.get("file", "") for m in md.get("Media", []) or [] for p in m.get("Part", []) or []]


def _truthy(v) -> bool:
    return v is True or str(v).lower() in ("1", "true")


async def _file_readable(client: httpx.AsyncClient, rating_key) -> bool | None:
    resp = await client.get(f"{config.PLEX_URL}/library/metadata/{rating_key}", params={"checkFiles": 1})
    resp.raise_for_status()
    md = (resp.json().get("MediaContainer", {}).get("Metadata") or [{}])[0]
    parts = [p for m in md.get("Media", []) or [] for p in m.get("Part", []) or []]
    if not parts or all("exists" not in p and "accessible" not in p for p in parts):
        return None
    return any(_truthy(p.get("exists")) and _truthy(p.get("accessible", True)) for p in parts)


async def _check_mount(client, mount: dict, sections: list[dict], bridge_items: int) -> dict:
    res = {k: mount[k] for k in ("key", "label", "root")}
    res.update({"bridge_items": bridge_items, "plex_items": 0, "libraries": [], "state": "ok", "reason": ""})
    mine = [s for s in sections if s.get("type") == mount["kind"]
            and any(_under(loc.get("path", ""), mount) for loc in s.get("Location", []) or [])]
    res["libraries"] = [s.get("title", "") for s in mine]
    if bridge_items == 0:
        res["state"] = "idle"
        return res
    if not mine:
        res["state"] = "unlinked"
        res["reason"] = f"No Plex library uses {mount['root']}."
        return res

    item_type = 1 if mount["kind"] == "movie" else 4
    candidates = []
    for s in mine:
        resp = await client.get(f"{config.PLEX_URL}/library/sections/{s['key']}/all", params={
            "type": item_type, "sort": "addedAt:desc",
            "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 25})
        resp.raise_for_status()
        mc = resp.json().get("MediaContainer", {})
        for md in mc.get("Metadata", []) or []:
            files = [f for f in _files(md) if f.startswith(mount["prefix"])]
            if files:
                m = _ID_RE.search(files[0].replace("\\", "/"))
                candidates.append((md.get("addedAt") or 0, md.get("ratingKey"), int(m.group(1)) if m else None))
        res["plex_items"] += int(mc.get("totalSize", mc.get("size", 0)) or 0)

    if not candidates:
        strikes = _empty_strikes.get(mount["key"], 0) + 1
        _empty_strikes[mount["key"]] = strikes
        if strikes >= 2:
            res["state"] = "down"
            res["reason"] = (f"The bridge has {bridge_items} active, but Plex lists nothing in "
                             f"{mount['root']}. Plex can't see the bridge's files there.")
        else:
            res["state"] = "pending"
            res["reason"] = "Plex lists nothing there yet; checking again before warning."
        return res
    _empty_strikes.pop(mount["key"], None)

    candidates.sort(reverse=True)
    safe = await _safe_ids(mount["kind"], [c[2] for c in candidates if c[2] is not None])
    pick = next((c for c in candidates if c[2] in safe), None)
    if not pick:
        res["reason"] = "No title safe to check yet (sizes not stored); counted only."
        return res
    readable = await _file_readable(client, pick[1])
    if readable is False:
        await asyncio.sleep(RECHECK_SECS)
        readable = await _file_readable(client, pick[1])
    if readable is False:
        res["state"] = "down"
        res["reason"] = (f"Plex can't read its files in {mount['root']}. The mount on the Plex "
                         f"machine is stopped or broken.")
    return res


async def check() -> dict:
    """Run one check now and remember the result."""
    async with _lock:
        if not (config.PLEX_URL and config.PLEX_TOKEN):
            _last.update({"checked_at": time.time(), "error": "Plex isn't set up.", "mounts": []})
            return dict(_last)
        counts = await _bridge_counts()
        try:
            headers = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                resp = await client.get(f"{config.PLEX_URL}/library/sections")
                resp.raise_for_status()
                sections = resp.json().get("MediaContainer", {}).get("Directory", []) or []
                mounts = [await _check_mount(client, m, sections, counts.get(m["key"], 0)) for m in _roots()]
        except Exception as e:
            _last.update({"checked_at": time.time(), "error": f"Couldn't ask Plex: {describe_error(e)}"})
            logger.warning("Plex mount check: %s", _last["error"])
            return dict(_last)
        _last.update({"checked_at": time.time(), "error": None, "mounts": mounts})
        for m in mounts:
            if m["state"] == "down":
                logger.warning("Plex mount check: %s (%s) DOWN: %s", m["label"], m["root"], m["reason"])
        return dict(_last)


def status() -> dict:
    out = dict(_last)
    out["down"] = [m for m in _last.get("mounts", []) if m.get("state") == "down"]
    return out


def summary(result: dict) -> str:
    """One Daily Run line; contains FAILED when a mount is down so the report shows it red."""
    if result.get("error"):
        return f"Plex mounts: not checked ({result['error']})"
    down = [m for m in result.get("mounts", []) if m["state"] == "down"]
    if down:
        return "Plex mounts: FAILED — " + "; ".join(f"{m['label']}: {m['reason']}" for m in down) + \
               " On the Plex machine, re-run Command B from the Setup tab; it repairs this."
    seen = [f"{m['label']} {m['plex_items']}" for m in result.get("mounts", []) if m["state"] in ("ok", "pending")]
    return "Plex mounts: OK" + (f" ({', '.join(seen)} in Plex)" if seen else "")


async def monitor_loop():
    await asyncio.sleep(FIRST_CHECK_SECS)
    while True:
        try:
            await check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Plex mount check failed")
        await asyncio.sleep(CHECK_EVERY_SECS)
