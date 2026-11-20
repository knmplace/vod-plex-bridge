"""Support tickets: collect a scrubbed report, let the user review it, seal it and post it to the private
tickets repo as an issue (issue number = ticket number VPB-n). Nothing is sent until the user submits, and
what is sent is exactly the previewed text (the browser only returns the collect_id and a note)."""
import logging
import re
import secrets as _secrets
import time
from datetime import datetime, timezone

import httpx

import ticket_collect
import ticket_seal
from database import get_db
from ticket_scrub import Scrubber

logger = logging.getLogger(__name__)

PENDING_TTL = 1800
PENDING_MAX = 5
CHUNK = 60000          # Gitea's issue/comment body limit is 65,535 characters
SEND_GAP = 60          # seconds between sends
NOTE_MAX = 4000

_pending: dict[str, dict] = {}
_last_send = 0.0


class TicketError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _prune():
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
        _pending.pop(k, None)
    while len(_pending) > PENDING_MAX:
        _pending.pop(min(_pending, key=lambda k: _pending[k]["at"]))


async def start_collect() -> dict:
    text, known = await ticket_collect.collect_report()
    scrubber = Scrubber(known)
    clean = scrubber.scrub(text)
    cid = _secrets.token_urlsafe(16)
    _prune()
    _pending[cid] = {"text": clean, "known": known, "at": time.time(), "consumed": None}
    _prune()
    return {"collect_id": cid, "text": clean, "replaced": scrubber.counts(), "bytes": len(clean.encode())}


def _title(note: str, version: str) -> str:
    words = " ".join(note.split())[:60] or "no note"
    return f"{datetime.now(timezone.utc):%Y-%m-%d} v{version} - {words}"


def _chunks(blob: str) -> list[str]:
    return [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)] or [""]


def _api(path: str) -> str:
    return f"{ticket_seal.TICKET_GITEA_URL.rstrip('/')}/api/v1/repos/{ticket_seal.TICKET_GITEA_REPO}{path}"


def _auth() -> dict:
    return {"Authorization": "token " + ticket_seal.TICKET_GITEA_TOKEN, "Accept": "application/json",
            "User-Agent": "vod-plex-bridge-ticket/1"}


async def _post_issue(title: str, body: str) -> int:
    async with httpx.AsyncClient(timeout=30, headers=_auth()) as c:
        r = await c.post(_api("/issues"), json={"title": title, "body": body})
        r.raise_for_status()
        return int(r.json()["number"])


async def _post_comment(number: int, body: str) -> None:
    async with httpx.AsyncClient(timeout=30, headers=_auth()) as c:
        r = await c.post(_api(f"/issues/{number}/comments"), json={"body": body})
        r.raise_for_status()


async def _send(blob: str, title: str) -> int:
    parts = _chunks(blob)
    number = await _post_issue(title, parts[0])
    for p in parts[1:]:
        await _post_comment(number, p)
    return number


def _row(r) -> dict:
    return {"id": r["id"], "number": r["number"], "ticket": f"VPB-{r['number']}" if r["number"] else None,
            "created_at": r["created_at"], "version": r["version"], "title": r["title"],
            "note": r["note"], "status": r["status"], "error": r["error"]}


async def submit(collect_id: str, note: str) -> dict:
    global _last_send
    _prune()
    item = _pending.get(collect_id)
    if item is None:
        raise TicketError("That report expired. Collect it again.", 410)
    db = await get_db()
    if item["consumed"]:
        r = await (await db.execute("SELECT * FROM tickets WHERE id=?", (item["consumed"],))).fetchone()
        return _row(r)
    if time.time() - _last_send < SEND_GAP:
        raise TicketError("Please wait a minute before sending another ticket", 429)
    note = Scrubber(item["known"]).scrub((note or "")[:NOTE_MAX])
    version = ticket_collect._app_version()
    title = _title(note, version)
    body = f"VOD Plex Bridge ticket\nversion: {version}\nnote: {note or '(none)'}\n\n{item['text']}"
    blob = ticket_seal.seal(body)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = await db.execute(
        "INSERT INTO tickets (created_at, version, title, note, status, blob, report) VALUES (?,?,?,?,?,?,?)",
        (now, version, title, note, "unsent", blob, item["text"]))
    await db.commit()
    tid = cur.lastrowid
    item["consumed"] = tid
    _last_send = time.time()
    await _try_send(tid, blob, title)
    r = await (await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))).fetchone()
    return _row(r)


async def _try_send(tid: int, blob: str, title: str) -> None:
    db = await get_db()
    try:
        number = await _send(blob, title)
        await db.execute("UPDATE tickets SET number=?, status='sent', error=NULL WHERE id=?", (number, tid))
        logger.info("Ticket VPB-%s sent", number)
    except Exception as e:
        msg = re.sub(r"https?://\S+", "<url>", f"{type(e).__name__}: {e}")[:300]
        await db.execute("UPDATE tickets SET status='unsent', error=? WHERE id=?", (msg, tid))
        logger.warning("Ticket send failed: %s", msg)
    await db.commit()


async def list_tickets() -> list[dict]:
    db = await get_db()
    rows = await (await db.execute("SELECT * FROM tickets ORDER BY id DESC")).fetchall()
    return [_row(r) for r in rows]


async def retry(tid: int) -> dict:
    db = await get_db()
    r = await (await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))).fetchone()
    if r is None:
        raise TicketError("No such ticket", 404)
    if r["status"] == "sent":
        return _row(r)
    await _try_send(tid, r["blob"], r["title"])
    r = await (await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))).fetchone()
    return _row(r)


async def get_ticket(tid: int) -> dict:
    """One ticket with the report text the user reviewed (None for tickets sent before it was kept)."""
    db = await get_db()
    r = await (await db.execute("SELECT * FROM tickets WHERE id=?", (tid,))).fetchone()
    if r is None:
        raise TicketError("No such ticket", 404)
    return {**_row(r), "report": r["report"]}


async def delete_tickets(ids: list) -> int:
    """Remove tickets from this bridge's list (the copy already sent to the support repo is untouched)."""
    clean = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:500]
    if not clean:
        return 0
    db = await get_db()
    cur = await db.execute(f"DELETE FROM tickets WHERE id IN ({','.join('?' * len(clean))})", clean)
    await db.commit()
    return cur.rowcount


async def blob_for_download(tid: int) -> str:
    db = await get_db()
    r = await (await db.execute("SELECT blob FROM tickets WHERE id=?", (tid,))).fetchone()
    if r is None:
        raise TicketError("No such ticket", 404)
    return r["blob"]
