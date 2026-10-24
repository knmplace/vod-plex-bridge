"""302 redirect streaming for movies: resolves a Dispatcharr VOD proxy URL,
coalesces rapid repeat requests, and enforces a per-title connection cap via
Dispatcharr's REST VOD-proxy-stats / stop-client endpoints (no Redis access
here, so this is REST-only, mirroring the plugin's Redis-based safety net).
"""
import asyncio
import json
import logging
import time

import httpx

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, MAX_CONCURRENT_PER_TITLE
from database import get_db

logger = logging.getLogger(__name__)

REDIRECT_COALESCE_SECS = 3
SWEEP_INTERVAL_SECS = 10

_resolve_locks: dict[int, asyncio.Lock] = {}
_resolve_cache: dict[int, dict] = {}  # movie_id -> {"url": str, "account_id": int, "ts": float}


def _auth_headers() -> dict:
    return {"X-API-Key": DISPATCHARR_API_KEY} if DISPATCHARR_API_KEY else {}


async def _account_has_capacity(client: httpx.AsyncClient, account_id: int) -> bool:
    if not account_id:
        return True
    try:
        resp = await client.get(f"{DISPATCHARR_URL}/api/m3u/accounts/{account_id}/",
                                 headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return True
        data = resp.json()
        profiles = data.get("profiles", [])
        if not profiles:
            return True
        return any(
            p.get("is_active", True) and p.get("current_viewers", 0) < p.get("max_streams", 0)
            for p in profiles
            if p.get("max_streams", 0) > 0
        ) or any(p.get("max_streams", 0) == 0 for p in profiles)
    except Exception as e:
        logger.warning("Capacity check failed for account %d: %s", account_id, e)
        return True


async def _pick_relation_with_capacity(client: httpx.AsyncClient, movie: dict) -> tuple[int, int, int] | None:
    """Return (stream_id, account_id, uuid-index-unused) for the first relation with free capacity.

    Checks the movie's primary stream first, then falls back to alt_streams
    (same JSON shape used by _switch_to_fallback in proxy.py).
    """
    primary_sid = movie.get("stream_id")
    primary_acct = movie.get("account_id")
    if primary_sid and await _account_has_capacity(client, primary_acct):
        return primary_sid, primary_acct

    try:
        alts = json.loads(movie.get("alt_streams") or "[]")
    except (json.JSONDecodeError, TypeError):
        alts = []

    for alt in alts:
        sid = alt.get("stream_id")
        acct = alt.get("account_id")
        if sid and await _account_has_capacity(client, acct):
            return sid, acct

    if primary_sid:
        return primary_sid, primary_acct
    return None


async def get_redirect_url(movie_id: int) -> tuple[str | None, str | None, bool]:
    """Resolve the Dispatcharr proxy URL to redirect this movie's playback to.

    Returns (url, error, fresh). Coalesces repeat calls within
    REDIRECT_COALESCE_SECS unless the previously-picked account has since
    lost capacity. `fresh` is False on a coalesced cache hit, so callers can
    skip logging the flood of rclone's repeat re-hits on the same URL.
    """
    if movie_id not in _resolve_locks:
        _resolve_locks[movie_id] = asyncio.Lock()

    async with _resolve_locks[movie_id]:
        cached = _resolve_cache.get(movie_id)
        now = time.time()
        if cached and (now - cached["ts"]) < REDIRECT_COALESCE_SECS:
            return cached["url"], None, False

        db = await get_db()
        row = await db.execute(
            "SELECT id, uuid, stream_id, account_id, content_type, alt_streams FROM movies WHERE id = ?",
            (movie_id,),
        )
        movie = await row.fetchone()
        if not movie:
            return None, "Movie not found", True
        movie = dict(movie)

        if not movie.get("uuid") or not movie.get("stream_id"):
            return None, "No stream mapping", True

        async with httpx.AsyncClient() as client:
            picked = await _pick_relation_with_capacity(client, movie)

        if not picked:
            return None, "No provider capacity available", True

        stream_id, account_id = picked
        url = f"{DISPATCHARR_URL}/proxy/vod/movie/{movie['uuid']}?stream_id={stream_id}"

        _resolve_cache[movie_id] = {"url": url, "account_id": account_id, "ts": now}

        asyncio.create_task(_enforce_max_concurrent_for_content(movie["uuid"]))

        return url, None, True


async def _fetch_vod_stats(client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(f"{DISPATCHARR_URL}/proxy/vod/stats/", headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("vod_connections", [])
    except Exception as e:
        logger.warning("VOD stats fetch failed: %s", e)
        return []


async def _stop_client(client: httpx.AsyncClient, client_id: str):
    try:
        resp = await client.post(f"{DISPATCHARR_URL}/proxy/vod/stop_client/",
                                  headers=_auth_headers(), json={"client_id": client_id}, timeout=10)
        if resp.status_code not in (200, 204):
            logger.warning("stop_client %s returned HTTP %d", client_id, resp.status_code)
        else:
            logger.info("Stopped excess VOD client %s (session-cap sweep)", client_id)
    except Exception as e:
        logger.warning("stop_client failed for %s: %s", client_id, e)


def _connections_for_content(all_conns: list[dict], content_uuid: str) -> list[dict]:
    matched = []
    for conn in all_conns:
        if conn.get("content_uuid") == content_uuid or conn.get("uuid") == content_uuid:
            matched.append(conn)
    return matched


async def _enforce_max_concurrent_for_content(content_uuid: str):
    async with httpx.AsyncClient() as client:
        all_conns = await _fetch_vod_stats(client)
        conns = _connections_for_content(all_conns, content_uuid)
        if len(conns) <= MAX_CONCURRENT_PER_TITLE:
            return

        conns.sort(key=lambda c: c.get("last_activity") or c.get("connected_at") or 0, reverse=True)
        excess = conns[MAX_CONCURRENT_PER_TITLE:]
        for conn in excess:
            client_id = conn.get("client_id")
            if client_id:
                await _stop_client(client, client_id)


async def _enforce_max_concurrent_globally():
    async with httpx.AsyncClient() as client:
        all_conns = await _fetch_vod_stats(client)
        if not all_conns:
            return

        by_content: dict[str, list[dict]] = {}
        for conn in all_conns:
            key = conn.get("content_uuid") or conn.get("uuid")
            if not key:
                continue
            by_content.setdefault(key, []).append(conn)

        for content_uuid, conns in by_content.items():
            if len(conns) <= MAX_CONCURRENT_PER_TITLE:
                continue
            conns.sort(key=lambda c: c.get("last_activity") or c.get("connected_at") or 0, reverse=True)
            excess = conns[MAX_CONCURRENT_PER_TITLE:]
            for conn in excess:
                client_id = conn.get("client_id")
                if client_id:
                    await _stop_client(client, client_id)


async def session_cap_sweep_loop():
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECS)
            await _enforce_max_concurrent_globally()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Session-cap sweep error: %s", e)


# --- Series/episode redirect streaming (mirrors the movie logic above) ---

_episode_resolve_locks: dict[int, asyncio.Lock] = {}
_episode_resolve_cache: dict[int, dict] = {}  # episode_id -> {"url": str, "account_id": int, "ts": float}


async def _pick_episode_relation_with_capacity(client: httpx.AsyncClient, episode: dict) -> tuple[int, int] | None:
    """Same as _pick_relation_with_capacity but for an episode dict (same field
    names: stream_id, account_id, alt_streams)."""
    primary_sid = episode.get("stream_id")
    primary_acct = episode.get("account_id")
    if primary_sid and await _account_has_capacity(client, primary_acct):
        return primary_sid, primary_acct

    try:
        alts = json.loads(episode.get("alt_streams") or "[]")
    except (json.JSONDecodeError, TypeError):
        alts = []

    for alt in alts:
        sid = alt.get("stream_id")
        acct = alt.get("account_id")
        if sid and await _account_has_capacity(client, acct):
            return sid, acct

    if primary_sid:
        return primary_sid, primary_acct
    return None


async def get_episode_redirect_url(episode_id: int) -> tuple[str | None, str | None, bool]:
    """Resolve the Dispatcharr proxy URL to redirect this episode's playback to.

    Mirrors get_redirect_url() exactly but keyed on episode_id with its own
    lock/cache dicts, and hits /proxy/vod/episode/ instead of /proxy/vod/movie/.
    """
    if episode_id not in _episode_resolve_locks:
        _episode_resolve_locks[episode_id] = asyncio.Lock()

    async with _episode_resolve_locks[episode_id]:
        cached = _episode_resolve_cache.get(episode_id)
        now = time.time()
        if cached and (now - cached["ts"]) < REDIRECT_COALESCE_SECS:
            return cached["url"], None, False

        db = await get_db()
        row = await db.execute(
            "SELECT id, uuid, stream_id, account_id, container_extension, alt_streams FROM episodes WHERE id = ?",
            (episode_id,),
        )
        episode = await row.fetchone()
        if not episode:
            return None, "Episode not found", True
        episode = dict(episode)

        if not episode.get("uuid") or not episode.get("stream_id"):
            return None, "No stream mapping", True

        async with httpx.AsyncClient() as client:
            picked = await _pick_episode_relation_with_capacity(client, episode)

        if not picked:
            return None, "No provider capacity available", True

        stream_id, account_id = picked
        url = f"{DISPATCHARR_URL}/proxy/vod/episode/{episode['uuid']}?stream_id={stream_id}"

        _episode_resolve_cache[episode_id] = {"url": url, "account_id": account_id, "ts": now}

        asyncio.create_task(_enforce_max_concurrent_for_content(episode["uuid"]))

        return url, None, True
