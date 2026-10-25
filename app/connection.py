"""Settings-tab sections (Plex, TMDB, Streaming): saved values win over the compose file.
Dispatcharr instances and provider accounts are lists of their own, in sources.py."""
import os
import re

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import config
import secret_box
from database import get_db

router = APIRouter(prefix="/api/settings")


async def _saved() -> dict:
    db = await get_db()
    rows = await (await db.execute("SELECT key, value FROM app_settings")).fetchall()
    return {r["key"]: r["value"] for r in rows if r["key"] in config.SETTINGS_KEYS and r["value"]}


def _current(key: str, saved: dict) -> str:
    """The value in use, as text (a saved restart-only value shows what applies after restart)."""
    if key in config.SECRET_KEYS:
        return getattr(config, key)
    if key in saved:
        return saved[key]
    return config.ENV_VALUES[key]


async def _group(group: str) -> dict:
    saved = await _saved()
    out = {}
    for k in config.SETTINGS_GROUPS[group]:
        unreadable = k in config.UNREADABLE
        source = "settings" if k in saved and not unreadable else ("compose" if os.environ.get(k) else "default")
        value = _current(k, saved)
        # Secrets never leave the server; the form only learns whether one is set.
        f = {"set": bool(value)} if k in config.SECRET_KEYS else {"value": value}
        f["source"] = source
        if unreadable:
            f["unreadable"] = True
        if k == "REDIRECT_MODE":
            f["restart_pending"] = (value.lower() in ("1", "true", "yes")) != config.REDIRECT_STREAMING
        out[k] = f
    return out


@router.get("")
async def get_settings():
    return {g: await _group(g) for g in config.SETTINGS_GROUPS}


def _merge(group: str, body: dict, saved: dict) -> dict:
    """The form's values; a blank secret keeps the current one, and secrets listed in _reset are cleared."""
    reset = set(body.get("_reset") or [])
    merged = {}
    for k in config.SETTINGS_GROUPS[group]:
        v = str(body.get(k) or "").strip()
        if k in config.SECRET_KEYS and not v and k not in reset:
            v = getattr(config, k) if k in saved and k not in config.UNREADABLE else ""
        merged[k] = v
    return merged


def _effective(values: dict) -> dict:
    """Blank fields fall back to the compose value."""
    return {k: v or config.ENV_VALUES[k] for k, v in values.items()}


def _validate(group: str, values: dict) -> str | None:
    v = _effective(values)
    if v.get("PLEX_URL") and not re.match(r"https?://", v["PLEX_URL"]):
        return "Plex URL must start with http:// or https://"
    for k in ("PLEX_LIBRARY_ID", "PLEX_SERIES_LIBRARY_ID"):
        if v.get(k) and not v[k].isdigit():
            return "Library IDs are numbers, e.g. 7"
    for k in ("PLEX_MOVIE_MOUNT_ROOT", "PLEX_SERIES_MOUNT_ROOT"):
        if v.get(k) and not v[k].startswith("/"):
            return "Mount paths start with /, e.g. /mnt/vod-bridge"
    if group == "streaming":
        if not v["MAX_CONCURRENT_PER_TITLE"].isdigit() or not 1 <= int(v["MAX_CONCURRENT_PER_TITLE"]) <= 20:
            return "Players per title must be a number from 1 to 20"
        if v["REDIRECT_MODE"].lower() not in ("true", "false"):
            return "Pick a streaming mode"
    return None


@router.post("/{group}")
async def save_settings(group: str, request: Request):
    if group not in config.SETTINGS_GROUPS:
        return JSONResponse({"error": "Unknown settings section"}, status_code=404)
    saved = await _saved()
    values = _merge(group, await request.json(), saved)
    error = _validate(group, values)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    db = await get_db()
    for k, v in values.items():
        if not v:
            await db.execute("DELETE FROM app_settings WHERE key=?", (k,))
        else:
            stored = secret_box.encrypt(v) if k in config.SECRET_KEYS else v
            await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, stored))
        config.UNREADABLE.discard(k)
    await db.commit()
    config.apply_settings({k: v for k, v in _effective(values).items() if k not in config.RESTART_KEYS})
    return await _group(group)


@router.post("/{group}/test")
async def test_settings(group: str, request: Request):
    values = _effective(_merge(group, await request.json(), await _saved()))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if group == "plex":
                return await _test_plex(client, values)
            if group == "tmdb":
                return await _test_tmdb(client, values)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Could not connect: {type(e).__name__}"}
    return JSONResponse({"error": "Nothing to test in this section"}, status_code=400)


async def _test_plex(client: httpx.AsyncClient, v: dict) -> dict:
    if not v["PLEX_URL"] or not v["PLEX_TOKEN"]:
        return {"ok": False, "message": "Enter the Plex URL and token first"}
    resp = await client.get(f"{v['PLEX_URL'].rstrip('/')}/library/sections",
                            headers={"X-Plex-Token": v["PLEX_TOKEN"], "Accept": "application/json"})
    if resp.status_code == 401:
        return {"ok": False, "message": "Plex refused the token"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"Plex answered HTTP {resp.status_code}"}
    libraries = [{"id": d.get("key"), "title": d.get("title"), "type": d.get("type")}
                 for d in resp.json().get("MediaContainer", {}).get("Directory", [])]
    ids = {lib["id"] for lib in libraries}
    titles = {lib["id"]: lib["title"] for lib in libraries}
    db = await get_db()
    rows = await (await db.execute(
        "SELECT name, plex_library_section FROM series_categories ORDER BY name")).fetchall()
    categories = []
    for r in rows:
        own = r["plex_library_section"] not in (None, 999)
        section = str(r["plex_library_section"]) if own else v["PLEX_SERIES_LIBRARY_ID"]
        categories.append({"name": r["name"], "id": section or "", "own": own,
                           "title": titles.get(section, "not found in Plex" if section else "none")})
    checks = [("PLEX_LIBRARY_ID", "movie library")]
    # The series ID is only a fallback, so it matters only when some category has no library of its own.
    if not rows or any(not c["own"] for c in categories):
        checks.append(("PLEX_SERIES_LIBRARY_ID", "fallback series library"))
    missing = [f"{name} {v[k]}" for k, name in checks if v[k] and v[k] not in ids]
    missing += [f"series category {c['name']} library {c['id']}" for c in categories if c["own"] and c["id"] not in ids]
    message = f"Connected to Plex — {len(libraries)} libraries"
    if missing:
        message += f". Not found: {', '.join(missing)}"
    return {"ok": not missing, "message": message, "libraries": libraries, "categories": categories}


async def _test_tmdb(client: httpx.AsyncClient, v: dict) -> dict:
    if v["TMDB_READ_TOKEN"]:
        params, headers = {}, {"Authorization": f"Bearer {v['TMDB_READ_TOKEN']}"}
    elif v["TMDB_API_KEY"]:
        params, headers = {"api_key": v["TMDB_API_KEY"]}, {}
    else:
        return {"ok": False, "message": "Enter an API key or read access token first"}
    resp = await client.get("https://api.themoviedb.org/3/configuration", params=params, headers=headers)
    if resp.status_code == 401:
        return {"ok": False, "message": "TMDB refused the key"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"TMDB answered HTTP {resp.status_code}"}
    used = "read access token" if v["TMDB_READ_TOKEN"] else "API key"
    return {"ok": True, "message": f"Connected to TMDB with the {used}"}
