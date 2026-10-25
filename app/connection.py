"""Settings-tab connection setup: Dispatcharr (URL + API key + XC login) or a provider's XC login."""
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import config
from database import get_db

router = APIRouter(prefix="/api/connection")

SECRETS = ("DISPATCHARR_API_KEY", "DISPATCHARR_XC_PASSWORD", "XC_PASSWORD")
MODES = ("dispatcharr", "xc")


async def _saved() -> dict:
    db = await get_db()
    return {r["key"]: r["value"] for r in await (await db.execute("SELECT key, value FROM app_settings")).fetchall()}


@router.get("")
async def get_connection():
    saved = await _saved()
    out = {}
    for k in config.CONNECTION_KEYS:
        value = getattr(config, k)
        source = "settings" if saved.get(k) else ("compose" if os.environ.get(k) else "")
        # Secrets never leave the server; the form only learns whether one is set.
        out[k] = {"set": bool(value), "source": source} if k in SECRETS else {"value": value, "source": source}
    return out


def _merge(body: dict) -> dict:
    """The form's values, with blank secret fields meaning 'keep the current one'."""
    merged = {k: getattr(config, k) for k in config.CONNECTION_KEYS}
    for k in config.CONNECTION_KEYS:
        v = (body.get(k) or "").strip()
        if v or k not in SECRETS:
            merged[k] = v
    return merged


@router.post("")
async def save_connection(request: Request):
    values = _merge(await request.json())
    if values["CONNECTION_MODE"] not in MODES:
        return JSONResponse({"error": "Pick Dispatcharr or XC"}, status_code=400)
    if values["CONNECTION_MODE"] == "dispatcharr" and not values["DISPATCHARR_URL"]:
        return JSONResponse({"error": "Dispatcharr URL is required"}, status_code=400)
    if values["CONNECTION_MODE"] == "xc" and not all(values[k] for k in ("XC_SERVER_URL", "XC_USERNAME", "XC_PASSWORD")):
        return JSONResponse({"error": "Server URL, username and password are required"}, status_code=400)
    db = await get_db()
    await db.executemany("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                         [(k, v) for k, v in values.items() if v])
    await db.commit()
    config.apply_settings(values)
    return await get_connection()


@router.post("/test")
async def test_connection(request: Request):
    values = _merge(await request.json())
    if values["CONNECTION_MODE"] == "xc":
        # The bridge is not behind the VPN, so it never contacts a provider itself.
        return {"ok": False, "message": "Not tested: the bridge does not contact providers directly. "
                                        "Credentials are checked when Dispatcharr uses them."}
    url = values["DISPATCHARR_URL"].rstrip("/")
    headers = {"X-API-Key": values["DISPATCHARR_API_KEY"]} if values["DISPATCHARR_API_KEY"] else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/api/vod/movies/", params={"page_size": 1}, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Could not reach {url}: {type(e).__name__}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "message": "Dispatcharr refused the API key"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"Dispatcharr answered HTTP {resp.status_code}"}
    count = resp.json().get("count")
    return {"ok": True, "message": "Connected to Dispatcharr" + (f" — {count:,} movies" if count is not None else "")}
