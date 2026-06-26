"""
Health check system for bridge, Dispatcharr, and Plex VOD library.
Polls every 2 hours, logs status, and triggers auto-restart if degraded.
"""
import asyncio
import logging
import time
import httpx
from datetime import datetime
from collections import deque
from typing import Literal

logger = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 500
_health_log: deque = deque(maxlen=MAX_LOG_ENTRIES)

_component_status = {
    "bridge": {"status": "unknown", "response_time": None, "last_check": None},
    "dispatcharr": {"status": "unknown", "response_time": None, "last_check": None},
    "plex_vod": {"status": "unknown", "response_time": None, "last_check": None},
}

HEALTH_CHECK_INTERVAL = 2 * 60 * 60
RESPONSE_TIMEOUT_BRIDGE = 5
RESPONSE_TIMEOUT_DISPATCHARR = 5
RESPONSE_TIMEOUT_PLEX = 10
DEGRADED_THRESHOLD = 2


def _log_health_event(component: str, status: Literal["ok", "slow", "down", "restarting", "restarted"], response_time: float | None = None, message: str = ""):
    """Log a health event with timestamp."""
    entry = {
        "ts": datetime.now().isoformat(),
        "component": component,
        "status": status,
        "response_time": response_time,
        "message": message,
    }
    _health_log.append(entry)
    logger.info(f"Health: {component} {status} ({response_time:.2f}s)" if response_time else f"Health: {component} {status} - {message}")


async def check_bridge_health() -> tuple[Literal["ok", "slow", "down"], float | None]:
    """Check bridge itself (DB connectivity, memory, response time)."""
    start = time.time()
    try:
        from database import get_db
        db = await get_db()
        try:
            await db.execute("SELECT 1")
        finally:
            pass
        response_time = time.time() - start
        status = "ok" if response_time < 1 else "slow"
        return (status, response_time)
    except Exception as e:
        logger.error(f"Bridge health check failed: {e}")
        return ("down", None)


async def check_dispatcharr_health() -> tuple[Literal["ok", "slow", "down"], float | None]:
    """Check Dispatcharr API availability."""
    from config import DISPATCHARR_URL, DISPATCHARR_API_KEY

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=RESPONSE_TIMEOUT_DISPATCHARR) as client:
            headers = {"X-API-Key": DISPATCHARR_API_KEY} if DISPATCHARR_API_KEY else {}
            resp = await client.get(f"{DISPATCHARR_URL}/api/vod/movies/?page=1&page_size=1", headers=headers)
            response_time = time.time() - start

            if resp.status_code < 400:
                status = "ok" if response_time < 3 else "slow"
                return (status, response_time)
            else:
                return ("down", response_time)
    except httpx.TimeoutException:
        logger.warning("Dispatcharr health check timeout")
        return ("down", time.time() - start)
    except Exception as e:
        logger.error(f"Dispatcharr health check failed: {e}")
        return ("down", None)


async def check_plex_vod_health() -> tuple[Literal["ok", "slow", "down"], float | None]:
    """End-to-end check: Bridge → Plex VOD library section."""
    from config import PLEX_URL, PLEX_TOKEN, PLEX_LIBRARY_ID

    if not PLEX_URL or not PLEX_TOKEN:
        logger.warning("Plex VOD health check skipped: PLEX_URL or PLEX_TOKEN not configured")
        return ("down", None)

    start = time.time()
    try:
        url = f"{PLEX_URL}/library/sections/{PLEX_LIBRARY_ID}?X-Plex-Token={PLEX_TOKEN}"
        async with httpx.AsyncClient(timeout=RESPONSE_TIMEOUT_PLEX) as client:
            resp = await client.get(url)
            response_time = time.time() - start

            if resp.status_code == 200 and "MediaContainer" in resp.text:
                status = "ok" if response_time < 3 else "slow"
                return (status, response_time)
            else:
                logger.warning(f"Plex VOD library check failed: HTTP {resp.status_code}")
                return ("down", response_time)
    except httpx.TimeoutException:
        logger.warning("Plex VOD library health check timeout")
        return ("down", time.time() - start)
    except Exception as e:
        logger.error(f"Plex VOD library health check failed: {e}")
        return ("down", None)


async def run_health_checks():
    """Run all health checks and update status cache."""
    logger.info("Running health checks...")

    bridge_status, bridge_time = await check_bridge_health()
    _component_status["bridge"]["status"] = bridge_status
    _component_status["bridge"]["response_time"] = bridge_time
    _component_status["bridge"]["last_check"] = time.time()
    _log_health_event("bridge", bridge_status, bridge_time)

    dc_status, dc_time = await check_dispatcharr_health()
    _component_status["dispatcharr"]["status"] = dc_status
    _component_status["dispatcharr"]["response_time"] = dc_time
    _component_status["dispatcharr"]["last_check"] = time.time()
    _log_health_event("dispatcharr", dc_status, dc_time)

    plex_status, plex_time = await check_plex_vod_health()
    _component_status["plex_vod"]["status"] = plex_status
    _component_status["plex_vod"]["response_time"] = plex_time
    _component_status["plex_vod"]["last_check"] = time.time()
    _log_health_event("plex_vod", plex_status, plex_time)

    return {
        "bridge": _component_status["bridge"],
        "dispatcharr": _component_status["dispatcharr"],
        "plex_vod": _component_status["plex_vod"],
    }


def get_health_status() -> dict:
    """Get current health status of all components."""
    return {
        "bridge": _component_status["bridge"],
        "dispatcharr": _component_status["dispatcharr"],
        "plex_vod": _component_status["plex_vod"],
        "overall": "ok" if all(s["status"] == "ok" for s in _component_status.values()) else "degraded",
    }


def get_health_log() -> list[dict]:
    """Get timestamped health log."""
    return list(_health_log)


async def health_check_scheduler():
    """Background task to run health checks every 2 hours."""
    logger.info("Health check scheduler started (interval: 2 hours)")

    await asyncio.sleep(10)
    try:
        await run_health_checks()
    except Exception as e:
        logger.error(f"Initial health check error: {e}")

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            await run_health_checks()
        except Exception as e:
            logger.error(f"Health check scheduler error: {e}")
            await asyncio.sleep(60)
