import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from config import DISPATCHARR_URL, DISPATCHARR_API_KEY
from database import get_db

logger = logging.getLogger(__name__)

PAGE_SIZE = 100

_cancel = False


def request_cancel():
    global _cancel
    _cancel = True


def is_cancelled():
    return _cancel


def _auth_headers() -> dict:
    return {"X-API-Key": DISPATCHARR_API_KEY} if DISPATCHARR_API_KEY else {}


async def _upsert_series(db, series):
    poster_url = None
    logo = series.get("logo")
    if logo and logo.get("url"):
        poster_url = logo["url"]

    custom = series.get("custom_properties") or {}
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except (json.JSONDecodeError, TypeError):
            custom = {}
    trailer_key = custom.get("youtube_trailer") or custom.get("trailer") or None

    await db.execute("""
        INSERT INTO series (id, uuid, name, year, rating, genre, description,
                           tmdb_id, poster_url, episode_count, synced_at, trailer_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid=excluded.uuid, name=excluded.name, year=excluded.year,
            rating=excluded.rating, genre=excluded.genre,
            description = CASE WHEN series.description IS NULL OR series.description = '' THEN excluded.description ELSE series.description END,
            tmdb_id=excluded.tmdb_id,
            poster_url = CASE WHEN series.poster_url IS NULL OR series.poster_url = '' THEN excluded.poster_url ELSE series.poster_url END,
            synced_at=excluded.synced_at,
            trailer_key = COALESCE(excluded.trailer_key, series.trailer_key)
    """, (
        series["id"],
        series["uuid"],
        series.get("name", ""),
        series.get("year"),
        float(series.get("rating") or 0),
        series.get("genre", ""),
        series.get("description", ""),
        series.get("tmdb_id"),
        poster_url,
        series.get("episode_count", 0),
        datetime.now(timezone.utc).isoformat(),
        trailer_key,
    ))


async def _prune_removed_series(db, seen_ids: set) -> int:
    """Delete series no longer in Dispatcharr's catalog. Only called after a full,
    uninterrupted scrape; series with any activated episode are kept."""
    rows = await db.execute(
        "SELECT id FROM series WHERE id NOT IN (SELECT series_id FROM episodes WHERE activated = 1)"
    )
    stale = [r["id"] for r in await rows.fetchall() if r["id"] not in seen_ids]
    for i in range(0, len(stale), 500):
        chunk = stale[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        await db.execute(f"DELETE FROM episodes WHERE series_id IN ({ph})", chunk)
        await db.execute(f"DELETE FROM series WHERE id IN ({ph})", chunk)
    await db.commit()
    if stale:
        logger.info("Series scrape: removed %d series no longer in Dispatcharr", len(stale))
    return len(stale)


async def scrape_series_catalog(max_series: int = 0):
    """Page through Dispatcharr's series catalog and upsert into the local series table."""
    global _cancel
    _cancel = False

    db = await get_db()
    await db.execute(
        "UPDATE series_sync_state SET status = 'scraping', message = 'Starting series catalog scrape...' WHERE id = 1"
    )
    await db.commit()

    total_synced = 0
    req_headers = _auth_headers()
    error = None
    completed = False
    seen_ids = set()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            page = 1
            while True:
                if _cancel:
                    break
                if max_series > 0 and total_synced >= max_series:
                    break

                url = f"{DISPATCHARR_URL}/api/vod/series/?page={page}&page_size={PAGE_SIZE}"
                resp = await client.get(url, headers=req_headers)
                if resp.status_code != 200:
                    logger.error(f"Series API error on page {page}: {resp.status_code}")
                    break

                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break

                for s in results:
                    if _cancel:
                        break
                    if max_series > 0 and total_synced >= max_series:
                        break
                    if not s.get("name", "").strip():
                        continue
                    await _upsert_series(db, s)
                    seen_ids.add(s["id"])
                    total_synced += 1

                await db.commit()
                await db.execute(
                    "UPDATE series_sync_state SET message = ? WHERE id = 1",
                    (f"Scraped {total_synced} series (page {page})...",),
                )
                await db.commit()

                if not data.get("next"):
                    completed = not _cancel and max_series == 0
                    break
                page += 1
                await asyncio.sleep(0.1)
    except Exception as e:
        # Without this, a timeout mid-scrape left status stuck at 'scraping' forever.
        error = e
        logger.error(f"Series catalog scrape failed after {total_synced} series: {e}")

    pruned = 0
    if completed and seen_ids:
        pruned = await _prune_removed_series(db, seen_ids)

    count_row = await db.execute("SELECT COUNT(*) as cnt FROM series")
    count = (await count_row.fetchone())["cnt"]

    status_msg = f"failed ({error})" if error else ("cancelled" if _cancel else "complete")
    await db.execute(
        "UPDATE series_sync_state SET last_catalog_sync = ?, total_series = ?, status = 'idle', message = ? WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(), count, f"Series catalog sync {status_msg}: {total_synced} series"
         + (f", {pruned} removed" if pruned else "")),
    )
    await db.commit()
    logger.info(f"Series catalog scrape {status_msg}: {total_synced} series synced, {count} total in DB")
    _cancel = False
    return total_synced


async def _upsert_episode(db, ep):
    providers = ep.get("providers") or []
    usable = [p for p in providers if p.get("m3u_account") and p.get("stream_id")]

    stream_id = None
    account_id = None
    account_name = ""
    container_extension = ""
    alt_streams = []

    if usable:
        primary = usable[0]
        stream_id = primary.get("stream_id")
        container_extension = primary.get("container_extension", "") or ""
        m3u = primary.get("m3u_account") or {}
        account_id = m3u.get("id")
        account_name = m3u.get("name", "") or ""

        for p in usable[1:]:
            m3u_alt = p.get("m3u_account") or {}
            alt_streams.append({
                "stream_id": p.get("stream_id"),
                "account_id": m3u_alt.get("id"),
                "account_name": m3u_alt.get("name", "") or "",
                "ext": p.get("container_extension", "") or "",
            })

    await db.execute("""
        INSERT INTO episodes (id, uuid, series_id, name, description, air_date, rating,
                             duration_secs, season_number, episode_number, tmdb_id,
                             stream_id, account_id, account_name, container_extension,
                             alt_streams, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid=excluded.uuid, series_id=excluded.series_id, name=excluded.name,
            description=excluded.description, air_date=excluded.air_date,
            rating=excluded.rating, duration_secs=excluded.duration_secs,
            season_number=excluded.season_number, episode_number=excluded.episode_number,
            tmdb_id=excluded.tmdb_id,
            stream_id = CASE WHEN excluded.stream_id IS NOT NULL THEN excluded.stream_id ELSE episodes.stream_id END,
            account_id = CASE WHEN excluded.stream_id IS NOT NULL THEN excluded.account_id ELSE episodes.account_id END,
            account_name = CASE WHEN excluded.stream_id IS NOT NULL THEN excluded.account_name ELSE episodes.account_name END,
            container_extension = CASE WHEN excluded.stream_id IS NOT NULL THEN excluded.container_extension ELSE episodes.container_extension END,
            alt_streams = CASE WHEN excluded.stream_id IS NOT NULL THEN excluded.alt_streams ELSE episodes.alt_streams END,
            synced_at=excluded.synced_at
    """, (
        ep["id"],
        ep["uuid"],
        ep["series_id"] if "series_id" in ep else ep.get("series"),
        ep.get("name", ""),
        ep.get("description", "") or "",
        ep.get("air_date"),
        float(ep.get("rating") or 0),
        ep.get("duration_secs"),
        ep.get("season_number"),
        ep.get("episode_number"),
        ep.get("tmdb_id"),
        stream_id,
        account_id,
        account_name,
        container_extension,
        json.dumps(alt_streams),
        datetime.now(timezone.utc).isoformat(),
    ))


async def fetch_series_episodes(series_id: int, force_refresh: bool = False, relation_id: int | None = None) -> dict:
    """Fetch episodes for a series from Dispatcharr's provider-info endpoint and
    upsert them into the local episodes table. Returns {status, episodes_synced}."""
    db = await get_db()

    url = f"{DISPATCHARR_URL}/api/vod/series/{series_id}/provider-info/?include_episodes=true"
    if force_refresh:
        url += "&force_refresh=true"
    if relation_id:
        url += f"&relation_id={relation_id}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, headers=_auth_headers())
            if resp.status_code != 200:
                logger.error("fetch_series_episodes: HTTP %d for series %d", resp.status_code, series_id)
                return {"status": "error", "message": f"HTTP {resp.status_code}", "episodes_synced": 0}
            data = resp.json()
    except Exception as e:
        logger.warning("fetch_series_episodes failed for series %d: %s", series_id, e)
        return {"status": "error", "message": str(e), "episodes_synced": 0}

    # Response shape: episodes grouped by season, e.g. {"episodes": {"1": [...], "2": [...]}}
    # or a flat "episodes": [...] list — handle both defensively.
    episodes_raw = data.get("episodes")
    flat_episodes = []
    if isinstance(episodes_raw, dict):
        for season_key, ep_list in episodes_raw.items():
            if isinstance(ep_list, list):
                flat_episodes.extend(ep_list)
    elif isinstance(episodes_raw, list):
        flat_episodes = episodes_raw

    synced = 0
    for ep in flat_episodes:
        if not ep.get("id") or not ep.get("uuid"):
            continue
        ep = dict(ep)
        ep.setdefault("series_id", series_id)
        try:
            await _upsert_episode(db, ep)
            synced += 1
        except Exception as e:
            logger.warning("Failed to upsert episode %s for series %d: %s", ep.get("id"), series_id, e)

    await db.commit()

    count_row = await db.execute("SELECT COUNT(*) as cnt FROM episodes WHERE series_id = ?", (series_id,))
    total = (await count_row.fetchone())["cnt"]
    await db.execute("UPDATE series SET episode_count = ? WHERE id = ?", (total, series_id))
    await db.commit()

    logger.info("fetch_series_episodes: series %d synced %d episodes (total now %d)", series_id, synced, total)
    return {"status": "ok", "episodes_synced": synced, "total_episodes": total}
