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

APP_VERSION = "0.28.9"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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
