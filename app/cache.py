import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY, CACHE_DIR, CACHE_MAX_BYTES, CACHE_IDLE_SECONDS

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK = 256 * 1024  # 256KB chunks from upstream


@dataclass
class CacheEntry:
    movie_id: int
    uuid: str
    stream_id: int
    file_path: str
    file_size: int = 0
    bytes_downloaded: int = 0
    last_accessed: float = field(default_factory=time.time)
    download_complete: bool = False
    download_failed: bool = False
    error_message: str = ""
    _task: asyncio.Task | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class StreamCache:
    def __init__(self):
        self._entries: dict[int, CacheEntry] = {}
        self._cleanup_task: asyncio.Task | None = None
        os.makedirs(CACHE_DIR, exist_ok=True)

    async def start(self):
        for f in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, f)
            if os.path.isfile(path):
                os.remove(path)
                logger.info("Cleaned stale cache file: %s", f)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for entry in list(self._entries.values()):
            if entry._task and not entry._task.done():
                entry._task.cancel()
            if os.path.exists(entry.file_path):
                os.remove(entry.file_path)
        self._entries.clear()

    def get_entry(self, movie_id: int) -> CacheEntry | None:
        entry = self._entries.get(movie_id)
        if entry:
            entry.last_accessed = time.time()
        return entry

    async def get_or_start(self, movie_id: int, uuid: str, stream_id: int, file_size: int) -> CacheEntry:
        entry = self._entries.get(movie_id)
        if entry and not entry.download_failed:
            entry.last_accessed = time.time()
            return entry

        if entry and entry.download_failed:
            if entry._task and not entry._task.done():
                entry._task.cancel()
            if os.path.exists(entry.file_path):
                os.remove(entry.file_path)
            del self._entries[movie_id]

        file_path = os.path.join(CACHE_DIR, f"{movie_id}.mp4")
        entry = CacheEntry(
            movie_id=movie_id,
            uuid=uuid,
            stream_id=stream_id,
            file_path=file_path,
            file_size=file_size,
        )
        self._entries[movie_id] = entry

        self._enforce_max_size(exclude=movie_id)

        entry._task = asyncio.create_task(self._download(entry))
        return entry

    async def wait_for_bytes(self, entry: CacheEntry, needed_end: int, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while entry.bytes_downloaded <= needed_end and not entry.download_complete and not entry.download_failed:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(0.1)
        if entry.download_failed:
            return False
        return entry.bytes_downloaded > needed_end or entry.download_complete

    async def _download(self, entry: CacheEntry):
        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{entry.uuid}?stream_id={entry.stream_id}"
        headers = {}
        if DISPATCHARR_API_KEY:
            headers["X-API-Key"] = DISPATCHARR_API_KEY

        logger.info("Cache download starting: movie %d (%s)", entry.movie_id, upstream_url)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30),
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", upstream_url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        entry.download_failed = True
                        entry.error_message = f"Upstream returned {resp.status_code}"
                        logger.error("Cache download failed for movie %d: HTTP %d", entry.movie_id, resp.status_code)
                        return

                    content_length = resp.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        entry.file_size = int(content_length)

                    with open(entry.file_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK):
                            if asyncio.current_task().cancelled():
                                raise asyncio.CancelledError()
                            f.write(chunk)
                            entry.bytes_downloaded += len(chunk)

            entry.download_complete = True
            actual_size = os.path.getsize(entry.file_path)
            entry.file_size = actual_size
            entry.bytes_downloaded = actual_size
            logger.info(
                "Cache download complete: movie %d, %.1f MB",
                entry.movie_id, actual_size / (1024 * 1024),
            )

        except asyncio.CancelledError:
            logger.info("Cache download cancelled: movie %d", entry.movie_id)
            entry.download_failed = True
            entry.error_message = "Cancelled"
        except Exception as e:
            entry.download_failed = True
            entry.error_message = str(e)
            logger.error("Cache download error for movie %d: %s", entry.movie_id, e)

    def _enforce_max_size(self, exclude: int = None):
        total = sum(
            os.path.getsize(e.file_path)
            for e in self._entries.values()
            if e.movie_id != exclude and os.path.exists(e.file_path)
        )
        if total < CACHE_MAX_BYTES:
            return

        by_access = sorted(
            [e for e in self._entries.values() if e.movie_id != exclude],
            key=lambda e: e.last_accessed,
        )
        for entry in by_access:
            if total < CACHE_MAX_BYTES:
                break
            size = os.path.getsize(entry.file_path) if os.path.exists(entry.file_path) else 0
            self._evict(entry)
            total -= size

    def _evict(self, entry: CacheEntry):
        logger.info("Evicting cache: movie %d", entry.movie_id)
        if entry._task and not entry._task.done():
            entry._task.cancel()
        if os.path.exists(entry.file_path):
            os.remove(entry.file_path)
        self._entries.pop(entry.movie_id, None)

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(300)
            try:
                now = time.time()
                to_evict = [
                    e for e in list(self._entries.values())
                    if now - e.last_accessed > CACHE_IDLE_SECONDS
                ]
                for entry in to_evict:
                    logger.info("Cache idle eviction: movie %d (idle %.0fs)", entry.movie_id, now - entry.last_accessed)
                    self._evict(entry)
            except Exception as e:
                logger.error("Cache cleanup error: %s", e)

    def stats(self) -> dict:
        total_cached = sum(
            os.path.getsize(e.file_path)
            for e in self._entries.values()
            if os.path.exists(e.file_path)
        )
        return {
            "entries": len(self._entries),
            "total_cached_mb": round(total_cached / (1024 * 1024), 1),
            "max_cache_mb": round(CACHE_MAX_BYTES / (1024 * 1024), 1),
            "active_downloads": sum(
                1 for e in self._entries.values()
                if not e.download_complete and not e.download_failed
            ),
            "movies": {
                e.movie_id: {
                    "downloaded_mb": round(e.bytes_downloaded / (1024 * 1024), 1),
                    "total_mb": round(e.file_size / (1024 * 1024), 1) if e.file_size else None,
                    "complete": e.download_complete,
                    "failed": e.download_failed,
                    "idle_seconds": round(time.time() - e.last_accessed),
                }
                for e in self._entries.values()
            },
        }


stream_cache = StreamCache()
