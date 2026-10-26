"""Single app login: created on the first visit, then required for the UI and /api."""
import asyncio
import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from database import get_db

COOKIE = "vpb_session"
SESSION_SECONDS = 30 * 86400
# Plex, rclone and players fetch these without a browser login.
# /setup/<token>/ is for the host scripts from the Setup tab; those routes check the token themselves.
OPEN_PREFIXES = ("/vod/", "/vod-series/", "/stream/", "/static/", "/api/auth/", "/login", "/version", "/setup/")
LOGIN_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "login.html")

router = APIRouter()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got.hex(), digest)
    except ValueError:
        return False


async def _user():
    db = await get_db()
    return await (await db.execute("SELECT * FROM app_user ORDER BY id LIMIT 1")).fetchone()


async def session_user(token: str | None):
    if not token:
        return None
    db = await get_db()
    return await (await db.execute(
        "SELECT u.* FROM auth_sessions s JOIN app_user u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?",
        (token, time.time()))).fetchone()


async def _start_session(request: Request, user_id: int) -> JSONResponse:
    db = await get_db()
    token = secrets.token_urlsafe(32)
    await db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (time.time(),))
    await db.execute("INSERT INTO auth_sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                     (token, user_id, time.time() + SESSION_SECONDS))
    await db.commit()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https")
    return resp


def is_open(path: str) -> bool:
    return path.startswith(OPEN_PREFIXES)


async def gate(request: Request, call_next):
    """HTTP middleware: everything outside OPEN_PREFIXES needs a signed-in session."""
    if is_open(request.url.path) or await session_user(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Login required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def _credentials(body: dict) -> tuple[str, str]:
    return (body.get("username") or "").strip(), body.get("password") or ""


@router.get("/login")
async def login_page():
    return FileResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/api/auth/status")
async def auth_status(request: Request):
    user = await session_user(request.cookies.get(COOKIE))
    return {"setup_required": await _user() is None, "username": user["username"] if user else None}


@router.post("/api/auth/setup")
async def auth_setup(request: Request):
    username, password = _credentials(await request.json())
    if await _user() is not None:
        return JSONResponse({"error": "An account already exists — sign in instead"}, status_code=409)
    if not username or len(password) < 8:
        return JSONResponse({"error": "Enter a username and a password of at least 8 characters"}, status_code=400)
    db = await get_db()
    cur = await db.execute("INSERT INTO app_user (username, password_hash) VALUES (?, ?)",
                           (username, hash_password(password)))
    await db.commit()
    return await _start_session(request, cur.lastrowid)


@router.post("/api/auth/login")
async def auth_login(request: Request):
    username, password = _credentials(await request.json())
    user = await _user()
    if user is None:
        return JSONResponse({"error": "No account yet — create one first"}, status_code=409)
    if username.lower() != user["username"].lower() or not check_password(password, user["password_hash"]):
        await asyncio.sleep(1)  # slows password guessing
        return JSONResponse({"error": "Wrong username or password"}, status_code=401)
    return await _start_session(request, user["id"])


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    db = await get_db()
    await db.execute("DELETE FROM auth_sessions WHERE token = ?", (request.cookies.get(COOKIE),))
    await db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@router.post("/api/auth/account")
async def auth_account(request: Request):
    """Change the username and/or password; signs out every other browser."""
    user = await session_user(request.cookies.get(COOKIE))
    if user is None:
        return JSONResponse({"error": "Login required"}, status_code=401)
    body = await request.json()
    if not check_password(body.get("current_password") or "", user["password_hash"]):
        await asyncio.sleep(1)
        return JSONResponse({"error": "Current password is wrong"}, status_code=401)
    username = (body.get("username") or "").strip() or user["username"]
    new_password = body.get("new_password") or ""
    if new_password and len(new_password) < 8:
        return JSONResponse({"error": "New password must be at least 8 characters"}, status_code=400)
    db = await get_db()
    await db.execute("UPDATE app_user SET username = ?, password_hash = ? WHERE id = ?",
                     (username, hash_password(new_password) if new_password else user["password_hash"], user["id"]))
    if new_password:
        await db.execute("DELETE FROM auth_sessions WHERE user_id = ? AND token != ?",
                         (user["id"], request.cookies.get(COOKIE)))
    await db.commit()
    return {"ok": True, "username": username}
