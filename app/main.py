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
from error_screens import generate_error_screens
from config import REDIRECT_STREAMING
import auth
from connection import router as connection_router
import sources
from tmdb_routes import router as tmdb_router
import setup_scripts
import tmdb_local
import tmdb_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_VERSION = "0.41.2"


async def _restore_strm_files():
    """Regenerate STRM files for activated movies missing from disk."""
    import os
    from database import get_db
    from generator import write_strm_for_movie, get_folder_name
    from config import STRM_OUTPUT_DIR

    db = await get_db()
    rows = await db.execute("SELECT * FROM movies WHERE activated = 1")
    activated = [dict(r) for r in await rows.fetchall()]
    if not activated:
        return

    restored = 0
    for movie in activated:
        name = movie.get("name")
        if not name:
            continue
        folder_name = get_folder_name(movie)
        if not os.path.isdir(os.path.join(STRM_OUTPUT_DIR, folder_name)):
            await write_strm_for_movie(movie)
            restored += 1

    if restored:
        os.makedirs(STRM_OUTPUT_DIR, exist_ok=True)
        count = sum(1 for d in os.listdir(STRM_OUTPUT_DIR) if os.path.isdir(os.path.join(STRM_OUTPUT_DIR, d)))
        await db.execute("UPDATE sync_state SET active_strm_count = ? WHERE id = 1", (count,))
        await db.commit()
        logging.getLogger(__name__).info("Startup: restored %d STRM files for activated movies", restored)


async def _restore_series_files():
    """Regenerate Series/Season folders + NFOs for activated episodes missing from disk."""
    import os
    from database import get_db
    from series_generator import write_tvshow_and_season_dirs, write_episode_nfo_batch, get_series_folder_name
    from config import STRM_SERIES_OUTPUT_DIR

    db = await get_db()
    rows = await db.execute("SELECT DISTINCT series_id FROM episodes WHERE activated = 1")
    series_ids = [r["series_id"] for r in await rows.fetchall()]
    if not series_ids:
        return

    restored = 0
    for series_id in series_ids:
        series_row = await db.execute("SELECT * FROM series WHERE id = ?", (series_id,))
        series = await series_row.fetchone()
        if not series:
            continue
        series = dict(series)

        cat_row = await db.execute("SELECT * FROM series_categories WHERE id = ?", (series.get("category_id"),))
        category = await cat_row.fetchone()
        if not category:
            continue
        category = dict(category)

        folder_name = get_series_folder_name(series)
        series_path = os.path.join(STRM_SERIES_OUTPUT_DIR, category["strm_folder"], folder_name)
        if os.path.isdir(series_path):
            continue

        await write_tvshow_and_season_dirs(series, category)
        ep_rows = await db.execute("SELECT * FROM episodes WHERE series_id = ? AND activated = 1", (series_id,))
        episodes = [dict(r) for r in await ep_rows.fetchall()]
        await write_episode_nfo_batch(series, category, episodes)
        restored += 1

    if restored:
        logging.getLogger(__name__).info("Startup: restored %d series folders for activated episodes", restored)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await sources.startup()
    generate_error_screens()
    await _restore_strm_files()
    await _restore_series_files()
    start_pipe_manager()
    dead_scan_task = asyncio.create_task(start_dead_scan_scheduler())
    health_task = asyncio.create_task(health_check_scheduler())
    log_archive_task = asyncio.create_task(start_log_archive_scheduler())
    await asyncio.to_thread(tmdb_store.init_db)
    tmdb_task = asyncio.create_task(tmdb_local.fill_loop())
    sweep_task = None
    if REDIRECT_STREAMING:
        from redirect import session_cap_sweep_loop
        sweep_task = asyncio.create_task(session_cap_sweep_loop())
    yield
    dead_scan_task.cancel()
    health_task.cancel()
    log_archive_task.cancel()
    tmdb_task.cancel()
    if sweep_task:
        sweep_task.cancel()


app = FastAPI(title="VOD Plex Bridge", version=APP_VERSION, lifespan=lifespan)
app.middleware("http")(auth.gate)


@app.get("/version")
async def version():
    return {"version": APP_VERSION}
app.include_router(proxy_router)
app.include_router(api_router)
app.include_router(auth.router)
app.include_router(connection_router)
app.include_router(sources.router)
app.include_router(tmdb_router)
app.include_router(setup_scripts.open_router)
app.include_router(setup_scripts.router)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

templates = Jinja2Templates(directory="/app/templates")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
