"""Dispatcharr instances and provider (XC) accounts, listed in Settings.

The primary Dispatcharr instance is the one catalog sync and playback use: its values replace
DISPATCHARR_URL and the other connection names in config. Provider accounts are a record of the
logins behind Dispatcharr; the bridge never contacts a provider itself."""
import asyncio
import os
import re
import sqlite3

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import secret_box
from database import DB_PATH, get_db

router = APIRouter(prefix="/api/sources")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS dispatcharr_instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        url TEXT NOT NULL,
        api_key TEXT DEFAULT '',
        xc_username TEXT DEFAULT '',
        xc_password TEXT DEFAULT '',
        is_primary INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        base_url TEXT NOT NULL,
        username TEXT DEFAULT '',
        password TEXT DEFAULT '',
        max_streams INTEGER DEFAULT 1,
        priority INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
"""
# Encrypted columns, per table.
SECRETS = {"dispatcharr_instances": ("api_key", "xc_password"), "providers": ("password",)}
# Settings from before there could be several instances; moved into the tables once.
OLD_KEYS = ("CONNECTION_MODE", "DISPATCHARR_URL", "DISPATCHARR_API_KEY", "DISPATCHARR_XC_USERNAME",
            "DISPATCHARR_XC_PASSWORD", "XC_SERVER_URL", "XC_USERNAME", "XC_PASSWORD")
MOVED_MARK = "SOURCES_MOVED"
# (table, id, column) of saved secrets that could not be decrypted (settings key changed).
UNREADABLE = set()


def _plain(value: str) -> str:
    try:
        return secret_box.decrypt(value) if value else ""
    except ValueError:
        return ""


def _primary_values(row) -> dict:
    return {"DISPATCHARR_URL": row["url"], "DISPATCHARR_API_KEY": _plain(row["api_key"]),
            "DISPATCHARR_XC_USERNAME": row["xc_username"] or "", "DISPATCHARR_XC_PASSWORD": _plain(row["xc_password"])}


def _move_old_settings(conn: sqlite3.Connection):
    """Turn the single saved connection (or the environment's) into the first instance and provider."""
    try:
        if conn.execute("SELECT 1 FROM app_settings WHERE key=?", (MOVED_MARK,)).fetchone():
            return
        saved = dict(conn.execute(f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(OLD_KEYS))})",
                                  OLD_KEYS).fetchall())
    except sqlite3.OperationalError:
        return  # first start: app_settings does not exist until init_db, which runs this again

    def value(key):
        return _plain(saved.get(key, "")) or os.environ.get(key, "")

    def encrypted(key):
        return secret_box.encrypt(value(key)) if value(key) else ""

    if value("DISPATCHARR_URL") and not conn.execute("SELECT 1 FROM dispatcharr_instances").fetchone():
        conn.execute("INSERT INTO dispatcharr_instances (label, url, api_key, xc_username, xc_password, is_primary) "
                     "VALUES ('Dispatcharr', ?, ?, ?, ?, 1)",
                     (value("DISPATCHARR_URL").rstrip("/"), encrypted("DISPATCHARR_API_KEY"),
                      value("DISPATCHARR_XC_USERNAME"), encrypted("DISPATCHARR_XC_PASSWORD")))
    if value("XC_SERVER_URL") and not conn.execute("SELECT 1 FROM providers").fetchone():
        conn.execute("INSERT INTO providers (name, base_url, username, password) VALUES ('Provider', ?, ?, ?)",
                     (value("XC_SERVER_URL").rstrip("/"), value("XC_USERNAME"), encrypted("XC_PASSWORD")))
    conn.execute(f"DELETE FROM app_settings WHERE key IN ({','.join('?' * len(OLD_KEYS))})", OLD_KEYS)
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '1')", (MOVED_MARK,))


def _rotate(conn: sqlite3.Connection) -> bool:
    """Encrypt plain secrets and re-encrypt with the current key; False if any could not be read."""
    UNREADABLE.clear()
    for table, cols in SECRETS.items():
        for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
            for col, v in zip(cols, row[1:]):
                if not v:
                    continue
                try:
                    secret_box.decrypt(v)
                except ValueError:
                    UNREADABLE.add((table, row[0], col))
                    continue
                if not secret_box.is_encrypted(v):
                    new = secret_box.encrypt(v)
                elif not secret_box.on_primary_key(v):
                    new = secret_box.rotate(v)
                else:
                    continue
                conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (new, row[0]))
    return not UNREADABLE


def prepare(conn: sqlite3.Connection) -> tuple[dict, bool]:
    """Create the tables, move old settings in, re-encrypt secrets, and return the primary
    instance as config values (empty when there is none) and whether every secret was readable."""
    conn.executescript(SCHEMA)
    _move_old_settings(conn)
    readable = _rotate(conn)
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    row = cur.execute("SELECT * FROM dispatcharr_instances WHERE is_primary=1").fetchone()
    return (_primary_values(row) if row else {}), readable


def _startup_sync():
    import config  # config imports this module while it loads
    with sqlite3.connect(DB_PATH) as conn:
        values, _ = prepare(conn)
    if values:
        config.apply_settings(values)


async def startup():
    """After init_db: on a first start, config could not read these tables yet."""
    await asyncio.to_thread(_startup_sync)


async def _apply_primary():
    import config
    db = await get_db()
    row = await (await db.execute("SELECT * FROM dispatcharr_instances WHERE is_primary=1")).fetchone()
    config.apply_settings(_primary_values(row) if row else config.INSTANCE_ENV)


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _unreadable(table: str, row_id: int) -> list:
    return sorted(c for t, i, c in UNREADABLE if t == table and i == row_id)


def _secrets(table: str, body: dict, current) -> dict:
    """Encrypt typed secrets; a blank one keeps the saved value unless it is listed in _reset."""
    reset = set(body.get("_reset") or [])
    out = {}
    for col in SECRETS[table]:
        typed = str(body.get(col) or "").strip()
        if typed:
            out[col] = secret_box.encrypt(typed)
        elif current is not None and col not in reset:
            out[col] = current[col] or ""
        else:
            out[col] = ""
        if current is not None and (typed or col in reset):
            UNREADABLE.discard((table, current["id"], col))
    return out


async def _row(table: str, row_id: int):
    db = await get_db()
    return await (await db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,))).fetchone()


# --- Dispatcharr instances ---

@router.get("/dispatcharr")
async def list_instances():
    db = await get_db()
    rows = await (await db.execute("SELECT * FROM dispatcharr_instances ORDER BY is_primary DESC, label")).fetchall()
    return [{"id": r["id"], "label": r["label"], "url": r["url"], "xc_username": r["xc_username"] or "",
             "has_api_key": bool(r["api_key"]), "has_xc_password": bool(r["xc_password"]),
             "primary": bool(r["is_primary"]), "unreadable": _unreadable("dispatcharr_instances", r["id"])}
            for r in rows]


def _instance_fields(body: dict) -> tuple[dict, str | None]:
    v = {k: str(body.get(k) or "").strip() for k in ("label", "url", "xc_username")}
    v["url"] = v["url"].rstrip("/")
    if not v["label"]:
        return v, "Give the instance a name"
    if not re.match(r"https?://", v["url"]):
        return v, "URL must start with http:// or https://"
    return v, None


@router.post("/dispatcharr")
async def add_instance(request: Request):
    body = await request.json()
    v, error = _instance_fields(body)
    if error:
        return _bad(error)
    s = _secrets("dispatcharr_instances", body, None)
    db = await get_db()
    first = not await (await db.execute("SELECT 1 FROM dispatcharr_instances")).fetchone()
    await db.execute("INSERT INTO dispatcharr_instances (label, url, api_key, xc_username, xc_password, is_primary) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (v["label"], v["url"], s["api_key"], v["xc_username"], s["xc_password"], int(first)))
    await db.commit()
    await _apply_primary()
    return await list_instances()


@router.put("/dispatcharr/{instance_id}")
async def update_instance(instance_id: int, request: Request):
    current = await _row("dispatcharr_instances", instance_id)
    if not current:
        return _bad("Instance not found", 404)
    body = await request.json()
    v, error = _instance_fields(body)
    if error:
        return _bad(error)
    s = _secrets("dispatcharr_instances", body, current)
    db = await get_db()
    await db.execute("UPDATE dispatcharr_instances SET label=?, url=?, api_key=?, xc_username=?, xc_password=? "
                     "WHERE id=?",
                     (v["label"], v["url"], s["api_key"], v["xc_username"], s["xc_password"], instance_id))
    await db.commit()
    await _apply_primary()
    return await list_instances()


@router.post("/dispatcharr/{instance_id}/primary")
async def make_primary(instance_id: int):
    if not await _row("dispatcharr_instances", instance_id):
        return _bad("Instance not found", 404)
    db = await get_db()
    await db.execute("UPDATE dispatcharr_instances SET is_primary = (id = ?)", (instance_id,))
    await db.commit()
    await _apply_primary()
    return await list_instances()


@router.delete("/dispatcharr/{instance_id}")
async def delete_instance(instance_id: int):
    current = await _row("dispatcharr_instances", instance_id)
    if not current:
        return _bad("Instance not found", 404)
    db = await get_db()
    await db.execute("DELETE FROM dispatcharr_instances WHERE id=?", (instance_id,))
    if current["is_primary"]:
        await db.execute("UPDATE dispatcharr_instances SET is_primary=1 "
                         "WHERE id = (SELECT MIN(id) FROM dispatcharr_instances)")
    await db.commit()
    await _apply_primary()
    return await list_instances()


@router.post("/dispatcharr/test")
async def test_instance(request: Request):
    """Test the form's values; a blank URL or key uses the saved instance's."""
    body = await request.json()
    url = str(body.get("url") or "").strip().rstrip("/")
    key = str(body.get("api_key") or "").strip()
    if body.get("id"):
        row = await _row("dispatcharr_instances", int(body["id"]))
        if row:
            url = url or row["url"]
            key = key or _plain(row["api_key"])
    if not re.match(r"https?://", url):
        return {"ok": False, "message": "Enter a URL starting with http:// or https://"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/api/vod/movies/", params={"page_size": 1},
                                    headers={"X-API-Key": key} if key else {})
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Could not connect: {type(e).__name__}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "message": "Dispatcharr refused the API key"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"Dispatcharr answered HTTP {resp.status_code}"}
    count = resp.json().get("count")
    return {"ok": True, "message": "Connected" + (f" — {count:,} movies" if count is not None else "")}


# --- Provider (XC) accounts: stored only, never contacted ---

@router.get("/providers")
async def list_providers():
    db = await get_db()
    rows = await (await db.execute("SELECT * FROM providers ORDER BY is_active DESC, priority, name")).fetchall()
    return [{"id": r["id"], "name": r["name"], "base_url": r["base_url"], "username": r["username"] or "",
             "has_password": bool(r["password"]), "max_streams": r["max_streams"], "priority": r["priority"],
             "active": bool(r["is_active"]), "unreadable": _unreadable("providers", r["id"])}
            for r in rows]


def _provider_fields(body: dict) -> tuple[dict, str | None]:
    v = {k: str(body.get(k) or "").strip() for k in ("name", "base_url", "username", "max_streams", "priority")}
    v["base_url"] = v["base_url"].rstrip("/")
    if not v["name"]:
        return v, "Give the account a name"
    if not re.match(r"https?://", v["base_url"]):
        return v, "Server URL must start with http:// or https://"
    if not v["max_streams"].isdigit() or not 1 <= int(v["max_streams"]) <= 100:
        return v, "Max streams must be a number from 1 to 100"
    if not v["priority"].isdigit() or not 1 <= int(v["priority"]) <= 999:
        return v, "Priority must be a number from 1 to 999"
    return v, None


async def _save_provider(sql: str, params: tuple):
    db = await get_db()
    try:
        await db.execute(sql, params)
    except sqlite3.IntegrityError:
        return _bad("An account with that name already exists")
    await db.commit()
    return await list_providers()


@router.post("/providers")
async def add_provider(request: Request):
    body = await request.json()
    v, error = _provider_fields(body)
    if error:
        return _bad(error)
    s = _secrets("providers", body, None)
    return await _save_provider(
        "INSERT INTO providers (name, base_url, username, password, max_streams, priority) VALUES (?, ?, ?, ?, ?, ?)",
        (v["name"], v["base_url"], v["username"], s["password"], int(v["max_streams"]), int(v["priority"])))


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: int, request: Request):
    current = await _row("providers", provider_id)
    if not current:
        return _bad("Account not found", 404)
    body = await request.json()
    v, error = _provider_fields(body)
    if error:
        return _bad(error)
    s = _secrets("providers", body, current)
    return await _save_provider(
        "UPDATE providers SET name=?, base_url=?, username=?, password=?, max_streams=?, priority=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (v["name"], v["base_url"], v["username"], s["password"], int(v["max_streams"]), int(v["priority"]),
         provider_id))


@router.post("/providers/{provider_id}/active")
async def set_provider_active(provider_id: int, request: Request):
    if not await _row("providers", provider_id):
        return _bad("Account not found", 404)
    active = bool((await request.json()).get("active"))
    return await _save_provider("UPDATE providers SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                (int(active), provider_id))


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int):
    db = await get_db()
    await db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    await db.commit()
    return await list_providers()
