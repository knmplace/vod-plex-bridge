"""Ticket report collector: sections, graceful failure, secrets, size cap."""
import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_enrichment import fresh_db  # noqa: E402,F401
import config  # noqa: E402
import database  # noqa: E402
import ticket_collect  # noqa: E402


@pytest.fixture
def quiet(monkeypatch, fresh_db):
    async def boom(self, *a, **k):
        raise httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    monkeypatch.setattr(config, "PLEX_URL", "http://plex.example.test:32400")
    monkeypatch.setattr(config, "PLEX_TOKEN", "PLEXTOKEN123456")
    monkeypatch.setattr(config, "TMDB_API_KEY", "tmdbkey98765")
    asyncio.run(database.init_db())


def run():
    return asyncio.run(ticket_collect.collect_report())


def test_report_has_all_sections(quiet):
    text, _ = run()
    for h in ("App", "Database counts", "Streaming", "Mounts", "Plex", "Dispatcharr", "Proxy log", "Container log tail"):
        assert f"=== {h}" in text


def test_plex_unreachable_is_unavailable_not_error(quiet):
    text, _ = run()
    assert "unavailable" in text.split("=== Plex ===")[1].split("===")[0]


def test_secret_settings_shown_as_set_unset_only(quiet):
    text, _ = run()
    assert "PLEX_TOKEN: set" in text and "PLEXTOKEN123456" not in text and "tmdbkey98765" not in text


def test_returned_secrets_include_plex_token_and_hosts(quiet):
    _, secrets = run()
    assert "PLEXTOKEN123456" in secrets["token"]
    assert "plex.example.test" in secrets["host"]


def test_cap_truncates_oldest_first():
    lines = [f"line{i:07d}" + "x" * 100 for i in range(50000)]
    kept = ticket_collect._cap(lines, 100_000)
    assert sum(len(l) + 1 for l in kept) <= 100_100
    assert kept[-1] == lines[-1] and kept[0].startswith("[truncated")
