"""Learn the bytes Plex reads beyond the cached head/tail during a library scan.

The activation cache holds a fixed head and tail. During a scan Plex (through rclone) sometimes
reads a little more: a tail that starts before the cached one, or a window after the head.
Those reads used to be redirected to Dispatcharr, which opened a provider session on every
refresh.

On the first such read for a title the bridge streams the bytes from the provider itself,
hands them to the client and keeps what the client actually took (at most LEARN_MAX). Later
reads of that range come from the database. A title keeps at most LEARN_PARTS captures / LEARN_TOTAL bytes: further misses (real
playback reading deep into the file) are redirected as before, so playback is not proxied.
"""
import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse

from config import DISPATCHARR_URL
from database import get_db

logger = logging.getLogger(__name__)

LEARN_MAX = 16 * 1024 * 1024   # most bytes kept (and proxied) for one capture
LEARN_TOTAL = 32 * 1024 * 1024 # most bytes kept for one title, over all captures
LEARN_PARTS = 4                # most captures for one title
MAX_ATTEMPTS = 6               # captures tried per title since start, failed ones included
_inflight: set[tuple[str, int]] = set()
_attempts: dict[tuple[str, int], int] = {}


def make_sig(*parts) -> str:
    return ":".join(str(p) for p in parts)


async def find(kind: str, item_id: int, sig: str, pos: int, end: int) -> bytes | None:
    """Learned bytes starting at pos (up to end), or None."""
    db = await get_db()
    row = await (await db.execute(
        "SELECT start, data FROM learned_ranges WHERE kind = ? AND item_id = ? AND sig = ? "
        "AND start <= ? AND start + length(data) > ? LIMIT 1",
        (kind, item_id, sig, pos, pos))).fetchone()
    if not row:
        return None
    return bytes(row["data"][pos - row["start"]:end - row["start"] + 1]) or None


async def _budget(kind: str, item_id: int, sig: str) -> int:
    """Bytes this title may still learn (0 = none). Rows from an older file are dropped."""
    db = await get_db()
    await db.execute("DELETE FROM learned_ranges WHERE kind = ? AND item_id = ? AND (sig != ? OR start < 0)", (kind, item_id, sig))
    row = await (await db.execute(
        "SELECT COALESCE(SUM(length(data)), 0), COUNT(*) FROM learned_ranges WHERE kind = ? AND item_id = ?",
        (kind, item_id))).fetchone()
    await db.commit()
    if row[1] >= LEARN_PARTS or _attempts.get((kind, item_id), 0) >= MAX_ATTEMPTS:
        return 0
    return max(0, LEARN_TOTAL - row[0])


async def _store(kind: str, item_id: int, sig: str, start: int, data: bytes):
    if not data:
        return
    db = await get_db()
    await db.execute("INSERT OR REPLACE INTO learned_ranges (kind, item_id, start, sig, data, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (kind, item_id, start, sig, data, time.time()))
    await db.commit()


async def learn(kind: str, item_id: int, sig: str, url: str, start: int, end: int, file_size: int,
                content_type: str, label: str, log_id: int | None) -> Response | None:
    """Stream one capture window to the client and keep it. None when the caller should
    redirect instead (already learned, in progress, or the provider did not cooperate)."""
    from proxy import _resolve_session, _log_event

    key = (kind, item_id)
    if key in _inflight:
        return None
    budget = await _budget(kind, item_id, sig)
    if budget <= 0:
        return None
    _inflight.add(key)
    _attempts[key] = _attempts.get(key, 0) + 1
    client = resp = None
    try:
        resolved = await _resolve_session(url, 0)
        session_url = resolved[0] if resolved else None
        if session_url and urlparse(session_url).netloc.split(":")[0] != urlparse(DISPATCHARR_URL).netloc.split(":")[0]:
            logger.error("Range learn: %s resolved to an outside host, blocked", label)
            session_url = None
        if session_url:
            end = min(end, start + min(LEARN_MAX, budget) - 1, file_size - 1)
            client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
                                       follow_redirects=False)
            resp = await client.send(client.build_request(
                "GET", session_url, headers={"Range": f"bytes={start}-{end}"}), stream=True)
    except Exception as e:
        logger.warning("Range learn: %s failed: %s", label, e)
    if resp is None or resp.status_code != 206:
        _inflight.discard(key)
        if resp is not None:
            await resp.aclose()
        if client:
            await client.aclose()
        return None

    want = end - start + 1
    _log_event("info", log_id, "Learning scan range from provider", movie_name=label,
               range_start=start, range_end=end)

    async def body():
        buf = bytearray()
        try:
            async for chunk in resp.aiter_bytes():
                chunk = chunk[:want - len(buf)]
                if not chunk:
                    break
                buf.extend(chunk)
                yield chunk
        finally:
            asyncio.create_task(_finish(buf))

    async def _finish(buf: bytearray):
        try:
            await resp.aclose()
            await client.aclose()
            await _store(kind, item_id, sig, start, bytes(buf))
            _log_event("info", log_id, "Learned scan range", movie_name=label,
                       range_start=start, bytes=len(buf))
        except Exception as e:
            logger.warning("Range learn: finishing %s failed: %s", label, e)
        finally:
            _inflight.discard(key)

    return StreamingResponse(body(), status_code=206, headers={
        "accept-ranges": "bytes",
        "content-type": content_type,
        "content-length": str(want),
        "content-range": f"bytes {start}-{end}/{file_size}",
    })
