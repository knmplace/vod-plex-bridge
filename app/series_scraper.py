import asyncio
import hashlib
import json
import logging
import os
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

    meta = (series.get("name", ""), series.get("year"), series.get("genre", ""),
            series.get("description", ""), series.get("tmdb_id"), poster_url)
    fingerprint = hashlib.sha1(json.dumps([str(v or "") for v in meta]).encode()).hexdigest()

    # Once TMDB has filled a series, its title/poster/plot/genre/year survive resyncs while
    # the TMDB id is unchanged; a Dispatcharr data change only re-queues the TMDB check.
    keep = ("series.tmdb_enriched_at IS NOT NULL "
            "AND COALESCE(series.tmdb_id, '') = COALESCE(excluded.tmdb_id, '')")
    await db.execute(f"""
        INSERT INTO series (id, uuid, name, year, rating, genre, description,
                           tmdb_id, poster_url, episode_count, synced_at, trailer_key, dispatcharr_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid=excluded.uuid,
            name = CASE WHEN {keep} THEN series.name ELSE excluded.name END,
            year = CASE WHEN {keep} THEN series.year ELSE excluded.year END,
            rating=excluded.rating,
            genre = CASE WHEN {keep} THEN series.genre ELSE excluded.genre END,
            description = CASE WHEN series.description IS NULL OR series.description = '' THEN excluded.description ELSE series.description END,
            tmdb_id=excluded.tmdb_id,
            poster_url = CASE WHEN series.poster_url IS NULL OR series.poster_url = '' THEN excluded.poster_url ELSE series.poster_url END,
            synced_at=excluded.synced_at,
            trailer_key = COALESCE(excluded.trailer_key, series.trailer_key),
            tmdb_check = CASE WHEN COALESCE(series.tmdb_id, '') != COALESCE(excluded.tmdb_id, '')
                              OR series.dispatcharr_hash != excluded.dispatcharr_hash THEN 1
                              ELSE series.tmdb_check END,
            dispatcharr_hash=excluded.dispatcharr_hash
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
        fingerprint,
    ))


async def enrich_series_from_tmdb(limit: int = 5000) -> dict:
    """TMDB (local store first, API on a miss) for series flagged by a Dispatcharr change
    or never checked: TMDB wins for title, poster, plot, genre and year. Locked series and
    series without a TMDB id keep Dispatcharr's data; folder names never change."""
    import tmdb_local
    result = {"enriched": 0, "changed": 0, "nfo_rewritten": 0, "not_found": 0, "failed": 0,
              "remaining": 0, "local": 0, "api": 0, "fields": {}}
    if not tmdb_local.has_credentials():
        return result

    db = await get_db()
    pending = ("(tmdb_check = 1 OR tmdb_enriched_at IS NULL) AND tmdb_id IS NOT NULL AND tmdb_id != '' "
               "AND COALESCE(tmdb_locked, 0) = 0")
    rows = await (await db.execute(
        f"SELECT id, tmdb_id, name, poster_url, description, genre, year FROM series "
        f"WHERE {pending} ORDER BY tmdb_check DESC, id DESC LIMIT ?", (limit,))).fetchall()

    rewrite = []
    for s in rows:
        if _cancel:
            break
        now = datetime.now(timezone.utc).isoformat()
        try:
            try:
                data, local = await tmdb_local.fetch_title("tv", s["tmdb_id"])
            except tmdb_local.TmdbNotFoundError:
                result["not_found"] += 1
                await db.execute("UPDATE series SET tmdb_check = 0, tmdb_enriched_at = ? WHERE id = ?", (now, s["id"]))
                continue
            if data is None:
                result["failed"] += 1  # transient: stays queued
                continue
            poster_path = data.get("poster_path")
            new = {
                "name": data.get("name") or "",
                "poster_url": f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{poster_path}" if poster_path else "",
                "description": data.get("overview") or "",
                "genre": ", ".join(g["name"] for g in data.get("genres", [])),
            }
            changes = {k: v for k, v in new.items() if v and v != (s[k] or "")}
            year = (data.get("first_air_date") or "")[:4]
            if year.isdigit() and str(s["year"] or "") != year:
                changes["year"] = int(year)
            sets = "".join(f"{k} = ?, " for k in changes)
            await db.execute(f"UPDATE series SET {sets}tmdb_check = 0, tmdb_enriched_at = ? WHERE id = ?",
                             (*changes.values(), now, s["id"]))
            result["enriched"] += 1
            result["local" if local else "api"] += 1
            if changes:
                result["changed"] += 1
                for k in changes:
                    result["fields"][k] = result["fields"].get(k, 0) + 1
                rewrite.append(s["id"])
            if not local:
                await asyncio.sleep(0.25)
        except Exception as e:
            logger.error(f"TMDB enrichment failed for series {s['id']}: {e}")
    await db.commit()

    from series_generator import rewrite_tvshow_nfo
    for series_id in rewrite:
        try:
            if await rewrite_tvshow_nfo(series_id):
                result["nfo_rewritten"] += 1
        except Exception as e:
            logger.error(f"tvshow.nfo rewrite failed for series {series_id}: {e}")

    result["remaining"] = (await (await db.execute(f"SELECT COUNT(*) FROM series WHERE {pending}")).fetchone())[0]
    logger.info("Series TMDB enrichment: %s", result)
    return result


async def _prune_removed_series(db, seen_ids: set) -> int:
    """Delete series not in seen_ids (gone from Dispatcharr or in no enabled category). Only called after a full,
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


def _enabled_category_series_ids() -> set | None:
    """Series ids in at least one enabled provider series category, per the dump.
    None when the dump is missing or empty, so callers don't prune on bad data."""
    path = os.environ.get("SERIES_CATEGORY_MAPPING_FILE", "/data/series_category_mapping.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            cats = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Series category dump unreadable, skipping category prune: %s", e)
        return None
    ids = set()
    for c in cats:
        ids.update(c.get("series_ids", []))
    return ids or None


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
        # Series whose provider categories are all disabled in Dispatcharr still come back
        # from /api/vod/series/, so drop them along with series gone from Dispatcharr.
        in_enabled = _enabled_category_series_ids()
        if in_enabled is not None:
            seen_ids &= in_enabled
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
    if not error:
        try:
            await enrich_series_from_tmdb()
        except Exception as e:
            logger.error(f"Series TMDB enrichment after catalog sync failed: {e}")
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
    else:
        # Dispatcharr's provider-info episodes carry no per-episode stream id —
        # only the series relation's account. The episode proxy selects that
        # account's relation itself when given m3u_account_id.
        m3u = ep.get("m3u_account") or {}
        account_id = m3u.get("id")
        account_name = m3u.get("name", "") or ""
        container_extension = ep.get("container_extension", "") or ""

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
            account_id = CASE WHEN excluded.account_id IS NOT NULL THEN excluded.account_id ELSE episodes.account_id END,
            account_name = CASE WHEN excluded.account_id IS NOT NULL THEN excluded.account_name ELSE episodes.account_name END,
            container_extension = CASE WHEN excluded.account_id IS NOT NULL THEN excluded.container_extension ELSE episodes.container_extension END,
            alt_streams = CASE WHEN excluded.account_id IS NOT NULL THEN excluded.alt_streams ELSE episodes.alt_streams END,
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
        ep.setdefault("m3u_account", data.get("m3u_account"))
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
