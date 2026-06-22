import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from .config import DISPATCHARR_URL
from .database import get_db

router = APIRouter()

CHUNK_SIZE = 65536


@router.get("/stream/{movie_id}.mkv")
@router.get("/stream/{movie_id}.mp4")
async def stream_movie(movie_id: int, request: Request):
    db = await get_db()
    try:
        row = await db.execute(
            "SELECT uuid, stream_id, content_type FROM movies WHERE id = ?",
            (movie_id,),
        )
        movie = await row.fetchone()
        if not movie:
            return Response(status_code=404, content="Movie not found")

        uuid = movie["uuid"]
        stream_id = movie["stream_id"]
        content_type = movie["content_type"] or "video/x-matroska"

        upstream_url = f"{DISPATCHARR_URL}/proxy/vod/movie/{uuid}"
        if stream_id:
            upstream_url += f"?stream_id={stream_id}"

        headers = {}
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]

        client = httpx.AsyncClient(timeout=None, follow_redirects=True)
        upstream_resp = await client.send(
            client.build_request("GET", upstream_url, headers=headers),
            stream=True,
        )

        response_headers = {}
        for key in ("content-type", "content-length", "content-range", "accept-ranges"):
            if key in upstream_resp.headers:
                response_headers[key] = upstream_resp.headers[key]

        if "content-type" not in response_headers:
            response_headers["content-type"] = content_type

        async def stream_chunks():
            try:
                async for chunk in upstream_resp.aiter_bytes(CHUNK_SIZE):
                    yield chunk
            finally:
                await upstream_resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_chunks(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
        )
    finally:
        await db.close()
