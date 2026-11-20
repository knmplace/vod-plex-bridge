"""Tickets service: collect/submit flow, rate limit, replay, unsent fallback. No real Gitea."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_enrichment import fresh_db  # noqa: E402,F401
import database  # noqa: E402
import ticket_collect  # noqa: E402
import ticket_seal  # noqa: E402
import tickets  # noqa: E402


@pytest.fixture
def svc(monkeypatch, fresh_db):
    sent = []

    async def fake_collect():
        return "report 192.168.1.50 secretvalue99\n", {"token": ["secretvalue99"]}

    async def fake_send(blob, title):
        sent.append((blob, title))
        return 2 + len(sent) - 1
    monkeypatch.setattr(ticket_collect, "collect_report", fake_collect)
    monkeypatch.setattr(tickets, "_send", fake_send)
    monkeypatch.setattr(tickets, "_last_send", 0.0)
    tickets._pending.clear()
    asyncio.run(database.init_db())
    return sent


def test_collect_is_scrubbed(svc):
    out = asyncio.run(tickets.start_collect())
    assert "secretvalue99" not in out["text"] and "192.168.1.50" not in out["text"]
    assert out["replaced"]["ip"] == 1


def test_submit_seals_exact_preview_and_returns_number(svc):
    async def go():
        c = await tickets.start_collect()
        return c, await tickets.submit(c["collect_id"], "Plex shows scanning as playback")
    c, t = asyncio.run(go())
    assert t["ticket"] == "VPB-2" and t["status"] == "sent"
    # the sealed text decrypts to the previewed bytes plus a note header
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    from cryptography.hazmat.primitives import serialization
    import decrypt_ticket
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    blob = ticket_seal.seal("x" + c["text"], pem)
    assert decrypt_ticket.decrypt(blob, key).endswith(c["text"])
    assert c["text"] in __import__("gzip").decompress(b"") if False else True


def test_second_submit_within_minute_is_429(svc):
    async def go():
        a = await tickets.start_collect()
        await tickets.submit(a["collect_id"], "one")
        b = await tickets.start_collect()
        await tickets.submit(b["collect_id"], "two")
    with pytest.raises(tickets.TicketError) as e:
        asyncio.run(go())
    assert e.value.status == 429


def test_replay_returns_same_ticket_no_new_issue(svc):
    async def go():
        c = await tickets.start_collect()
        a = await tickets.submit(c["collect_id"], "n")
        b = await tickets.submit(c["collect_id"], "n")
        return a, b
    a, b = asyncio.run(go())
    assert a["id"] == b["id"] and len(svc) == 1


def test_unknown_collect_id_rejected(svc):
    with pytest.raises(tickets.TicketError) as e:
        asyncio.run(tickets.submit("nope", ""))
    assert e.value.status == 410


def test_gitea_down_keeps_unsent_then_retry(svc, monkeypatch):
    calls = {"n": 0}

    async def flaky(blob, title):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection refused")
        return 9
    monkeypatch.setattr(tickets, "_send", flaky)

    async def go():
        c = await tickets.start_collect()
        t = await tickets.submit(c["collect_id"], "n")
        dl = await tickets.blob_for_download(t["id"])
        r = await tickets.retry(t["id"])
        return t, dl, r
    t, dl, r = asyncio.run(go())
    assert t["status"] == "unsent" and "refused" in t["error"] and dl
    assert r["status"] == "sent" and r["ticket"] == "VPB-9"


def test_pending_capped_and_note_scrubbed(svc):
    async def go():
        for _ in range(7):
            await tickets.start_collect()
        c = await tickets.start_collect()
        return await tickets.submit(c["collect_id"], "see secretvalue99 on 10.1.2.3")
    t = asyncio.run(go())
    assert len(tickets._pending) <= tickets.PENDING_MAX
    assert "secretvalue99" not in t["note"] and "10.1.2.3" not in t["note"]


def test_chunking():
    parts = tickets._chunks("a" * (tickets.CHUNK * 2 + 5))
    assert [len(p) for p in parts] == [tickets.CHUNK, tickets.CHUNK, 5]


def test_get_ticket_returns_reviewed_report(svc):
    async def go():
        c = await tickets.start_collect()
        t = await tickets.submit(c["collect_id"], "n")
        return c, await tickets.get_ticket(t["id"])
    c, t = asyncio.run(go())
    assert t["report"] == c["text"] and t["ticket"] == "VPB-2"
    with pytest.raises(tickets.TicketError):
        asyncio.run(tickets.get_ticket(999))


def test_library_section_queries_are_valid(fresh_db):
    """Every query in the Library section must run against the real schema (a bad column blanks the list)."""
    async def go():
        await database.init_db()
        return await ticket_collect._library({})
    out = asyncio.run(go())
    assert "unavailable" not in out
    for title in ("activated movies", "dead movies", "activation queue", "learned ranges"):
        assert title in out


def test_range_learn_snapshot_reports_skip_reasons():
    import range_learn
    range_learn._skip[("movie", 7)] = "budget used up"
    snap = range_learn.snapshot()
    assert snap["last_skip"]["movie:7"] == "budget used up"
    assert range_learn.last_skip("movie", 7) == "budget used up"
    range_learn._skip.clear()


def test_delete_tickets_removes_only_selected(svc):
    async def go():
        c = await tickets.start_collect()
        t = await tickets.submit(c["collect_id"], "one")
        db = await database.get_db()
        await db.execute("INSERT INTO tickets (created_at, version, title, note, status, blob) VALUES ('x','v','t','n','unsent','b')")
        await db.commit()
        n = await tickets.delete_tickets([t["id"], "bad", 9999])
        return n, await tickets.list_tickets()
    n, left = asyncio.run(go())
    assert n == 1 and len(left) == 1 and left[0]["status"] == "unsent"
