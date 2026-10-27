"""Movie and series catalog syncs run on their own user-set intervals, skipping while a sync is busy."""
import asyncio
from datetime import datetime, timedelta

import pytest

from test_enrichment import fresh_db  # noqa: F401
import api  # noqa: E402
import database  # noqa: E402

T0 = datetime(2026, 10, 27, 12, 0, 0)


@pytest.fixture()
def runs(monkeypatch):
    calls = []

    async def movie_sync(max_movies=0, category_ids=None, account_ids=None):
        calls.append(("movies", category_ids, account_ids))

    async def series_sync(max_series=0):
        calls.append(("series",))

    monkeypatch.setattr(api, "_run_catalog_sync", movie_sync)
    monkeypatch.setattr(api, "scrape_series_catalog", series_sync)
    monkeypatch.setattr(api, "_refresh_running", False)
    return calls


async def _setup(movies_hours, series_hours, at=T0):
    await database.init_db()
    db = await database.get_db()
    await db.execute("INSERT INTO selected_categories (category_id, enabled) VALUES (7, 1)")
    await db.commit()
    await api.save_catalog_schedule(movies_hours, series_hours, now=at)
    return db


def test_due_check():
    assert not api._catalog_sync_due(0, T0.isoformat(), T0 + timedelta(days=9))
    assert not api._catalog_sync_due(4, (T0 - timedelta(hours=3)).isoformat(), T0)
    assert api._catalog_sync_due(4, (T0 - timedelta(hours=4)).isoformat(), T0)
    assert not api._catalog_sync_due(4, None, T0)


def test_runs_each_sync_on_its_own_interval(fresh_db, runs):
    async def run():
        await _setup(2, 6)
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=1)) == []
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=2)) == ["movies"]
        assert runs[0] == ("movies", [7], [])
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=3)) == []
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=6)) == ["movies", "series"]

    asyncio.run(run())


def test_off_and_busy_skip(fresh_db, runs, monkeypatch):
    async def run():
        db = await _setup(1, 0)
        await db.execute("UPDATE sync_state SET status = 'scraping' WHERE id = 1")
        await db.commit()
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=5)) == []
        await db.execute("UPDATE sync_state SET status = 'idle' WHERE id = 1")
        await db.commit()
        monkeypatch.setattr(api, "_refresh_running", True)
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=5)) == []
        monkeypatch.setattr(api, "_refresh_running", False)
        assert await api.run_due_catalog_syncs(T0 + timedelta(hours=5)) == ["movies"]
        assert runs == [("movies", [7], [])]

    asyncio.run(run())


def test_rejects_unknown_interval(fresh_db):
    async def run():
        await database.init_db()
        with pytest.raises(ValueError):
            await api.save_catalog_schedule(5, 0)
        assert (await api.get_catalog_schedule())["movies_hours"] == 0

    asyncio.run(run())
