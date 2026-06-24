import asyncio
import logging
import re
import time
from collections import deque
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY
from database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

STREAM_CHUNK = 256 * 1024

# --- Session Reuse (prevents connection flooding) ---
# Maps movie_id → (session_id, resolved_upstream_url, timestamp)
_movie_sessions: dict[int, tuple[str, str, float]] = {}
# Per-movie locks to serialize concurrent requests
_movie_locks: dict[int, asyncio.Lock] = {}
SESSION_TTL = 3600  # 1 hour — sessions expire after this

def _get_movie_lock(movie_id: int) -> asyncio.Lock:
    if movie_id not in _movie_locks:
        _movie_locks[movie_id] = asyncio.Lock()
    return _movie_locks[movie_id]


def _get_cached_session(movie_id: int) -> tuple[str, str] | None:
    entry = _movie_sessions.get(movie_id)
    if not entry:
        return None
    session_id, resolved_url, ts = entry
    if time.time() - ts > SESSION_TTL:
        del _movie_sessions[movie_id]
        return None
    return session_id, resolved_url


def _cache_session(movie_id: int, session_id: str, resolved_url: str):
    _movie_sessions[movie_id] = (session_id, resolved_url, time.time())
    logger.info("Cached session for movie %d: session_id=%s", movie_id, session_id)


def clear_movie_session(movie_id: int):
    _movie_sessions.pop(movie_id, None)
    _movie_locks.pop(movie_id, None)


async def _resolve_session(xc_url: str, movie_id: int) -> tuple[str, str] | None:
    """Make initial request with follow_redirects=False to capture session_id from 301."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(xc_url, headers={"Range": "bytes=0-0"})
            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                if location:
                    parsed = urlparse(location)
                    qs = parse_qs(parsed.query)
                    session_id = qs.get("session_id", [None])[0]
                    if session_id:
                        if location.startswith("/"):
                            base = urlparse(DISPATCHARR_URL)
                            resolved = f"{base.scheme}://{base.netloc}{location}"
                        else:
                            resolved = location
                        _cache_session(movie_id, session_id, resolved)
                        logger.info("Resolved session for movie %d: %s", movie_id, session_id)
                        return session_id, resolved
                    else:
                        logger.warning("301 redirect for movie %d but no session_id in Location: %s", movie_id, location)
            elif resp.status_code < 400:
                logger.info("No redirect for movie %d (status %d), using direct URL", movie_id, resp.status_code)
                return None
            else:
                logger.error("Session resolve failed for movie %d: HTTP %d", movie_id, resp.status_code)
    except Exception as e:
        logger.error("Session resolve error for movie %d: %s", movie_id, e)
    return None


# --- Circuit Breaker ---
_failure_tracker: dict[int, list] = {}
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_COOLDOWN = 30


def _check_circuit(stream_id: int) -> bool:
    record = _failure_tracker.get(stream_id)
    if not record:
        return True
    count, last_fail = record
    if count >= CIRCUIT_FAIL_THRESHOLD and (time.time() - last_fail) < CIRCUIT_COOLDOWN:
        return False
    if (time.time() - last_fail) >= CIRCUIT_COOLDOWN:
        del _failure_tracker[stream_id]
    return True


def _record_failure(stream_id: int):
    record = _failure_tracker.get(stream_id)
    now = time.time()
    if record:
        _failure_tracker[stream_id] = [record[0] + 1, now]
    else:
        _failure_tracker[stream_id] = [1, now]


def _clear_failure(stream_id: int):
    _failure_tracker.pop(stream_id, None)


# --- Proxy Activity Log ---
MAX_LOG_ENTRIES = 200
_proxy_log: deque = deque(maxlen=MAX_LOG_ENTRIES)


def _log_event(level: str, movie_id: int | None, msg: str, **extra):
    entry = {
        "ts": time.time(),
        "level": level,
        "movie_id": movie_id,
        "msg": msg,
        **extra,
    }
    _proxy_log.append(entry)


def get_proxy_log() -> list[dict]:
    return list(_proxy_log)


async def _check_if_stream_dead(movie_id: int) -> bool:
    """Check if a movie's stream has been failing repeatedly (marked as dead)."""
    try:
        from database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT stream_dead FROM movies WHERE id = ?", (movie_id,))
            result = await row.fetchone()
            return result and result["stream_dead"] == 1
        finally:
            await db.close()
    except Exception as e:
        logger.warning("Failed to check if stream is dead: %s", e)
        return False


async def probe_file_size(uuid: str, stream_id: int, account_id: int | None = None, ext: str = "mkv", movie_id: int | None = None) -> int | None:
    from stream_mapper import get_xc_url
    xc_path = get_xc_url(movie_id, ext) if movie_id else None

    if xc_path and movie_id:
        base_xc_url = f"{DISPATCHARR_URL}{xc_path}"
        cached = _get_cached_session(movie_id)
        if cached:
            session_id, resolved_url = cached
            parsed = urlparse(resolved_url)
            qs = parse_qs(parsed.query)
            qs["session_id"] = [session_id]
            new_query = urlencode(qs, doseq=True)
            upstream_url = urlunparse(parsed._replace(query=new_query))
        else:
            result = await _resolve_session(base_xc_url, movie_id)
            if result:
                _, resolved_url = result
                upstream_url = resolved_url
            else:
                upstream_url = base_xc_url
        headers = {"Range": "bytes=0-0"}
    elif xc_path:
        upstream_url = f"{DISPATCHARR_URL}{xc_path}"
        headers = {"Range": "bytes=0-0"}
    else:
        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
        headers = {"Range": "bytes=0-0"}
        if DISPATCHARR_API_KEY:
            headers["X-API-Key"] = DISPATCHARR_API_KEY
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(upstream_url, headers=headers)
            cr = resp.headers.get("content-range", "")
            if "/" in cr:
                total = cr.split("/")[-1]
                if total.isdigit():
                    return int(total)
    except Exception as e:
        logger.warning("probe_file_size failed for %s: %s", uuid, e)
    return None


def sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip('. ')[:200]


def make_filename(movie) -> str:
    clean = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', movie["name"]).strip()
    clean = re.sub(r'\s*\(\d{4}\)\s*$', '', clean).strip()
    if movie["year"]:
        base = sanitize_folder(f"{clean} ({movie['year']})")
    else:
        base = sanitize_folder(clean)
    return f"{base} [{movie['id']}].mp4"


def extract_movie_id(filename: str) -> int | None:
    m = re.search(r'\[(\d+)\]\.mp4$', filename)
    return int(m.group(1)) if m else None


FALLBACK_FILE_SIZE = 8_000_000_000


@router.api_route("/vod/", methods=["GET", "HEAD"])
async def vod_root(request: Request):
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT id, name, year FROM movies "
            "WHERE name != '' AND stream_id IS NOT NULL AND activated = 1 "
            "ORDER BY name"
        )
        movies = await rows.fetchall()

        links = []
        for m in movies:
            fname = make_filename(m)
            links.append(f'<a href="{quote(fname)}">{fname}</a>')

        html = "<html><body>\n" + "\n".join(links) + "\n</body></html>"
        return HTMLResponse(content=html)
    finally:
        await db.close()


@router.api_route("/vod/{filename:path}", methods=["GET", "HEAD"])
async def vod_file(filename: str, request: Request):
    movie_id = extract_movie_id(filename)
    if not movie_id:
        return Response(status_code=404, content="Invalid filename")

    db = await get_db()
    try:
        row = await db.execute(
            "SELECT uuid, stream_id, account_id, content_type, file_size, header_data, header_size, tail_data, tail_size, tail_offset FROM movies WHERE id = ?",
            (movie_id,),
        )
        movie = await row.fetchone()
        if not movie:
            return Response(status_code=404, content="Movie not found")

        uuid = movie["uuid"]
        stream_id = movie["stream_id"]

        if not stream_id:
            return Response(status_code=503, content="No stream mapping")

        content_type = movie["content_type"] or "video/x-matroska"

        file_size = movie["file_size"]
        if not file_size:
            acct_id = movie["account_id"]
            probe_ext = "mp4" if content_type == "video/mp4" else "mkv"
            file_size = await probe_file_size(uuid, stream_id, account_id=acct_id, ext=probe_ext, movie_id=movie_id)
            if file_size:
                await db.execute(
                    "UPDATE movies SET file_size = ? WHERE id = ?",
                    (file_size, movie_id),
                )
                await db.commit()
            else:
                file_size = FALLBACK_FILE_SIZE

        if request.method == "HEAD":
            return Response(
                status_code=200,
                headers={
                    "accept-ranges": "bytes",
                    "content-type": content_type,
                    "content-length": str(file_size),
                },
            )

        range_start = 0
        range_end = None
        if "range" in request.headers:
            m = re.match(r"bytes=(\d+)-(\d*)", request.headers["range"])
            if m:
                range_start = int(m.group(1))
                if m.group(2):
                    range_end = int(m.group(2))

        if range_end is None:
            range_end = file_size - 1

        header_data = movie["header_data"]
        header_size = movie["header_size"] or 0
        tail_data = movie["tail_data"]
        tail_size = movie["tail_size"] or 0
        tail_offset = movie["tail_offset"] or 0

        # Serve from cached head (first 5MB)
        if header_data and header_size > 0 and range_start < header_size:
            serve_end = min(range_end, header_size - 1)
            chunk = header_data[range_start:serve_end + 1]
            logger.info("Header cache hit: movie %d, %d bytes (range %d-%d)", movie_id, len(chunk), range_start, serve_end)
            _log_event("info", movie_id, "Header cache hit", bytes=len(chunk), range_start=range_start, range_end=serve_end)
            return Response(
                status_code=206,
                headers={
                    "accept-ranges": "bytes",
                    "content-type": content_type,
                    "content-length": str(len(chunk)),
                    "content-range": f"bytes {range_start}-{serve_end}/{file_size}",
                },
                content=chunk,
            )

        # Serve from cached tail (last 5MB)
        if tail_data and tail_size > 0 and range_start >= tail_offset:
            local_start = range_start - tail_offset
            local_end = min(range_end - tail_offset, tail_size - 1)
            if local_start < tail_size:
                chunk = tail_data[local_start:local_end + 1]
                serve_end = range_start + len(chunk) - 1
                logger.info("Tail cache hit: movie %d, %d bytes (range %d-%d)", movie_id, len(chunk), range_start, serve_end)
                _log_event("info", movie_id, "Tail cache hit", bytes=len(chunk), range_start=range_start, range_end=serve_end)
                return Response(
                    status_code=206,
                    headers={
                        "accept-ranges": "bytes",
                        "content-type": content_type,
                        "content-length": str(len(chunk)),
                        "content-range": f"bytes {range_start}-{serve_end}/{file_size}",
                    },
                    content=chunk,
                )

        if not _check_circuit(stream_id):
            cooldown_left = int(CIRCUIT_COOLDOWN - (time.time() - _failure_tracker[stream_id][1]))
            logger.warning("Circuit open for stream_id %d, retry in %ds", stream_id, cooldown_left)
            _log_event("warn", movie_id, "Circuit breaker open", stream_id=stream_id, retry_in=cooldown_left)

            # Check if this is a dead stream (repeated 500 errors from provider)
            # If so, return a user-friendly message instead of generic 503
            is_dead = await _check_if_stream_dead(movie_id)
            if is_dead:
                return Response(
                    status_code=410,  # 410 Gone - stream is permanently unavailable
                    headers={"X-Stream-Status": "dead"},
                    content="This movie's stream is no longer available. It will be removed from your library.",
                )

            return Response(
                status_code=503,
                headers={"Retry-After": str(cooldown_left)},
                content=f"Provider temporarily unavailable, retry in {cooldown_left}s",
            )

        account_id = movie["account_id"]
        ext = "mp4" if content_type == "video/mp4" else "mkv"
        _log_event("info", movie_id, "Stream proxy request", range_start=range_start, range_end=range_end, stream_id=stream_id)
        return await _stream_proxy(uuid, stream_id, content_type, file_size, range_start, range_end, movie_id, request, account_id=account_id, ext=ext)
    finally:
        await db.close()


async def _stream_proxy(uuid: str, stream_id: int, content_type: str, file_size: int, start: int, end: int, movie_id: int | None = None, request: Request | None = None, account_id: int | None = None, ext: str = "mkv"):
    chunk_length = end - start + 1

    from stream_mapper import get_xc_url
    xc_path = get_xc_url(movie_id, ext) if movie_id else None

    if xc_path and movie_id:
        base_xc_url = f"{DISPATCHARR_URL}{xc_path}"

        cached = _get_cached_session(movie_id)
        if cached:
            session_id, resolved_url = cached
            parsed = urlparse(resolved_url)
            qs = parse_qs(parsed.query)
            qs["session_id"] = [session_id]
            new_query = urlencode(qs, doseq=True)
            upstream_url = urlunparse(parsed._replace(query=new_query))
            logger.info("Reusing session %s for movie %d (range %d-%d)", session_id, movie_id, start, end)
        else:
            lock = _get_movie_lock(movie_id)
            async with lock:
                cached = _get_cached_session(movie_id)
                if cached:
                    session_id, resolved_url = cached
                    parsed = urlparse(resolved_url)
                    qs = parse_qs(parsed.query)
                    qs["session_id"] = [session_id]
                    new_query = urlencode(qs, doseq=True)
                    upstream_url = urlunparse(parsed._replace(query=new_query))
                else:
                    result = await _resolve_session(base_xc_url, movie_id)
                    if result:
                        session_id, resolved_url = result
                        upstream_url = resolved_url
                    else:
                        upstream_url = base_xc_url
    elif xc_path:
        upstream_url = f"{DISPATCHARR_URL}{xc_path}"
    else:
        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"

    headers = {"Range": f"bytes={start}-{end}"}
    if not xc_path and DISPATCHARR_API_KEY:
        headers["X-API-Key"] = DISPATCHARR_API_KEY

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=60, write=30, pool=30),
        follow_redirects=True,
    )

    try:
        resp = await client.send(
            client.build_request("GET", upstream_url, headers=headers),
            stream=True,
        )

        if resp.status_code >= 400:
            await resp.aclose()
            await client.aclose()

            if movie_id and resp.status_code in (401, 403, 404, 410):
                old_session = _get_cached_session(movie_id)
                if old_session:
                    logger.warning("Session expired for movie %d (HTTP %d), clearing cache", movie_id, resp.status_code)
                    clear_movie_session(movie_id)
                    _log_event("warn", movie_id, f"Session expired (HTTP {resp.status_code}), will get new session on next request", stream_id=stream_id)
                    return Response(status_code=503, headers={"Retry-After": "1"}, content="Session expired, retrying")

            logger.error("Upstream error %d for stream_id %d", resp.status_code, stream_id)
            _record_failure(stream_id)
            _log_event("error", movie_id, f"Upstream {resp.status_code}", stream_id=stream_id)

            if resp.status_code >= 500 and movie_id:
                try:
                    db = await get_db()
                    try:
                        await db.execute(
                            "UPDATE movies SET stream_dead_count = COALESCE(stream_dead_count, 0) + 1 WHERE id = ?",
                            (movie_id,),
                        )
                        row = await db.execute("SELECT stream_dead_count, name, year FROM movies WHERE id = ?", (movie_id,))
                        result = await row.fetchone()
                        if result:
                            count = result["stream_dead_count"]
                            if count >= 3:
                                from api import _deactivate_dead_movie
                                await _deactivate_dead_movie(movie_id, result["name"], result["year"], f"playback HTTP {resp.status_code}, strike {count}/3")
                                await db.execute(
                                    "UPDATE movies SET stream_dead = 1 WHERE id = ?",
                                    (movie_id,),
                                )
                                _log_event("warn", movie_id, f"Auto-deactivated: HTTP {resp.status_code}, strike {count}/3", stream_id=stream_id)
                            else:
                                _log_event("warn", movie_id, f"Stream error: HTTP {resp.status_code}, strike {count}/3", stream_id=stream_id)
                        await db.commit()
                    finally:
                        await db.close()
                except Exception as e:
                    logger.error("Failed to handle stream error for movie %d: %s", movie_id, e)

            return Response(status_code=502, content=f"Upstream returned {resp.status_code}")

        _clear_failure(stream_id)
        if movie_id:
            try:
                db = await get_db()
                try:
                    await db.execute(
                        "UPDATE movies SET stream_dead_count = 0 WHERE id = ? AND stream_dead_count > 0",
                        (movie_id,),
                    )
                    await db.commit()
                except Exception:
                    pass
                finally:
                    await db.close()
            except Exception:
                pass

        actual_length = resp.headers.get("content-length")
        if actual_length and actual_length.isdigit():
            chunk_length = int(actual_length)
            end = start + chunk_length - 1

        async def stream_body():
            try:
                async for chunk in resp.aiter_bytes(STREAM_CHUNK):
                    if request and await request.is_disconnected():
                        logger.info("Client disconnected, closing upstream for stream_id %d", stream_id)
                        _log_event("info", movie_id, "Client disconnected, upstream closed", stream_id=stream_id)
                        break
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()
                logger.debug("Upstream connection closed for stream_id %d", stream_id)

        return StreamingResponse(
            content=stream_body(),
            status_code=206,
            headers={
                "accept-ranges": "bytes",
                "content-type": content_type,
                "content-length": str(chunk_length),
                "content-range": f"bytes {start}-{end}/{file_size}",
            },
        )
    except Exception as e:
        await client.aclose()
        logger.error("Stream proxy failed: %s", e)
        _record_failure(stream_id)
        _log_event("error", None, f"Stream proxy failed: {e}", stream_id=stream_id)
        return Response(status_code=502, content=str(e))


@router.api_route("/stream/{movie_id}.mkv", methods=["GET", "HEAD"])
@router.api_route("/stream/{movie_id}.mp4", methods=["GET", "HEAD"])
async def stream_movie(movie_id: int, request: Request):
    return await vod_file(f"legacy [{movie_id}].mp4", request)
