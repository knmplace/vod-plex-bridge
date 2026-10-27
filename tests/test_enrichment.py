"""Metadata enrichment: provider-info (an upstream call through Dispatcharr) is fetched
once per movie and again only when Dispatcharr's data for it changes; TMDB covers every
movie with an id, not just flagged ones; language lookups don't repeat TMDB 404s; and a
catalog sync ends idle with a summary instead of sitting on "Enriching..."."""
import asyncio
import os
import sys
import tempfile

import pytest

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "vod_bridge.db"))
os.environ.setdefault("SETTINGS_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import api  # noqa: E402
import database  # noqa: E402
import scraper  # noqa: E402
import tmdb_local  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "vod_bridge.db"))
    monkeypatch.setattr(database, "_db", None)
    yield
    if database._db is not None:
        asyncio.run(database._db.close())
        database._db = None


async def _seed(rows):
    await database.init_db()
    db = await database.get_db()
    for r in rows:
        cols = {"uuid": f"u{r['id']}", "name": f"Movie {r['id']}", **r}
        await db.execute(f"INSERT INTO movies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         tuple(cols.values()))
    await db.commit()
    return db


async def _col(db, col, movie_id):
    return (await (await db.execute(f"SELECT {col} FROM movies WHERE id = ?", (movie_id,))).fetchone())[0]


class _Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data


def _fake_dispatcharr(monkeypatch, answers, calls):
    """answers: movie_id -> status code, or an Exception to raise (network failure)."""
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            mid = int(url.rstrip("/").split("/")[-2])
            calls.append(mid)
            answer = answers.get(mid, 200)
            if isinstance(answer, Exception):
                raise answer
            return _Resp(answer, {"genre": "Drama", "duration_secs": "5400", "bitrate": 4000})

    monkeypatch.setattr(api.httpx, "AsyncClient", Client)
    monkeypatch.setattr(api.asyncio, "sleep", _no_sleep)


async def _no_sleep(*a, **k):
    return None


def test_provider_info_fetches_each_movie_once_activated_first(fresh_db, monkeypatch):
    calls = []
    _fake_dispatcharr(monkeypatch, {3: 404}, calls)

    async def run():
        db = await _seed([{"id": 1}, {"id": 2, "activated": 1}, {"id": 3}, {"id": 4, "name": ""}])
        first = await api._enrich_from_provider_info(batch_size=10)
        assert calls[0] == 2 and sorted(calls) == [1, 2, 3]
        assert first == 2
        assert await _col(db, "genre", 1) == "Drama"
        # a 404 is an answer: stamped, not retried every sync
        assert await _col(db, "provider_info_at", 3) is not None
        calls.clear()
        assert await api._enrich_from_provider_info(batch_size=10) == 0
        assert calls == []

    asyncio.run(run())


def test_provider_info_network_failure_is_retried(fresh_db, monkeypatch):
    calls = []
    _fake_dispatcharr(monkeypatch, {1: RuntimeError("down")}, calls)

    async def run():
        db = await _seed([{"id": 1}])
        await api._enrich_from_provider_info(batch_size=10)
        assert await _col(db, "provider_info_at", 1) is None

    asyncio.run(run())


def test_provider_info_batch_progresses_through_backlog(fresh_db, monkeypatch):
    calls = []
    _fake_dispatcharr(monkeypatch, {}, calls)

    async def run():
        await _seed([{"id": i} for i in range(1, 6)])
        await api._enrich_from_provider_info(batch_size=2)
        await api._enrich_from_provider_info(batch_size=2)
        await api._enrich_from_provider_info(batch_size=2)
        assert sorted(calls) == [1, 2, 3, 4, 5]

    asyncio.run(run())


def test_dispatcharr_change_requeues_provider_info(fresh_db):
    async def run():
        db = await _seed([])
        movie = {"id": 7, "uuid": "u7", "name": "A", "genre": "", "description": "x"}
        await scraper.save_dispatcharr_movie(db, movie, "", "", {})
        await db.execute("UPDATE movies SET provider_info_at = 'then' WHERE id = 7")
        await scraper.save_dispatcharr_movie(db, movie, "", "", {})
        assert await _col(db, "provider_info_at", 7) == "then"
        await scraper.save_dispatcharr_movie(db, {**movie, "description": "changed"}, "", "", {})
        assert await _col(db, "provider_info_at", 7) is None

    asyncio.run(run())


@pytest.fixture()
def fake_tmdb(monkeypatch):
    calls = []

    async def fetch_title(media_type, tmdb_id, **kwargs):
        calls.append(tmdb_id)
        if tmdb_id == "404":
            raise tmdb_local.TmdbNotFoundError(tmdb_id)
        if tmdb_id == "down":
            return None, False
        return {"genres": [{"name": "Drama"}], "overview": "Plot", "original_language": "fr",
                "release_date": "2001-01-01", "runtime": 90}, True

    monkeypatch.setattr(tmdb_local, "has_credentials", lambda: True)
    monkeypatch.setattr(tmdb_local, "fetch_title", fetch_title)
    monkeypatch.setattr(scraper.asyncio, "sleep", _no_sleep)
    import tmdb_store
    monkeypatch.setattr(tmdb_store, "get_meta", lambda *a: "0")
    monkeypatch.setattr(tmdb_store, "set_meta", lambda *a: None)
    return calls


def test_tmdb_enriches_never_enriched_movies_and_sets_language(fresh_db, fake_tmdb):
    async def run():
        db = await _seed([{"id": 1, "tmdb_id": "11"}, {"id": 2, "tmdb_id": "404"}, {"id": 3, "tmdb_id": "down"}])
        res = await scraper.enrich_from_tmdb()
        assert res["enriched"] == 1 and res["not_found"] == 1
        assert await _col(db, "genre", 1) == "Drama"
        assert await _col(db, "language", 1) == "fr"
        # transient failure stays queued; the 404 is checked off
        assert res["remaining"] == 1
        fake_tmdb.clear()
        await scraper.enrich_from_tmdb()
        assert fake_tmdb == ["down"]

    asyncio.run(run())


def test_tmdb_keeps_existing_language(fresh_db, fake_tmdb):
    async def run():
        db = await _seed([{"id": 1, "tmdb_id": "11", "language": "en"}])
        await scraper.enrich_from_tmdb()
        assert await _col(db, "language", 1) == "en"

    asyncio.run(run())


def test_language_detection_does_not_repeat_not_found(fresh_db, fake_tmdb, monkeypatch):
    monkeypatch.setattr(api.asyncio, "sleep", _no_sleep)

    async def run():
        db = await _seed([{"id": 1, "tmdb_id": "404"}, {"id": 2, "tmdb_id": "11"}])
        await db.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")
        await db.commit()
        await api._bulk_detect_languages()
        assert await _col(db, "language", 2) == "fr"
        fake_tmdb.clear()
        await api._bulk_detect_languages()
        assert fake_tmdb == []

    asyncio.run(run())


def test_catalog_sync_finishes_idle_with_summary(fresh_db, monkeypatch):
    async def scrape(**kwargs):
        db = await database.get_db()
        await db.execute("UPDATE sync_state SET status = 'idle', message = 'Synced 5 movies' WHERE id = 1")
        await db.commit()

    async def zero(*a, **k):
        return 0

    async def search(*a, **k):
        return {"found": 2, "not_found": 1, "skipped": 0}

    async def tmdb(*a, **k):
        return {"enriched": 4, "changed": 3, "remaining": 10}

    async def langs():
        return None

    monkeypatch.setattr(api, "scrape_catalog", scrape)
    monkeypatch.setattr(api, "_prune_unselected_movies", zero)
    monkeypatch.setattr(api, "apply_stream_mapping_to_db", zero)
    monkeypatch.setattr(api, "_enrich_from_provider_info", zero)
    monkeypatch.setattr(api, "search_tmdb_for_missing", search)
    monkeypatch.setattr(api, "enrich_from_tmdb", tmdb)
    monkeypatch.setattr(api, "_bulk_detect_languages", langs)
    monkeypatch.setattr(tmdb_local, "has_credentials", lambda: True)

    async def run():
        db = await _seed([])
        await db.execute("INSERT OR IGNORE INTO sync_state (id) VALUES (1)")
        await db.commit()
        await api._run_catalog_sync()
        row = await (await db.execute("SELECT status, message FROM sync_state WHERE id = 1")).fetchone()
        assert row["status"] == "idle"
        assert row["message"].startswith("Synced 5 movies")
        assert "3 updated from TMDB" in row["message"] and "10 queued" in row["message"]

    asyncio.run(run())
