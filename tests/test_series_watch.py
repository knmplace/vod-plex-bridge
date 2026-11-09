"""Series Watch List: completeness by aired episodes, once-only auto-add, notify fallbacks."""
import asyncio
import json

import pytest

from test_enrichment import fresh_db  # noqa: F401
import database  # noqa: E402
import series_watch  # noqa: E402
import tmdb_local  # noqa: E402

TODAY = "2026-11-08"
OLD, BASE, NEW = "2026-01-01T00:00:00", "2026-02-01T00:00:00", "2026-03-01T00:00:00"


@pytest.fixture()
def tmdb(monkeypatch):
    """{(tmdb_id, season): [(episode_number, air_date)]} served instead of TMDB."""
    data = {}

    async def payload(media_type, tmdb_id, **kw):
        return {"next_episode_to_air": data.get(("next", tmdb_id))}

    async def season(tmdb_id, season_number, max_age=0):
        eps = data.get((tmdb_id, season_number))
        return None if eps is None else [{"episode_number": n, "air_date": d, "name": ""} for n, d in eps]

    monkeypatch.setattr(tmdb_local, "fetch_title_payload", payload)
    monkeypatch.setattr(tmdb_local, "fetch_season", season)
    monkeypatch.setattr(series_watch, "_today", lambda: TODAY)
    return data


async def _show(db, sid, status, category_id=1, tmdb_id="100"):
    await db.execute("INSERT INTO series (id, uuid, name, tmdb_id, tmdb_status, category_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (sid, f"s{sid}", f"Show {sid}", tmdb_id, status, category_id))


async def _ep(db, eid, sid, season, num, activated=0, discovered=None, air=None):
    await db.execute(
        "INSERT INTO episodes (id, uuid, series_id, season_number, episode_number, activated, activated_at, "
        "discovered_at, air_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, f"u{eid}", sid, season, num, activated, BASE if activated else None, discovered, air))


async def _state(db, sid):
    s = (await series_watch._active_shows(db, sid))[0]
    return await series_watch.compute_show(db, s, TODAY)


def test_ended_show_with_all_aired_activated_is_not_listed(fresh_db, tmdb):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "ended")
        for n in (1, 2, 3):
            await _ep(db, n, 1, 1, n, activated=1)
        tmdb[("100", 1)] = [(1, "2025-01-01"), (2, "2025-01-08"), (3, "2025-01-15")]
        assert not (await _state(db, 1))["listed"]

    asyncio.run(run())


def test_ended_show_stays_listed_when_aired_episodes_missing_or_unactivated(fresh_db, tmdb):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "ended")
        await _ep(db, 1, 1, 1, 1, activated=1)
        await _ep(db, 2, 1, 1, 2, activated=0, discovered=OLD)   # at the provider, never activated
        tmdb[("100", 1)] = [(n, "2025-01-0%d" % n) for n in (1, 2, 3, 4)]  # E3, E4 aired, provider lacks them
        d = await _state(db, 1)
        row = d["rows"][0]
        assert d["listed"] and row["state"] == "gap"
        assert row["missing"] == [3, 4] and row["available"] == [2] and row["have"] == 1 and row["aired"] == 4
        assert d["candidates"] == []     # old (pre-baseline) episodes are listed, never auto-added

    asyncio.run(run())


def test_continuing_show_lists_next_air_and_future_episodes_are_not_owed(fresh_db, tmdb):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "continuing")
        for n in (1, 2):
            await _ep(db, n, 1, 1, n, activated=1)
        tmdb[("100", 1)] = [(1, "2026-10-01"), (2, "2026-10-08"), (3, "2026-11-20")]
        tmdb[("next", "100")] = {"air_date": "2026-11-20", "season_number": 1, "episode_number": 3}
        d = await _state(db, 1)
        row = d["rows"][0]
        assert d["listed"] and row["state"] == "waiting" and row["missing"] == []
        assert row["next_air"] == "2026-11-20" and row["upcoming"] == [{"episode": 3, "date": "2026-11-20"}]

    asyncio.run(run())


def test_run_watch_queues_new_episode_once(fresh_db, tmdb, monkeypatch):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await db.execute("INSERT INTO series_categories (id, name, strm_folder) VALUES (1, 'TV', 'TV')")
        await _show(db, 1, "continuing")
        await _ep(db, 1, 1, 1, 1, activated=1)
        await _ep(db, 2, 1, 1, 2, activated=0, discovered=NEW, air="2026-10-01")
        await db.commit()
        tmdb[("100", 1)] = [(1, "2026-09-01"), (2, "2026-10-01")]

        async def no_fetch(series_id, **kw):
            return {"status": "ok"}
        import series_scraper
        monkeypatch.setattr(series_scraper, "fetch_series_episodes", no_fetch)

        r = await series_watch.run_watch()
        assert r["auto_added"] == 1
        q = await (await db.execute("SELECT category_id, episode_ids FROM series_activation_queue")).fetchone()
        assert q["category_id"] == 1 and json.loads(q["episode_ids"]) == [2]

        await db.execute("DELETE FROM series_activation_queue")   # worker finished (or the user removed it)
        await db.commit()
        r = await series_watch.run_watch()
        assert r["auto_added"] == 0                                # handled once, never re-added
        assert (await (await db.execute("SELECT COUNT(*) AS n FROM series_activation_queue")).fetchone())["n"] == 0

    asyncio.run(run())


def test_notify_mode_and_no_library_only_report(fresh_db, tmdb, monkeypatch):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "continuing", category_id=1)
        await _show(db, 2, "continuing", category_id=None, tmdb_id="200")
        for sid, base in ((1, 10), (2, 20)):
            await _ep(db, base, sid, 1, 1, activated=1)
            await _ep(db, base + 1, sid, 1, 2, activated=0, discovered=NEW, air="2026-10-01")
        await db.execute("INSERT INTO series_watch_prefs (series_id, mode) VALUES (1, 'notify')")
        await db.commit()
        tmdb[("100", 1)] = [(1, "2026-09-01"), (2, "2026-10-01")]
        tmdb[("200", 1)] = [(1, "2026-09-01"), (2, "2026-10-01")]

        async def no_fetch(series_id, **kw):
            return {"status": "ok"}
        import series_scraper
        monkeypatch.setattr(series_scraper, "fetch_series_episodes", no_fetch)

        r = await series_watch.run_watch()
        assert r["auto_added"] == 0 and r["notified"] == 2
        kinds = {x["kind"] for x in await (await db.execute("SELECT kind FROM series_watch_events")).fetchall()}
        assert kinds == {"new_available", "no_category"}
        assert (await (await db.execute("SELECT COUNT(*) AS n FROM series_activation_queue")).fetchone())["n"] == 0

    asyncio.run(run())


def test_new_season_is_reported_not_queued(fresh_db, tmdb, monkeypatch):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "continuing")
        await _ep(db, 1, 1, 1, 1, activated=1)
        await _ep(db, 2, 1, 2, 1, activated=0, discovered=NEW, air="2026-10-01")
        await db.commit()
        tmdb[("100", 1)] = [(1, "2026-01-01")]
        tmdb[("100", 2)] = [(1, "2026-10-01")]

        async def no_fetch(series_id, **kw):
            return {"status": "ok"}
        import series_scraper
        monkeypatch.setattr(series_scraper, "fetch_series_episodes", no_fetch)

        r = await series_watch.run_watch()
        assert r["new_seasons"] == 1 and r["auto_added"] == 0
        d = await _state(db, 1)
        assert [x["state"] for x in d["rows"]] == ["complete", "new_season"]

    asyncio.run(run())


def test_notifications_unread_until_marked_seen(fresh_db, tmdb):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await _show(db, 1, "continuing")
        await _ep(db, 1, 1, 1, 1, activated=1)
        await series_watch._event(db, 1, 1, 1, 1, "auto_added", "queued")
        await db.commit()
        r = await series_watch.get_notifications()
        assert r["unread"] == 1 and r["items"][0]["name"] == "Show 1"
        await series_watch.mark_notifications_seen()
        assert (await series_watch.get_notifications())["unread"] == 0

    asyncio.run(run())
