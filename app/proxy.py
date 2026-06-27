import asyncio
import logging
import os
import re
import time
from collections import deque
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY
from database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

STREAM_CHUNK = 256 * 1024
BUFFER_DIR = "/data/buffers"

# --- Streaming Pipe ---
# One persistent connection per movie, throttled to match playback pace.
# Bridge stays ~60s ahead of Plex, reads from Dispatcharr only as needed.

PIPE_IDLE_TIMEOUT = 900     # 15 min with no activity at all → close everything
PLEX_IDLE_TIMEOUT = 30      # 30 sec with no Plex reads → disconnect upstream, keep buffer
BUFFER_MAX_AGE = 86400      # 24 hours — delete stale buffer files
BUFFER_CLEANUP_INTERVAL = 3600  # check every hour
PIPE_POLL_INTERVAL = 0.1    # seconds between checks when waiting for data
PIPE_POLL_MAX_WAIT = 30     # max seconds to wait for data before giving up
BUFFER_TARGET_SECS = 60     # adaptive throttle target — speeds up/slows down around this
DEFAULT_BITRATE = 500_000  # 4 Mbps = 500 KB/s fallback (bytes/sec)

os.makedirs(BUFFER_DIR, exist_ok=True)


class StreamPipe:
    """Manages one persistent streaming connection from Dispatcharr for a movie.

    Reads from upstream at a throttled pace matching video bitrate,
    staying ~60 seconds ahead of where Plex is currently reading.
    Connection stays open for the entire viewing session.
    """

    def __init__(self, movie_id: int, upstream_url: str, file_size: int,
                 duration_seconds: int | None = None, session_id: str | None = None,
                 start_offset: int = 0, stream_bitrate_kbps: int | None = None):
        self.movie_id = movie_id
        self.upstream_url = upstream_url
        self.session_id = session_id
        self.file_size = file_size
        self.duration_seconds = duration_seconds
        self.start_offset = start_offset
        self.buffer_path = os.path.join(BUFFER_DIR, f"movie_{movie_id}.buf")
        self.bytes_written: int = 0
        self.started = False
        self.finished = False
        self.error: str | None = None
        self.created_at = time.time()
        self.last_read_at = time.time()
        self.last_plex_read = time.time()
        self.plex_position: int = start_offset
        self._download_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._resp = None
        if stream_bitrate_kbps and stream_bitrate_kbps > 0:
            self.bytes_per_second = (stream_bitrate_kbps * 1000) / 8
            self.bitrate_source = "provider"
        elif duration_seconds and duration_seconds > 0 and file_size > 0:
            self.bytes_per_second = file_size / duration_seconds
            self.bitrate_source = "calculated"
        else:
            self.bytes_per_second = DEFAULT_BITRATE
            self.bitrate_source = "fallback"

    async def start(self, resume_offset: int | None = None):
        async with self._lock:
            if self.started:
                return
            self.started = True
            self._resume_offset = resume_offset

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30, read=600, write=30, pool=30),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        self._download_task = asyncio.create_task(self._download_loop())
        base_bps = self.bytes_per_second
        est_duration = (self.file_size / base_bps / 60) if base_bps > 0 else 0
        if resume_offset:
            logger.info("Pipe RESUMED for movie %d (cached=%dMB, resuming from offset %d, bitrate=%.0f B/s [%s])",
                         self.movie_id, self.bytes_written // (1024*1024), resume_offset, self.bytes_per_second, self.bitrate_source)
        else:
            logger.info("Pipe started for movie %d (bitrate=%.0f B/s [%s], adaptive throttle, est=%.0fmin, offset=%d)",
                         self.movie_id, self.bytes_per_second, self.bitrate_source,
                         est_duration, self.start_offset)

    def _adaptive_multiplier(self) -> float:
        """Smooth adaptive rate based on how far download is ahead of Plex."""
        if self.bytes_per_second <= 0:
            return 1.2
        total_downloaded = self.start_offset + self.bytes_written
        buffer_ahead_secs = (total_downloaded - self.plex_position) / self.bytes_per_second
        if buffer_ahead_secs < 10:
            return 1.5
        elif buffer_ahead_secs < 30:
            return 1.3
        elif buffer_ahead_secs < 60:
            return 1.2
        elif buffer_ahead_secs < 120:
            return 1.0
        else:
            return 0.8

    async def _download_loop(self):
        CHUNK_SIZE = 65536  # 64KB iteration size from the stream
        INITIAL_BURST = 2 * 1024 * 1024  # first 2MB at full speed
        LOG_INTERVAL = 30
        try:
            if self._resume_offset is not None:
                offset = self._resume_offset
                file_mode = "ab"
            else:
                offset = self.start_offset
                file_mode = "wb"
                with open(self.buffer_path + ".meta", "w") as mf:
                    mf.write(str(self.start_offset))

            range_header = f"bytes={offset}-"

            self._resp = await self._client.send(
                self._client.build_request("GET", self.upstream_url,
                                           headers={"Range": range_header}),
                stream=True,
            )

            if self._resp.status_code >= 400:
                self.error = f"HTTP {self._resp.status_code}"
                self.finished = True
                if self._resp.status_code >= 500:
                    _record_failure_by_movie(self.movie_id)
                logger.error("Pipe upstream error for movie %d: HTTP %d", self.movie_id, self._resp.status_code)
                return

            cr = self._resp.headers.get("content-range", "")
            if "/" in cr:
                total = cr.split("/")[-1]
                if total.isdigit():
                    self.file_size = int(total)

            _clear_failure_by_movie(self.movie_id)

            wall_start = time.monotonic()
            last_log = wall_start
            session_bytes = 0
            last_multiplier = 1.2

            with open(self.buffer_path, file_mode) as f:
                async for chunk in self._resp.aiter_bytes(CHUNK_SIZE):
                    f.write(chunk)
                    f.flush()
                    self.bytes_written += len(chunk)
                    session_bytes += len(chunk)
                    self.last_read_at = time.time()

                    if session_bytes > INITIAL_BURST and self.bytes_per_second > 0:
                        multiplier = self._adaptive_multiplier()
                        last_multiplier = multiplier
                        target_bps = self.bytes_per_second * multiplier
                        elapsed = time.monotonic() - wall_start
                        expected_time = session_bytes / target_bps
                        ahead = expected_time - elapsed
                        if ahead > 0.05:
                            await asyncio.sleep(ahead)

                    now = time.monotonic()
                    if now - last_log >= LOG_INTERVAL:
                        elapsed = now - wall_start
                        actual_rate = session_bytes / elapsed if elapsed > 0 else 0
                        total_buffered = self.start_offset + self.bytes_written
                        pct = (total_buffered / self.file_size * 100) if self.file_size else 0
                        buffer_ahead_secs = (total_buffered - self.plex_position) / self.bytes_per_second if self.bytes_per_second > 0 else 0
                        logger.info("Pipe movie %d: %.1f%% (%dMB / %dMB) @ %.0f KB/s (x%.1f, buf %.0fs ahead) elapsed %.0fs",
                                    self.movie_id, pct,
                                    total_buffered // (1024*1024), self.file_size // (1024*1024),
                                    actual_rate / 1024, last_multiplier, buffer_ahead_secs, elapsed)
                        last_log = now

            self.finished = True
            elapsed = time.monotonic() - wall_start
            logger.info("Pipe complete for movie %d: %d bytes this session (%d total) in %.0fs (avg %.0f KB/s)",
                        self.movie_id, session_bytes, self.bytes_written, elapsed,
                        (session_bytes / elapsed / 1024) if elapsed > 0 else 0)

        except asyncio.CancelledError:
            self.finished = True
            logger.info("Pipe cancelled for movie %d at %d bytes", self.movie_id, self.bytes_written)
        except Exception as e:
            self.error = str(e)
            self.finished = True
            logger.error("Pipe download error for movie %d: %s", self.movie_id, e)
        finally:
            try:
                if self._resp:
                    await self._resp.aclose()
            except Exception:
                pass

    async def read_range(self, start: int, end: int) -> tuple[bytes, int] | None:
        """Read buffered data for a range. Returns (data, actual_end) or None.

        Will wait for at least STREAM_CHUNK bytes (or finish) before returning.
        Returns whatever is available — Plex handles partial 206 responses.
        """
        self.last_read_at = time.time()
        self.last_plex_read = time.time()
        local_start = start - self.start_offset

        min_needed = local_start + STREAM_CHUNK

        waited = 0.0
        while self.bytes_written < min_needed and not self.finished and not self.error:
            await asyncio.sleep(PIPE_POLL_INTERVAL)
            waited += PIPE_POLL_INTERVAL
            if waited >= PIPE_POLL_MAX_WAIT:
                if self.bytes_written > local_start:
                    break
                logger.warning("Pipe read timeout for movie %d: needed offset %d, have %d",
                               self.movie_id, min_needed, self.bytes_written)
                return None

        if not os.path.exists(self.buffer_path):
            return None

        local_end = end - self.start_offset
        available_end = min(local_end, self.bytes_written - 1)
        if local_start > available_end or local_start < 0:
            return None

        self.plex_position = max(self.plex_position, self.start_offset + available_end + 1)

        try:
            with open(self.buffer_path, "rb") as f:
                f.seek(local_start)
                data = f.read(available_end - local_start + 1)
                actual_end = start + len(data) - 1
                return (data, actual_end)
        except Exception as e:
            logger.error("Pipe buffer read error for movie %d: %s", self.movie_id, e)
            return None

    def has_data_for(self, start: int, end: int) -> bool:
        if start < self.start_offset:
            return False
        local_start = start - self.start_offset
        return self.bytes_written > local_start

    @property
    def is_idle(self) -> bool:
        return (time.time() - self.last_read_at) > PIPE_IDLE_TIMEOUT

    @property
    def is_plex_idle(self) -> bool:
        return (time.time() - self.last_plex_read) > PLEX_IDLE_TIMEOUT

    async def close(self, keep_buffer: bool = True):
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
            try:
                await self._download_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self.finished = True
        if keep_buffer:
            logger.info("Pipe closed for movie %d (buffered %d bytes, buffer retained)", self.movie_id, self.bytes_written)
        else:
            self.cleanup_buffer()
            logger.info("Pipe closed for movie %d (buffer deleted)", self.movie_id)

    def cleanup_buffer(self):
        try:
            if os.path.exists(self.buffer_path):
                os.remove(self.buffer_path)
                logger.info("Buffer cleared for movie %d", self.movie_id)
        except Exception as e:
            logger.warning("Failed to remove buffer %s: %s", self.buffer_path, e)

    def buffer_end_offset(self) -> int:
        return self.start_offset + self.bytes_written

    def status_dict(self) -> dict:
        now = time.time()
        buffer_ahead = (self.start_offset + self.bytes_written) - self.plex_position
        total_downloaded = self.start_offset + self.bytes_written
        download_pct = round(total_downloaded / self.file_size * 100, 1) if self.file_size > 0 else 0
        plex_pct = round(self.plex_position / self.file_size * 100, 1) if self.file_size > 0 else 0
        elapsed = now - self.created_at
        actual_speed = round(self.bytes_written / elapsed) if elapsed > 1 else 0
        duration = self.duration_seconds or 0
        plex_time = round(duration * (self.plex_position / self.file_size)) if self.file_size > 0 and duration > 0 else 0
        remaining_time = max(0, duration - plex_time) if duration > 0 else 0
        rate_multiplier = self._adaptive_multiplier()
        return {
            "movie_id": self.movie_id,
            "session_id": self.session_id,
            "bytes_written": self.bytes_written,
            "file_size": self.file_size,
            "duration_seconds": duration,
            "start_offset": self.start_offset,
            "plex_position": self.plex_position,
            "total_downloaded": total_downloaded,
            "download_pct": download_pct,
            "plex_pct": plex_pct,
            "buffer_ahead_bytes": buffer_ahead,
            "buffer_ahead_secs": round(buffer_ahead / self.bytes_per_second, 1) if self.bytes_per_second > 0 else 0,
            "bitrate_bps": round(self.bytes_per_second),
            "bitrate_source": self.bitrate_source,
            "actual_speed_bps": actual_speed,
            "rate_multiplier": rate_multiplier,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "age_seconds": int(elapsed),
            "idle_seconds": int(now - self.last_read_at),
            "plex_idle_seconds": int(now - self.last_plex_read),
            "plex_time_seconds": plex_time,
            "remaining_seconds": remaining_time,
            "buffer_exists": os.path.exists(self.buffer_path),
        }


# --- Pipe Manager ---
_movie_pipes: dict[int, StreamPipe] = {}
_pipe_creating: dict[int, asyncio.Lock] = {}


def get_all_pipes() -> dict:
    return {mid: pipe.status_dict() for mid, pipe in _movie_pipes.items()}


async def _resolve_session(base_url: str, movie_id: int) -> tuple[str, str | None] | None:
    """Follow Dispatcharr's 301 redirect to get the session URL.

    Dispatcharr v0.27.0+ puts session_id in the URL path for /proxy/vod/ routes
    (e.g. /proxy/vod/movie/{uuid}/{session_id}?stream_id=X) and in the query
    string for XC endpoints. We check both locations.
    """
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(base_url, headers={"Range": "bytes=0-0"})
            if resp.status_code in (301, 302):
                location = resp.headers.get("location", "")
                if location:
                    parsed = urlparse(location)
                    qs = parse_qs(parsed.query)
                    session_id = qs.get("session_id", [None])[0]
                    if not session_id:
                        path_parts = parsed.path.rstrip("/").split("/")
                        for part in path_parts:
                            if part.startswith("vod_"):
                                session_id = part
                                break
                    if location.startswith("/"):
                        base = urlparse(DISPATCHARR_URL)
                        resolved = f"{base.scheme}://{base.netloc}{location}"
                    else:
                        resolved = location
                    p = urlparse(resolved)
                    clean_qs = {k: v[0] for k, v in parse_qs(p.query).items()}
                    resolved_clean = urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(clean_qs), p.fragment))
                    logger.info("Resolved session for movie %d: %s", movie_id, session_id)
                    return (resolved_clean, session_id)
            elif resp.status_code < 400:
                return (base_url, None)
            else:
                logger.error("Session resolve failed for movie %d: HTTP %d", movie_id, resp.status_code)
    except Exception as e:
        logger.error("Session resolve error for movie %d: %s", movie_id, e)
    return None


async def _get_or_create_pipe(movie_id: int, file_size: int, duration_seconds: int | None,
                               uuid: str | None = None, stream_id: int | None = None,
                               ext: str = "mkv", start_offset: int = 0,
                               stream_bitrate_kbps: int | None = None) -> StreamPipe | None:
    """Get existing active pipe or create a new one for this movie.

    Uses Dispatcharr's /proxy/vod/ endpoint which routes through nginx's
    streaming-optimized location (uwsgi_buffering off, 300s timeouts).
    """
    if movie_id not in _pipe_creating:
        _pipe_creating[movie_id] = asyncio.Lock()
    async with _pipe_creating[movie_id]:
        return await _create_pipe_locked(movie_id, file_size, duration_seconds,
                                          uuid, stream_id, ext, start_offset, stream_bitrate_kbps)


async def _create_pipe_locked(movie_id: int, file_size: int, duration_seconds: int | None,
                               uuid: str | None = None, stream_id: int | None = None,
                               ext: str = "mkv", start_offset: int = 0,
                               stream_bitrate_kbps: int | None = None) -> StreamPipe | None:
    existing = _movie_pipes.get(movie_id)

    if existing and not existing.error:
        if existing.has_data_for(start_offset, start_offset):
            existing.last_read_at = time.time()
            return existing
        if not existing.finished and start_offset >= existing.start_offset:
            expected_end = existing.start_offset + existing.bytes_written
            gap = start_offset - expected_end
            if gap < 5 * 1024 * 1024:
                existing.last_read_at = time.time()
                return existing
            logger.info("Plex seeked beyond buffer for movie %d (gap=%dKB), restarting pipe from offset %d",
                         movie_id, gap // 1024, start_offset)
        await existing.close(keep_buffer=False)
        del _movie_pipes[movie_id]
    elif existing and existing.error:
        await existing.close(keep_buffer=False)
        del _movie_pipes[movie_id]

    buffer_path = os.path.join(BUFFER_DIR, f"movie_{movie_id}.buf")
    cached_size = 0
    cached_start = 0
    if os.path.exists(buffer_path):
        cached_size = os.path.getsize(buffer_path)
        meta_path = buffer_path + ".meta"
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as mf:
                    cached_start = int(mf.read().strip())
            except Exception:
                cached_start = 0

    if cached_size > 0 and start_offset >= cached_start and start_offset < cached_start + cached_size:
        resume_offset = cached_start + cached_size
        logger.info("Resuming movie %d from cached buffer: %dMB cached (offset %d-%d), Plex at %d, new pipe from %d",
                     movie_id, cached_size // (1024*1024), cached_start, resume_offset, start_offset, resume_offset)
        actual_start = cached_start
        effective_offset = resume_offset
    else:
        if cached_size > 0:
            logger.info("Buffer exists for movie %d but doesn't cover offset %d (buffer: %d-%d), replacing",
                         movie_id, start_offset, cached_start, cached_start + cached_size)
            try:
                os.remove(buffer_path)
                meta_path = buffer_path + ".meta"
                if os.path.exists(meta_path):
                    os.remove(meta_path)
            except Exception:
                pass
        actual_start = start_offset
        effective_offset = start_offset
        cached_size = 0

    if not uuid or not stream_id:
        db = await get_db()
        try:
            row = await db.execute("SELECT uuid, stream_id FROM movies WHERE id = ?", (movie_id,))
            movie = await row.fetchone()
            if movie:
                uuid = uuid or str(movie["uuid"])
                stream_id = stream_id or movie["stream_id"]
        finally:
            pass

    if not uuid or not stream_id:
        logger.error("No uuid/stream_id for movie %d", movie_id)
        return None

    base_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"
    result = await _resolve_session(base_url, movie_id)
    if not result:
        logger.error("Session resolve failed for movie %d — cannot create pipe", movie_id)
        return None

    session_url, session_id = result

    disp_host = urlparse(DISPATCHARR_URL).netloc.split(":")[0]
    session_host = urlparse(session_url).netloc.split(":")[0]
    if session_host != disp_host:
        logger.error("Session URL for movie %d resolved to external host %s (expected %s) — BLOCKED to prevent VPN bypass",
                      movie_id, session_host, disp_host)
        return None

    pipe = StreamPipe(movie_id, session_url, file_size, duration_seconds, session_id, actual_start, stream_bitrate_kbps)
    if cached_size > 0 and actual_start != effective_offset:
        pipe.bytes_written = cached_size
    _movie_pipes[movie_id] = pipe
    await pipe.start(resume_offset=effective_offset if effective_offset != actual_start else None)
    return pipe


async def close_movie_pipe(movie_id: int, keep_buffer: bool = False):
    """Close a movie's pipe. Error paths should pass keep_buffer=False."""
    pipe = _movie_pipes.pop(movie_id, None)
    if pipe:
        await pipe.close(keep_buffer=keep_buffer)


async def _pipe_manager_loop():
    """Background task: manage pipe lifecycle.

    Plex idle (5s): disconnect upstream, keep buffer on disk for resume.
    Full idle (15 min): remove pipe object from memory, buffer stays for 24h.
    Error: delete buffer, mark dead if provider error.
    """
    while True:
        await asyncio.sleep(5)
        try:
            to_cleanup = []
            for mid, pipe in list(_movie_pipes.items()):
                if pipe.error:
                    to_cleanup.append((mid, "error"))
                elif pipe.is_plex_idle and pipe.started and pipe.finished:
                    to_cleanup.append((mid, "plex_idle"))
                elif pipe.is_idle and pipe.started:
                    to_cleanup.append((mid, "full_idle"))

            for mid, reason in to_cleanup:
                pipe = _movie_pipes.pop(mid, None)
                if pipe:
                    if reason == "plex_idle":
                        plex_idle = int(time.time() - pipe.last_plex_read)
                        logger.info("Plex idle %ds for movie %d — disconnecting upstream, buffer retained (%dMB)",
                                    plex_idle, mid, pipe.bytes_written // (1024*1024))
                        await pipe.close(keep_buffer=True)
                    elif reason == "error" and pipe.error and "HTTP 5" in pipe.error:
                        logger.warning("Closing errored pipe for movie %d (%s) — marking dead", mid, pipe.error)
                        try:
                            db = await get_db()
                            row = await db.execute("SELECT stream_id FROM movies WHERE id = ?", (mid,))
                            movie = await row.fetchone()
                            if movie:
                                status_code_val = int(pipe.error.split()[-1]) if pipe.error.split()[-1].isdigit() else 500
                                await _handle_upstream_error(mid, movie["stream_id"], status_code_val)
                        except Exception as he:
                            logger.error("Failed to mark movie %d dead from pipe manager: %s", mid, he)
                        await pipe.close(keep_buffer=False)
                    elif reason == "error":
                        logger.info("Closing errored pipe for movie %d (%s) — clearing buffer", mid, pipe.error)
                        await pipe.close(keep_buffer=False)
                    else:
                        idle_secs = int(time.time() - pipe.last_read_at)
                        logger.info("Full idle %ds for movie %d — removing from memory, buffer stays on disk", idle_secs, mid)
                        await pipe.close(keep_buffer=True)
        except Exception as e:
            logger.error("Pipe manager error: %s", e)


async def _buffer_cleanup_loop():
    """Hourly cleanup: delete buffer files older than 24 hours."""
    while True:
        await asyncio.sleep(BUFFER_CLEANUP_INTERVAL)
        try:
            now = time.time()
            cleaned = 0
            for fname in os.listdir(BUFFER_DIR):
                fpath = os.path.join(BUFFER_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                mtime = os.path.getmtime(fpath)
                if now - mtime > BUFFER_MAX_AGE:
                    movie_id_match = re.search(r'movie_(\d+)', fname)
                    mid = int(movie_id_match.group(1)) if movie_id_match else None
                    if mid and mid in _movie_pipes:
                        continue
                    os.remove(fpath)
                    cleaned += 1
            if cleaned:
                logger.info("Buffer cleanup: removed %d stale files (>24h)", cleaned)
        except Exception as e:
            logger.error("Buffer cleanup error: %s", e)


_pipe_manager_task: asyncio.Task | None = None
_buffer_cleanup_task: asyncio.Task | None = None


def start_pipe_manager():
    global _pipe_manager_task, _buffer_cleanup_task
    if _pipe_manager_task is None or _pipe_manager_task.done():
        _pipe_manager_task = asyncio.create_task(_pipe_manager_loop())
        logger.info("Pipe manager started (plex_idle=%ds, full_idle=%ds)", PLEX_IDLE_TIMEOUT, PIPE_IDLE_TIMEOUT)
    if _buffer_cleanup_task is None or _buffer_cleanup_task.done():
        _buffer_cleanup_task = asyncio.create_task(_buffer_cleanup_loop())
        logger.info("Buffer cleanup task started (max_age=%dh, interval=%dh)",
                     BUFFER_MAX_AGE // 3600, BUFFER_CLEANUP_INTERVAL // 3600)


# --- Circuit Breaker ---
_failure_tracker: dict[int, list] = {}
_movie_failure_tracker: dict[int, list] = {}
CIRCUIT_FAIL_THRESHOLD = 1
CIRCUIT_COOLDOWN = 300


def _check_circuit(stream_id: int) -> bool:
    record = _failure_tracker.get(stream_id)
    if not record:
        return True
    # Once tripped, stays tripped until container restart. Never auto-reset.
    return False


def _record_failure(stream_id: int):
    record = _failure_tracker.get(stream_id)
    now = time.time()
    if record:
        _failure_tracker[stream_id] = [record[0] + 1, now]
    else:
        _failure_tracker[stream_id] = [1, now]


def _clear_failure(stream_id: int):
    _failure_tracker.pop(stream_id, None)


def _record_failure_by_movie(movie_id: int):
    record = _movie_failure_tracker.get(movie_id)
    now = time.time()
    if record:
        _movie_failure_tracker[movie_id] = [record[0] + 1, now]
    else:
        _movie_failure_tracker[movie_id] = [1, now]


def _check_circuit_by_movie(movie_id: int) -> bool:
    record = _movie_failure_tracker.get(movie_id)
    if not record:
        return True
    # Once tripped, stays tripped until container restart. Never auto-reset.
    return False


def _clear_failure_by_movie(movie_id: int):
    _movie_failure_tracker.pop(movie_id, None)


# --- Proxy Activity Log ---
MAX_LOG_ENTRIES = 200
_proxy_log: deque = deque(maxlen=MAX_LOG_ENTRIES)


def _log_event(level: str, movie_id: int | None, msg: str, movie_name: str | None = None, **extra):
    entry = {
        "ts": time.time(),
        "level": level,
        "movie_id": movie_id,
        "movie_name": movie_name,
        "msg": msg,
        **extra,
    }
    _proxy_log.append(entry)


def get_proxy_log() -> list[dict]:
    return list(_proxy_log)


async def _check_if_stream_dead(movie_id: int) -> bool:
    try:
        from database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT stream_dead FROM movies WHERE id = ?", (movie_id,))
            result = await row.fetchone()
            return result and result["stream_dead"] == 1
        finally:
            pass
    except Exception as e:
        logger.warning("Failed to check if stream is dead: %s", e)
        return False


async def probe_file_size(uuid: str, stream_id: int, account_id: int | None = None, ext: str = "mkv", movie_id: int | None = None) -> int | None:
    base_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}?stream_id={stream_id}"

    result = await _resolve_session(base_url, movie_id or 0)
    if not result:
        logger.warning("probe_file_size: session resolve failed for %s", uuid)
        return None

    session_url, _ = result

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(session_url, headers={"Range": "bytes=0-0"})
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
        pass


@router.api_route("/vod/{filename:path}", methods=["GET", "HEAD"])
async def vod_file(filename: str, request: Request):
    movie_id = extract_movie_id(filename)
    if not movie_id:
        return Response(status_code=404, content="Invalid filename")

    db = await get_db()
    try:
        row = await db.execute(
            "SELECT uuid, stream_id, account_id, content_type, file_size, duration_seconds, stream_bitrate_kbps, "
            "name, header_data, header_size, tail_data, tail_size, tail_offset FROM movies WHERE id = ?",
            (movie_id,),
        )
        movie = await row.fetchone()
        if not movie:
            return Response(status_code=404, content="Movie not found")

        uuid = movie["uuid"]
        stream_id = movie["stream_id"]
        movie_name = movie["name"] or None

        if not stream_id:
            return Response(status_code=503, content="No stream mapping")

        content_type = movie["content_type"] or "video/x-matroska"

        file_size = movie["file_size"]
        if not file_size:
            acct_id = movie["account_id"]
            probe_ext = "mp4" if content_type == "video/mp4" else "mkv"
            file_size = await probe_file_size(uuid, stream_id, account_id=acct_id, ext=probe_ext, movie_id=movie_id)
            if file_size:
                await db.execute("UPDATE movies SET file_size = ? WHERE id = ?", (file_size, movie_id))
                await db.commit()
            else:
                file_size = FALLBACK_FILE_SIZE

        duration_seconds = movie["duration_seconds"]
        stream_bitrate_kbps = movie["stream_bitrate_kbps"]

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

        # --- Serve from cache FIRST (no provider connection needed) ---

        # Serve from cached head
        if header_data and header_size > 0 and range_start < header_size:
            serve_end = min(range_end, header_size - 1)
            chunk = header_data[range_start:serve_end + 1]
            _log_event("info", movie_id, "Served from cache (header)", movie_name=movie_name, bytes=len(chunk), range_start=range_start, range_end=serve_end)
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

        # Serve from cached tail
        if tail_data and tail_size > 0 and range_start >= tail_offset:
            local_start = range_start - tail_offset
            local_end = min(range_end - tail_offset, tail_size - 1)
            if local_start < tail_size:
                chunk = tail_data[local_start:local_end + 1]
                serve_end = range_start + len(chunk) - 1
                _log_event("info", movie_id, "Served from cache (tail)", movie_name=movie_name, bytes=len(chunk), range_start=range_start, range_end=serve_end)
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

        # --- Serve from disk buffer (no active pipe, but buffer file exists) ---
        buffer_path = os.path.join(BUFFER_DIR, f"movie_{movie_id}.buf")
        if not _movie_pipes.get(movie_id) and os.path.exists(buffer_path):
            buf_size = os.path.getsize(buffer_path)
            buf_start = 0
            meta_path = buffer_path + ".meta"
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as mf:
                        buf_start = int(mf.read().strip())
                except Exception:
                    pass
            local_start = range_start - buf_start
            if buf_size > 0 and local_start >= 0 and local_start < buf_size:
                local_end = min(range_end - buf_start, buf_size - 1)
                try:
                    with open(buffer_path, "rb") as bf:
                        bf.seek(local_start)
                        chunk = bf.read(min(STREAM_CHUNK, local_end - local_start + 1))
                    if chunk:
                        serve_end = range_start + len(chunk) - 1
                        os.utime(buffer_path)
                        _log_event("info", movie_id, f"Served from disk buffer ({buf_size // (1024*1024)}MB cached)",
                                   movie_name=movie_name, bytes=len(chunk), range_start=range_start, range_end=serve_end)
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
                except Exception as be:
                    logger.warning("Disk buffer read error for movie %d: %s", movie_id, be)

        # --- Need streaming pipe (create or resume) ---

        ext = "mp4" if content_type == "video/mp4" else "mkv"
        if not _movie_pipes.get(movie_id):
            await _get_or_create_pipe(movie_id, file_size, duration_seconds,
                                      uuid=uuid, stream_id=stream_id, ext=ext,
                                      start_offset=range_start, stream_bitrate_kbps=stream_bitrate_kbps)

        # Serve from live pipe (or completed pipe) if it has data for this range
        existing_pipe = _movie_pipes.get(movie_id)
        if existing_pipe and not existing_pipe.error and existing_pipe.has_data_for(range_start, range_start):
            result = await existing_pipe.read_range(range_start, range_end)
            if result:
                data, serve_end = result
                source = "Served from buffer" if existing_pipe.finished else "Served from pipe"
                _log_event("info", movie_id, source, movie_name=movie_name, bytes=len(data), range_start=range_start, range_end=serve_end)
                return Response(
                    status_code=206,
                    headers={
                        "accept-ranges": "bytes",
                        "content-type": content_type,
                        "content-length": str(len(data)),
                        "content-range": f"bytes {range_start}-{serve_end}/{file_size}",
                    },
                    content=data,
                )

        # Circuit breaker check
        if not _check_circuit(stream_id) or not _check_circuit_by_movie(movie_id):
            cooldown_record = _failure_tracker.get(stream_id) or _movie_failure_tracker.get(movie_id)
            cooldown_left = int(CIRCUIT_COOLDOWN - (time.time() - cooldown_record[1])) if cooldown_record else CIRCUIT_COOLDOWN

            is_dead = await _check_if_stream_dead(movie_id)
            if is_dead:
                return Response(
                    status_code=410,
                    headers={"X-Stream-Status": "dead"},
                    content="This movie's stream is no longer available.",
                )

            return Response(
                status_code=503,
                headers={"Retry-After": str(cooldown_left)},
                content=f"Provider temporarily unavailable, retry in {cooldown_left}s",
            )

        # --- Streaming pipe ---
        # Wrapped in try/except: ANY failure trips the circuit breaker
        # immediately. One strike, full stop, no retries to the provider.
        try:
            pipe = await _get_or_create_pipe(movie_id, file_size, duration_seconds,
                                               uuid=uuid, stream_id=stream_id, ext=ext,
                                               start_offset=range_start, stream_bitrate_kbps=stream_bitrate_kbps)

            if not pipe:
                _record_failure(stream_id)
                _record_failure_by_movie(movie_id)
                _log_event("error", movie_id, "Failed to create pipe — breaker tripped", movie_name=movie_name)
                await close_movie_pipe(movie_id)
                return Response(status_code=502, content="Could not connect to upstream")

            if pipe.error:
                _log_event("error", movie_id, f"Pipe error: {pipe.error} — breaker tripped", movie_name=movie_name)
                _record_failure(stream_id)
                _record_failure_by_movie(movie_id)
                if "HTTP 5" in pipe.error:
                    await _handle_upstream_error(movie_id, stream_id, int(pipe.error.split()[-1]) if pipe.error.split()[-1].isdigit() else 500)
                await close_movie_pipe(movie_id)
                return Response(status_code=502, content=f"Upstream error: {pipe.error}")

            result = await pipe.read_range(range_start, range_end)

            if result is None:
                _log_event("warn", movie_id, "Pipe returned no data — breaker tripped", movie_name=movie_name)
                _record_failure(stream_id)
                _record_failure_by_movie(movie_id)
                if pipe.error and "HTTP 5" in pipe.error:
                    status_code_val = int(pipe.error.split()[-1]) if pipe.error.split()[-1].isdigit() else 500
                    await _handle_upstream_error(movie_id, stream_id, status_code_val)
                await close_movie_pipe(movie_id)
                return Response(status_code=502, content="Stream data not available")

            data, serve_end = result
            buffer_ahead = (pipe.start_offset + pipe.bytes_written) - pipe.plex_position
            buffer_secs = round(buffer_ahead / pipe.bytes_per_second, 1) if pipe.bytes_per_second > 0 else 0
            _log_event("info", movie_id, "Served from pipe", movie_name=movie_name,
                       bytes=len(data), range_start=range_start, range_end=serve_end,
                       buffer_ahead_secs=buffer_secs)

            return Response(
                status_code=206,
                headers={
                    "accept-ranges": "bytes",
                    "content-type": content_type,
                    "content-length": str(len(data)),
                    "content-range": f"bytes {range_start}-{serve_end}/{file_size}",
                },
                content=data,
            )
        except Exception as e:
            logger.error("Pipe crash for movie %d: %s — breaker tripped", movie_id, e)
            _record_failure(stream_id)
            _record_failure_by_movie(movie_id)
            await close_movie_pipe(movie_id)
            return Response(status_code=502, content="Internal error")
    finally:
        pass


async def _handle_upstream_error(movie_id: int, stream_id: int, status_code: int):
    _record_failure(stream_id)
    _record_failure_by_movie(movie_id)
    logger.error("Upstream %d for movie %d stream_id %d — circuit breaker engaged",
                 status_code, movie_id, stream_id)
    _log_event("error", movie_id, f"Upstream {status_code} — blocked for {CIRCUIT_COOLDOWN}s",
               stream_id=stream_id)

    if status_code >= 500:
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
                    mname = result["name"] or None
                    from api import _deactivate_dead_movie
                    await _deactivate_dead_movie(movie_id, result["name"], result["year"],
                                                 f"playback HTTP {status_code}")
                    await db.execute("UPDATE movies SET stream_dead = 1 WHERE id = ?", (movie_id,))
                    _log_event("warn", movie_id, f"Auto-deactivated: HTTP {status_code} (1-strike)",
                               movie_name=mname, stream_id=stream_id)
                await db.commit()
            finally:
                pass
        except Exception as e:
            logger.error("Failed to handle stream error for movie %d: %s", movie_id, e)


@router.api_route("/stream/{movie_id}.mkv", methods=["GET", "HEAD"])
@router.api_route("/stream/{movie_id}.mp4", methods=["GET", "HEAD"])
async def stream_movie(movie_id: int, request: Request):
    return await vod_file(f"legacy [{movie_id}].mp4", request)
