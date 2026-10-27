"""TMDB is the title/poster source of truth for anything with a TMDB id: Dispatcharr never
updates a title's name after first import, so the bridge applies TMDB's name (local store
first, API on a miss) to movies and series, skipping locked titles."""
import asyncio
import os

import pytest

from test_enrichment import _col, _no_sleep, _seed, fresh_db  # noqa: F401
import database  # noqa: E402
import scraper  # noqa: E402
import series_generator  # noqa: E402
import series_scraper  # noqa: E402
import tmdb_local  # noqa: E402

POSTER = "https://image.tmdb.org/t/p/w600_and_h900_bestv2/p.jpg"


@pytest.fixture()
def fake_titles(monkeypatch):
    calls = []

    async def fetch_title(media_type, tmdb_id, **kwargs):
        calls.append((media_type, tmdb_id))
        if tmdb_id == "404":
            raise tmdb_local.TmdbNotFoundError(tmdb_id)
        if media_type == "movie":
            return {"title": "10 Items or Less", "poster_path": "/p.jpg", "overview": "Plot"}, True
        return {"name": "30 Coins", "poster_path": "/p.jpg", "overview": "Plot",
                "genres": [{"name": "Drama"}], "first_air_date": "2020-11-29"}, True

    monkeypatch.setattr(tmdb_local, "has_credentials", lambda: True)
    monkeypatch.setattr(tmdb_local, "fetch_title", fetch_title)
    monkeypatch.setattr(scraper.asyncio, "sleep", _no_sleep)
    import tmdb_store
    monkeypatch.setattr(tmdb_store, "get_meta", lambda *a: "0")
    monkeypatch.setattr(tmdb_store, "set_meta", lambda *a: None)
    return calls


def _dispatcharr_series(**kw):
    return {"id": 1, "uuid": "u1", "name": "30 Monedas (es)", "tmdb_id": "280614",
            "logo": {"url": "http://provider/images/dead.jpg"}, **kw}


async def _series_col(db, col, sid=1):
    return (await (await db.execute(f"SELECT {col} FROM series WHERE id = ?", (sid,))).fetchone())[0]


def test_movie_enrichment_sets_tmdb_title(fresh_db, fake_titles):
    async def run():
        db = await _seed([{"id": 1, "tmdb_id": "11", "name": "10 Items or Less (es)"}])
        await scraper.enrich_from_tmdb()
        assert await _col(db, "name", 1) == "10 Items or Less"

    asyncio.run(run())


def test_movie_title_search_replaces_provider_poster(fresh_db, monkeypatch):
    async def search_movie(query, year=None):
        return [{"id": 9, "title": "Movie 1", "poster_path": "/p.jpg"}], True

    monkeypatch.setattr(tmdb_local, "has_credentials", lambda: True)
    monkeypatch.setattr(tmdb_local, "search_movie", search_movie)

    async def run():
        db = await _seed([{"id": 1, "tmdb_id": None, "poster_url": "http://provider/images/dead.jpg"}])
        await scraper.search_tmdb_for_missing()
        assert await _col(db, "poster_url", 1) == POSTER

    asyncio.run(run())


def test_series_enrichment_sets_title_poster_and_skips_locked(fresh_db, fake_titles):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await series_scraper._upsert_series(db, _dispatcharr_series())
        await series_scraper._upsert_series(db, _dispatcharr_series(id=2, uuid="u2", tmdb_id="77"))
        await series_scraper._upsert_series(db, _dispatcharr_series(id=3, uuid="u3", tmdb_id="404"))
        await db.execute("UPDATE series SET tmdb_locked = 1 WHERE id = 2")
        await db.commit()
        res = await series_scraper.enrich_series_from_tmdb()
        assert ("tv", "280614") in fake_titles and ("tv", "77") not in fake_titles
        assert await _series_col(db, "name") == "30 Coins"
        assert await _series_col(db, "poster_url") == POSTER
        assert await _series_col(db, "year") == 2020
        assert await _series_col(db, "name", 2) == "30 Monedas (es)"
        assert res["not_found"] == 1 and res["remaining"] == 0
        fake_titles.clear()
        await series_scraper.enrich_series_from_tmdb()
        assert fake_titles == []

    asyncio.run(run())


def test_series_resync_keeps_tmdb_title_and_requeues_on_change(fresh_db, fake_titles):
    async def run():
        await database.init_db()
        db = await database.get_db()
        await series_scraper._upsert_series(db, _dispatcharr_series())
        await series_scraper.enrich_series_from_tmdb()
        await series_scraper._upsert_series(db, _dispatcharr_series())
        assert await _series_col(db, "name") == "30 Coins"
        assert await _series_col(db, "tmdb_check") == 0
        await series_scraper._upsert_series(db, _dispatcharr_series(logo={"url": "http://provider/images/new.jpg"}))
        assert await _series_col(db, "name") == "30 Coins"
        assert await _series_col(db, "poster_url") == POSTER
        assert await _series_col(db, "tmdb_check") == 1

    asyncio.run(run())


def test_series_enrichment_rewrites_tvshow_nfo(fresh_db, fake_titles, tmp_path, monkeypatch):
    monkeypatch.setattr(series_generator, "STRM_SERIES_OUTPUT_DIR", str(tmp_path))

    async def run():
        await database.init_db()
        db = await database.get_db()
        await db.execute("INSERT INTO series_categories (id, name, strm_folder) VALUES (5, 'TV', 'tv')")
        await series_scraper._upsert_series(db, _dispatcharr_series())
        await db.execute("UPDATE series SET strm_folder = '30 Monedas', category_id = 5 WHERE id = 1")
        await db.commit()
        nfo = tmp_path / "tv" / "30 Monedas" / "tvshow.nfo"
        nfo.parent.mkdir(parents=True)
        nfo.write_text("<tvshow><title>30 Monedas (es)</title></tvshow>", encoding="utf-8")
        res = await series_scraper.enrich_series_from_tmdb()
        text = nfo.read_text(encoding="utf-8")
        assert "<title>30 Coins</title>" in text and POSTER in text
        assert res["nfo_rewritten"] == 1
        assert os.listdir(tmp_path / "tv") == ["30 Monedas"]

    asyncio.run(run())
