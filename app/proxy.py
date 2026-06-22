import asyncio
import logging
import os
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY
from database import get_db
from cache import stream_cache

logger = logging.getLogger(__name__)
router = APIRouter()

READ_CHUNK = 65536
MAX_CONCURRENT_DOWNLOADS = 3
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


async def probe_file_size(uuid: str, stream_id: int) -> int | None:
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
            "SELECT uuid, stream_id, content_type, file_size FROM movies WHERE id = ?",
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
            file_size = await probe_file_size(uuid, stream_id)
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

        entry = await stream_cache.get_or_start(movie_id, uuid, stream_id, file_size)

        ready = await stream_cache.wait_for_bytes(entry, range_start, timeout=60.0)
        if not ready:
            if entry.download_failed:
                return Response(status_code=502, content=f"Upstream error: {entry.error_message}")
            return Response(status_code=504, content="Timeout waiting for stream data")

        actual_size = entry.file_size if entry.download_complete else file_size
        available_end = min(range_end, actual_size - 1)
        if not entry.download_complete:
            available_end = min(available_end, entry.bytes_downloaded - 1)

        if available_end < range_start:
            return Response(status_code=416, content="Range not satisfiable")

        chunk_length = available_end - range_start + 1

        response_headers = {
            "accept-ranges": "bytes",
            "content-type": content_type,
            "content-length": str(chunk_length),
            "content-range": f"bytes {range_start}-{available_end}/{actual_size}",
        }

        try:
            with open(entry.file_path, "rb") as f:
                f.seek(range_start)
                data = f.read(chunk_length)
        except FileNotFoundError:
            return Response(status_code=503, content="Cache file missing")

        return Response(
            status_code=206,
            headers=response_headers,
            content=data,
        )
    finally:
        await db.close()


@router.get("/api/cache/stats")
async def cache_stats():
    return stream_cache.stats()


@router.api_route("/stream/{movie_id}.mkv", methods=["GET", "HEAD"])
@router.api_route("/stream/{movie_id}.mp4", methods=["GET", "HEAD"])
async def stream_movie(movie_id: int, request: Request):
    return await vod_file(f"legacy [{movie_id}].mp4", request)
