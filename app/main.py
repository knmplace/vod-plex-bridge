import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from database import init_db
from proxy import router as proxy_router, start_pipe_manager
from api import router as api_router, start_dead_scan_scheduler, start_log_archive_scheduler
from health import health_check_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_VERSION = "0.30.1"


async def _restore_strm_files():
    """Regenerate STRM files for activated movies missing from disk."""
    import os
    import re
    from database import get_db
    from generator import write_strm_for_movie, sanitize_filename
    from config import STRM_OUTPUT_DIR

    db = await get_db()
    rows = await db.execute("SELECT * FROM movies WHERE activated = 1")
    activated = [dict(r) for r in await rows.fetchall()]
    if not activated:
        return

    restored = 0
    for movie in activated:
        name = movie.get("name")
        year = movie.get("year")
        if not name:
            continue
        clean_name = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', name).strip()
        clean_name = re.sub(r'\s*\(\d{4}\)\s*$', '', clean_name).strip()
        folder_name = sanitize_filename(f"{clean_name} ({year})" if year else clean_name)
        if not os.path.isdir(os.path.join(STRM_OUTPUT_DIR, folder_name)):
            await write_strm_for_movie(movie)
            restored += 1

    if restored:
        os.makedirs(STRM_OUTPUT_DIR, exist_ok=True)
        count = sum(1 for d in os.listdir(STRM_OUTPUT_DIR) if os.path.isdir(os.path.join(STRM_OUTPUT_DIR, d)))
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (count,))
        await db.commit()
        logging.getLogger(__name__).info("Startup: restored %d STRM files for activated movies", restored)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _restore_strm_files()
    start_pipe_manager()
    dead_scan_task = asyncio.create_task(start_dead_scan_scheduler())
    health_task = asyncio.create_task(health_check_scheduler())
    log_archive_task = asyncio.create_task(start_log_archive_scheduler())
    yield
    dead_scan_task.cancel()
    health_task.cancel()
    log_archive_task.cancel()


app = FastAPI(title="VOD Plex Bridge", version=APP_VERSION, lifespan=lifespan)


@app.get("/version")
async def version():
    return {"version": APP_VERSION}
app.include_router(proxy_router)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

templates = Jinja2Templates(directory="/app/templates")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
