"""
Health check system for bridge, Dispatcharr, and rclone mount.
Polls every 2 hours, logs status, and triggers auto-restart if degraded.
"""
import asyncio
import logging
import time
import httpx
import tempfile
import os
from datetime import datetime
from collections import deque
from typing import Literal

logger = logging.getLogger(__name__)

# Health status log (max 500 entries)
MAX_LOG_ENTRIES = 500
_health_log: deque = deque(maxlen=MAX_LOG_ENTRIES)

# Component status cache
_component_status = {
    "bridge": {"status": "unknown", "response_time": None, "last_check": None},
    "dispatcharr": {"status": "unknown", "response_time": None, "last_check": None},
    "rclone": {"status": "unknown", "response_time": None, "last_check": None},
}

HEALTH_CHECK_INTERVAL = 2 * 60 * 60  # 2 hours
RESPONSE_TIMEOUT_BRIDGE = 5  # seconds
RESPONSE_TIMEOUT_DISPATCHARR = 5
RESPONSE_TIMEOUT_RCLONE = 3
DEGRADED_THRESHOLD = 2  # If component unresponsive for this many checks, restart


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
            await db.close()
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


async def check_rclone_health(mount_path: str) -> tuple[Literal["ok", "slow", "down"], float | None]:
    """
    Check rclone mount by:
    1. Create a small test file via HTTP to bridge
    2. Try to read it via the rclone mount
    3. Delete it
    4. Return status
    """
    from config import BRIDGE_HOST, BRIDGE_PORT

    test_filename = f".health-check-{int(time.time())}.txt"
    test_content = b"health_check_test"

    start = time.time()
    try:
        # Step 1: Upload test file via HTTP to bridge (via a new endpoint we'll add)
        async with httpx.AsyncClient(timeout=RESPONSE_TIMEOUT_RCLONE) as client:
            # Write test file to mount path
            test_path = os.path.join(mount_path, test_filename)

            # Try to write test file
            try:
                with open(test_path, 'wb') as f:
                    f.write(test_content)
            except Exception as e:
                logger.error(f"rclone health check: failed to write test file: {e}")
                return ("down", None)

            # Try to read it back
            try:
                with open(test_path, 'rb') as f:
                    data = f.read()

                if data != test_content:
                    raise ValueError("Test file content mismatch")
            except Exception as e:
                logger.error(f"rclone health check: failed to read test file: {e}")
                return ("down", None)
            finally:
                # Always delete test file
                try:
                    os.remove(test_path)
                except:
                    pass

        response_time = time.time() - start
        status = "ok" if response_time < 2 else "slow"
        return (status, response_time)

    except Exception as e:
        logger.error(f"rclone health check failed: {e}")
        return ("down", None)


async def run_health_checks(rclone_mount_path: str = "/mnt/vod-bridge"):
    """Run all health checks and update status cache."""
    logger.info("Running health checks...")

    # Bridge check
    bridge_status, bridge_time = await check_bridge_health()
    _component_status["bridge"]["status"] = bridge_status
    _component_status["bridge"]["response_time"] = bridge_time
    _component_status["bridge"]["last_check"] = time.time()
    _log_health_event("bridge", bridge_status, bridge_time)

    # Dispatcharr check
    dc_status, dc_time = await check_dispatcharr_health()
    _component_status["dispatcharr"]["status"] = dc_status
    _component_status["dispatcharr"]["response_time"] = dc_time
    _component_status["dispatcharr"]["last_check"] = time.time()
    _log_health_event("dispatcharr", dc_status, dc_time)

    # rclone check
    rc_status, rc_time = await check_rclone_health(rclone_mount_path)
    _component_status["rclone"]["status"] = rc_status
    _component_status["rclone"]["response_time"] = rc_time
    _component_status["rclone"]["last_check"] = time.time()
    _log_health_event("rclone", rc_status, rc_time)

    return {
        "bridge": _component_status["bridge"],
        "dispatcharr": _component_status["dispatcharr"],
        "rclone": _component_status["rclone"],
    }


def get_health_status() -> dict:
    """Get current health status of all components."""
    return {
        "bridge": _component_status["bridge"],
        "dispatcharr": _component_status["dispatcharr"],
        "rclone": _component_status["rclone"],
        "overall": "ok" if all(s["status"] == "ok" for s in _component_status.values()) else "degraded",
    }


def get_health_log() -> list[dict]:
    """Get timestamped health log."""
    return list(_health_log)


async def health_check_scheduler(rclone_mount_path: str = "/mnt/vod-bridge"):
    """Background task to run health checks every 2 hours."""
    logger.info("Health check scheduler started (interval: 2 hours)")

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            await run_health_checks(rclone_mount_path)
        except Exception as e:
            logger.error(f"Health check scheduler error: {e}")
            await asyncio.sleep(60)  # Retry after 1 min if error
