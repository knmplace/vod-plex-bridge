import asyncio
import hashlib
import json
import logging
import os
import time
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
                           tmdb_id, poster_url, episode_count, synced_at, trailer_key, dispatcharr_hash, language)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            dispatcharr_hash=excluded.dispatcharr_hash,
            language = COALESCE(NULLIF(excluded.language, ''), series.language)
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
        (series.get("language") or "").strip().lower()[:8],
    ))


_CONTINUING = {"returning series", "in production", "planned", "pilot"}
_ENDED = {"ended", "canceled", "cancelled"}


def map_tmdb_status(status: str | None) -> str | None:
    """TMDB /tv status -> 'continuing' / 'ended' (None when unknown)."""
    s = (status or "").strip().lower()
    if s in _CONTINUING:
        return "continuing"
    if s in _ENDED:
        return "ended"
    return None


async def enrich_series_from_tmdb(limit: int = 5000) -> dict:
    """TMDB (local store first, API on a miss) for series flagged by a Dispatcharr change
    or never checked: TMDB wins for title, poster, plot, genre and year. Locked series and
    series without a TMDB id keep Dispatcharr's data; folder names never change."""
    import tmdb_local
    result = {"enriched": 0, "changed": 0, "nfo_rewritten": 0, "not_found": 0, "failed": 0,
              "remaining": 0, "local": 0, "api": 0, "fields": {},
              "status_changed": 0}
    if not tmdb_local.has_credentials():
        return result

    import tmdb_store
    run_started = time.time()
    db = await get_db()

    # Shows the TMDB store refreshed since the last pass (e.g. flagged by /tv/changes) are
    # re-applied, so a show that ends or is cancelled gets its status updated.
    since = float(await asyncio.to_thread(tmdb_store.get_meta, "series_applied_at", "0") or 0)
    if since:
        ids = [str(i) for i in await asyncio.to_thread(tmdb_store.list_fetched_since, "tv", since)]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            await db.execute(
                f"UPDATE series SET tmdb_check = 1 WHERE tmdb_enriched_at IS NOT NULL "
                f"AND tmdb_id IN ({','.join('?' * len(chunk))})", chunk,
            )
        await db.commit()

    # lang_checked / status_checked = 0 also re-queue series enriched before those columns existed.
    pending = ("(tmdb_check = 1 OR tmdb_enriched_at IS NULL OR COALESCE(lang_checked, 0) = 0 "
               "OR COALESCE(status_checked, 0) = 0) "
               "AND tmdb_id IS NOT NULL AND tmdb_id != '' "
               "AND COALESCE(tmdb_locked, 0) = 0")
    rows = await (await db.execute(
        f"SELECT id, tmdb_id, name, poster_url, description, genre, year, language, tmdb_status FROM series "
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
                await db.execute("UPDATE series SET tmdb_check = 0, lang_checked = 1, status_checked = 1, "
                                 "tmdb_enriched_at = ? WHERE id = ?", (now, s["id"]))
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
            # Dispatcharr's language (from the category) wins; TMDB's original language fills a gap.
            # Not part of tvshow.nfo, so it never triggers a rewrite.
            lang = "" if s["language"] else (data.get("original_language") or "").strip().lower()
            sets += "language = ?, " if lang else ""
            vals = (*changes.values(), lang) if lang else tuple(changes.values())
            # Show status for the card badge; not part of tvshow.nfo.
            status = map_tmdb_status(data.get("status"))
            if status and status != s["tmdb_status"]:
                result["status_changed"] += 1
            sets += "tmdb_status = COALESCE(?, tmdb_status), "
            vals = (*vals, status)
            await db.execute(f"UPDATE series SET {sets}tmdb_check = 0, lang_checked = 1, status_checked = 1, "
                             f"tmdb_enriched_at = ? WHERE id = ?", (*vals, now, s["id"]))
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

    if not _cancel:
        await asyncio.to_thread(tmdb_store.set_meta, "series_applied_at", run_started)
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


async def tag_series_accounts() -> dict:
    """Store which provider accounts carry each series (series.account_names, comma list),
    shown as provider tags on the series cards. Dispatcharr API only: one count request per
    account, and a full id listing only for an account that carries some but not all series."""
    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{DISPATCHARR_URL}/api/m3u/accounts/", headers=headers)
        resp.raise_for_status()
        accounts = resp.json()
        if isinstance(accounts, dict):
            accounts = accounts.get("results", [])

        async def count(extra: str) -> int:
            r = await client.get(f"{DISPATCHARR_URL}/api/vod/series/?page_size=1{extra}", headers=headers)
            r.raise_for_status()
            return int(r.json().get("count") or 0)

        total = await count("")
        everywhere, partial = [], {}
        for a in sorted(accounts, key=lambda a: (a.get("name") or "").lower()):
            name = (a.get("name") or "").strip() or f"account {a.get('id')}"
            n = await count(f"&m3u_account={a['id']}")
            if n == 0:
                continue
            if n >= total:
                everywhere.append(name)
                continue
            ids, page = set(), 1
            while True:
                r = await client.get(f"{DISPATCHARR_URL}/api/vod/series/?m3u_account={a['id']}"
                                     f"&page={page}&page_size={PAGE_SIZE}", headers=headers)
                r.raise_for_status()
                data = r.json()
                ids.update(x["id"] for x in data.get("results", []))
                if not data.get("next"):
                    break
                page += 1
                await asyncio.sleep(0.05)
            partial[name] = ids

    db = await get_db()
    rows = await (await db.execute("SELECT id, account_names FROM series")).fetchall()
    updates = []
    for r in rows:
        names = everywhere + [n for n, ids in partial.items() if r["id"] in ids]
        value = ", ".join(sorted(names, key=str.lower)) or None
        if value != r["account_names"]:
            updates.append((value, r["id"]))
    await db.executemany("UPDATE series SET account_names = ? WHERE id = ?", updates)
    await db.commit()
    result = {"accounts": len(everywhere) + len(partial), "updated": len(updates)}
    logger.info("Series provider tags: %s", result)
    return result


_scrape_running = False


def is_scrape_running() -> bool:
    """True from the start of a series sync until its tagging and TMDB enrichment finish."""
    return _scrape_running


async def scrape_series_catalog(max_series: int = 0):
    """One series sync at a time: a second request (a double click, or the schedule coming due
    while a manual sync is in its TMDB step) is ignored. A full sync also restarts the schedule's
    clock, so a scheduled sync doesn't run straight after a manual one."""
    global _scrape_running
    if _scrape_running:
        logger.info("Series catalog sync already running; request ignored")
        return 0
    _scrape_running = True
    try:
        if max_series == 0:
            db = await get_db()
            await db.execute("UPDATE series_sync_state SET last_auto_catalog_sync = ? WHERE id = 1",
                             (datetime.now().isoformat(timespec="seconds"),))
            await db.commit()
        return await _scrape_series_catalog(max_series)
    finally:
        _scrape_running = False


async def _scrape_series_catalog(max_series: int = 0):
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
    if completed:
        try:
            await tag_series_accounts()
        except Exception as e:
            logger.error(f"Series provider tagging after catalog sync failed: {e}")
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
            synced_at=excluded.synced_at,
            -- A new uuid is a different file in Dispatcharr: drop the cached head/tail and size.
            header_data = CASE WHEN episodes.uuid != excluded.uuid THEN NULL ELSE episodes.header_data END,
            header_size = CASE WHEN episodes.uuid != excluded.uuid THEN 0 ELSE episodes.header_size END,
            tail_data = CASE WHEN episodes.uuid != excluded.uuid THEN NULL ELSE episodes.tail_data END,
            tail_size = CASE WHEN episodes.uuid != excluded.uuid THEN 0 ELSE episodes.tail_size END,
            tail_offset = CASE WHEN episodes.uuid != excluded.uuid THEN 0 ELSE episodes.tail_offset END,
            file_size = CASE WHEN episodes.uuid != excluded.uuid THEN NULL ELSE episodes.file_size END,
            cache_account_id = CASE WHEN episodes.uuid != excluded.uuid THEN NULL ELSE episodes.cache_account_id END
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
