"""Fill the head/tail cache for active titles that don't have one.

Plex reads the start and end of a file (and its size) when it adds, scans or analyses it.
The bridge answers those reads from the database, so they reach no provider. A title has
no cache when:
  - it is an episode (activating episodes opens no provider connection, so nothing was
    fetched); its listed size is also only a guess until the real one is stored;
  - it is a movie whose cache was cleared after a failover to another stream or a UUID
    change in Dispatcharr.

The activation queue worker calls fill_next() when it has nothing else to do. Each call
fetches one title (one provider connection, the same requests activation makes), so the
worker's spacing keeps the pace. A title is skipped when:
  - none of its provider accounts has a free connection (checked again later);
  - it was played recently (that would open a second connection while someone watches).
A failed fetch is retried after 1 h, 6 h and then every 24 h; it never marks anything dead.
"""
import logging
import time
from urllib.parse import urlparse, urlencode

import httpx

from config import DISPATCHARR_URL
from database import get_db

logger = logging.getLogger(__name__)

EPISODE_HEAD_SIZE = 6 * 1024 * 1024   # above ffprobe's 5 MB default probe size
TAIL_SIZE = 256 * 1024                # moov atom / cues, same as movies
BUSY_RECHECK_SECS = 600               # no free slot, or recently played
RECENT_PLAY_SECS = 3 * 3600
RETRY_SECS = (3600, 6 * 3600, 24 * 3600)

_backoff: dict[tuple[str, int], tuple[int, float]] = {}   # (kind, id) -> (failures, next try)


def _due(key) -> bool:
    return _backoff.get(key, (0, 0.0))[1] <= time.time()


def _later(key, secs: float):
    _backoff[key] = (_backoff.get(key, (0, 0.0))[0], time.time() + secs)


def _failed(key):
    n = _backoff.get(key, (0, 0.0))[0]
    _backoff[key] = (n + 1, time.time() + RETRY_SECS[min(n, len(RETRY_SECS) - 1)])


def _done(key):
    _backoff.pop(key, None)


async def _read_range(client: httpx.AsyncClient, url: str, start: int, end: int) -> tuple[bytes, int | None] | None:
    """Bytes start..end and the total size from Content-Range. Only a 206 is accepted, and
    reading stops at the requested length, so a server ignoring Range can't send a whole file."""
    want = end - start + 1
    async with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as resp:
        if resp.status_code != 206:
            logger.warning("Cache fill: HTTP %d for range %d-%d", resp.status_code, start, end)
            return None
        total = None
        cr = resp.headers.get("content-range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            total = int(cr.rsplit("/", 1)[1])
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) >= want:
                break
        return bytes(buf[:want]), total


async def _fetch(url: str, head_size: int, label: str) -> dict | None:
    """One session: head, then tail. None when it failed."""
    from proxy import _resolve_session

    resolved = await _resolve_session(url, 0)
    if not resolved:
        return None
    session_url = resolved[0]
    if urlparse(session_url).netloc.split(":")[0] != urlparse(DISPATCHARR_URL).netloc.split(":")[0]:
        logger.error("Cache fill: %s resolved to an outside host, blocked", label)
        return None
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
                                 follow_redirects=False) as client:
        head = await _read_range(client, session_url, 0, head_size - 1)
        if not head or not head[0]:
            return None
        head_bytes, size = head
        tail_bytes, tail_offset = None, 0
        if size and size > max(TAIL_SIZE, len(head_bytes)):
            tail_offset = size - TAIL_SIZE
            tail = await _read_range(client, session_url, tail_offset, size - 1)
            if tail:
                tail_bytes = tail[0]
    return {"head": head_bytes, "tail": tail_bytes, "tail_offset": tail_offset, "size": size}


async def _free_account(client, candidates: list) -> int | None | bool:
    """First account in candidates with a free connection; False when all are full."""
    from redirect import account_slots, slots_free
    seen = {}
    for acct in candidates:
        if acct not in seen:
            seen[acct] = slots_free(await account_slots(client, acct))
        if seen[acct]:
            return acct
    return False


async def _fill_episode(db, ep: dict) -> bool:
    import json
    from redirect import _episode_resolve_cache

    key = ("episode", ep["id"])
    played = _episode_resolve_cache.get(ep["id"])
    if played and time.time() - played["ts"] < RECENT_PLAY_SECS:
        _later(key, BUSY_RECHECK_SECS)
        return False

    options = []
    if ep.get("stream_id") or ep.get("account_id"):
        options.append((ep.get("stream_id"), ep.get("account_id")))
    try:
        options += [(a.get("stream_id"), a.get("account_id")) for a in json.loads(ep.get("alt_streams") or "[]")
                    if a.get("stream_id") or a.get("account_id")]
    except (ValueError, TypeError):
        pass
    if not options:
        _failed(key)
        return False
    async with httpx.AsyncClient() as client:
        acct = await _free_account(client, [o[1] for o in options])
    if acct is False:
        _later(key, BUSY_RECHECK_SECS)
        return False
    stream_id = next(o[0] for o in options if o[1] == acct)

    params = {}
    if acct:
        params["m3u_account_id"] = acct
    if stream_id:
        params["stream_id"] = stream_id
    url = f"{DISPATCHARR_URL}/proxy/vod/episode/{ep['uuid']}" + (f"?{urlencode(params)}" if params else "")
    label = f"episode {ep['id']} ({ep.get('name') or ''})"
    try:
        got = await _fetch(url, EPISODE_HEAD_SIZE, label)
    except Exception as e:
        logger.warning("Cache fill: %s failed: %s", label, e)
        got = None
    if not got:
        _failed(key)
        return True
    # The uuid guard drops the result if Dispatcharr changed the episode meanwhile.
    await db.execute(
        "UPDATE episodes SET header_data = ?, header_size = ?, tail_data = ?, tail_size = ?, tail_offset = ?, "
        "file_size = COALESCE(?, file_size), cache_account_id = ? WHERE id = ? AND activated = 1 AND uuid = ?",
        (got["head"], len(got["head"]), got["tail"], len(got["tail"] or b""), got["tail_offset"],
         got["size"], acct, ep["id"], ep["uuid"]))
    await db.commit()
    _done(key)
    logger.info("Cache fill: %s cached, head=%d tail=%d size=%s", label, len(got["head"]),
                len(got["tail"] or b""), got["size"])
    return True


async def _fill_movie(db, movie: dict) -> bool:
    from proxy import _movie_pipes, _log_event
    from redirect import _resolve_cache
    from api import HEADER_FETCH_SIZE

    key = ("movie", movie["id"])
    played = _resolve_cache.get(movie["id"])
    if movie["id"] in _movie_pipes or (played and time.time() - played["ts"] < RECENT_PLAY_SECS):
        _later(key, BUSY_RECHECK_SECS)
        return False
    if not movie.get("stream_id"):
        _failed(key)
        return False
    async with httpx.AsyncClient() as client:
        acct = await _free_account(client, [movie.get("account_id")])
    if acct is False:
        _later(key, BUSY_RECHECK_SECS)
        return False
    url = f"{DISPATCHARR_URL}/proxy/vod/movie/{movie['uuid']}?stream_id={movie['stream_id']}"
    label = f"movie {movie['id']} ({movie.get('name') or ''})"
    try:
        got = await _fetch(url, HEADER_FETCH_SIZE, label)
    except Exception as e:
        logger.warning("Cache fill: %s failed: %s", label, e)
        got = None
    if not got:
        _failed(key)
        return True
    # Guarded on uuid and stream_id: a failover or resync meanwhile makes these bytes stale.
    await db.execute(
        "UPDATE movies SET header_data = ?, header_size = ?, tail_data = ?, tail_size = ?, tail_offset = ?, "
        "file_size = COALESCE(?, file_size) WHERE id = ? AND activated = 1 AND uuid = ? AND stream_id = ?",
        (got["head"], len(got["head"]), got["tail"], len(got["tail"] or b""), got["tail_offset"],
         got["size"], movie["id"], movie["uuid"], movie["stream_id"]))
    await db.commit()
    _done(key)
    logger.info("Cache fill: %s cached, head=%d tail=%d size=%s", label, len(got["head"]),
                len(got["tail"] or b""), got["size"])
    _log_event("info", movie["id"], "Head/tail cache refilled", movie_name=movie.get("name"))
    return True


async def pending() -> dict:
    db = await get_db()
    ep = await (await db.execute(
        "SELECT COUNT(*) FROM episodes WHERE activated = 1 AND COALESCE(header_size, 0) = 0")).fetchone()
    mv = await (await db.execute(
        "SELECT COUNT(*) FROM movies WHERE activated = 1 AND COALESCE(dead, 0) = 0 "
        "AND COALESCE(stream_dead, 0) = 0 AND COALESCE(header_size, 0) = 0")).fetchone()
    return {"episodes": ep[0], "movies": mv[0]}


async def fill_next(kind: str) -> bool | None:
    """Cache one due title of this kind ('episode' or 'movie').
    True: a provider connection was used. False: one was due but skipped. None: none due."""
    db = await get_db()
    if kind == "episode":
        rows = await (await db.execute(
            "SELECT id, uuid, name, stream_id, account_id, alt_streams FROM episodes "
            "WHERE activated = 1 AND COALESCE(header_size, 0) = 0 AND uuid IS NOT NULL AND uuid != '' "
            "ORDER BY activated_at LIMIT 500")).fetchall()
        fill = _fill_episode
    else:
        rows = await (await db.execute(
            "SELECT * FROM movies WHERE activated = 1 AND COALESCE(dead, 0) = 0 AND COALESCE(stream_dead, 0) = 0 "
            "AND COALESCE(header_size, 0) = 0 AND uuid IS NOT NULL LIMIT 500")).fetchall()
        fill = _fill_movie
    for r in rows:
        item = dict(r)
        if _due((kind, item["id"])):
            return await fill(db, item)
    return None
