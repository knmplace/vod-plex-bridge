"""Mark catalog movies, series and episodes that Plex already has outside the bridge."""
import asyncio
import logging
import re
import unicodedata
from collections import defaultdict

import httpx

from config import PLEX_URL, PLEX_TOKEN, PLEX_MOVIE_MOUNT_ROOT, PLEX_SERIES_MOUNT_ROOT
from database import get_db
from generator import parse_title

logger = logging.getLogger(__name__)

def _headers() -> dict:
    # Built per call so a token saved in Settings applies without a restart.
    return {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"}
_PAGE = 1000
# Files the bridge itself serves to Plex; those titles already show as ACTIVE.
_BRIDGE_ROOTS = tuple(r + "/" for r in (PLEX_MOVIE_MOUNT_ROOT, PLEX_SERIES_MOUNT_ROOT) if r)
_lock = asyncio.Lock()


def normalize_title(s: str) -> str:
    # "Amélie" == "Amelie", "Spider-Man: Homecoming" == "spider man homecoming", leading "The" ignored.
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", " and ")).strip()
    return re.sub(r"^(the|a|an) ", "", s)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _id(v):
    v = str(v).strip().lower() if v not in (None, "") else ""
    return v or None


def _guids(md: dict) -> dict:
    out = {}
    for g in md.get("Guid", []) or []:
        scheme, _, value = (g.get("id") or "").partition("://")
        if value:
            out[scheme] = value
    return out


def _from_bridge(md: dict) -> bool:
    files = [p.get("file", "") for m in md.get("Media", []) or [] for p in m.get("Part", []) or []]
    return bool(files) and all(f.startswith(_BRIDGE_ROOTS) for f in files)


class _Index:
    """Plex entries looked up TMDB -> IMDb -> title + year (±1), never across conflicting ids."""

    def __init__(self):
        self.by_tmdb = defaultdict(list)
        self.by_imdb = defaultdict(list)
        self.by_title = defaultdict(list)

    def add(self, tmdb, imdb, title, year, payload):
        entry = (_id(tmdb), _id(imdb), _int(year), payload)
        if entry[0]:
            self.by_tmdb[entry[0]].append(entry)
        if entry[1]:
            self.by_imdb[entry[1]].append(entry)
        norm = normalize_title(title)
        if norm:
            self.by_title[norm].append(entry)

    def find(self, tmdb, imdb, title, year) -> list:
        tmdb, imdb, year = _id(tmdb), _id(imdb), _int(year)
        found = list(self.by_tmdb.get(tmdb, [])) if tmdb else []
        if imdb:
            found += self.by_imdb.get(imdb, [])
        candidates = [e for e in self.by_title.get(normalize_title(title), [])
                      if not (tmdb and e[0] and e[0] != tmdb) and not (imdb and e[1] and e[1] != imdb)]
        identities = {e[:3] for e in candidates}
        for e in candidates:
            if year is not None and e[2] is not None:
                if abs(year - e[2]) <= 1:
                    found.append(e)
            elif len(identities) == 1:
                found.append(e)
        out, seen = [], set()
        for e in found:
            if id(e[3]) not in seen:
                seen.add(id(e[3]))
                out.append(e[3])
        return out


async def _list(client: httpx.AsyncClient, section: str, item_type: int) -> list[dict]:
    items, start = [], 0
    while True:
        resp = await client.get(f"{PLEX_URL}/library/sections/{section}/all", params={
            "type": item_type, "includeGuids": 1,
            "X-Plex-Container-Start": start, "X-Plex-Container-Size": _PAGE,
        })
        resp.raise_for_status()
        mc = resp.json().get("MediaContainer", {})
        page = mc.get("Metadata", []) or []
        items += page
        start += len(page)
        if not page or start >= mc.get("totalSize", start):
            return items


async def _read_plex():
    movies, shows = _Index(), _Index()
    counts = {"plex_movies": 0, "plex_shows": 0}
    async with httpx.AsyncClient(timeout=120, headers=_headers()) as client:
        resp = await client.get(f"{PLEX_URL}/library/sections")
        resp.raise_for_status()
        for d in resp.json().get("MediaContainer", {}).get("Directory", []):
            if d.get("type") == "movie":
                for md in await _list(client, d["key"], 1):
                    if _from_bridge(md):
                        continue
                    g = _guids(md)
                    movies.add(g.get("tmdb"), g.get("imdb"), md.get("title"), md.get("year"), d["title"])
                    counts["plex_movies"] += 1
            elif d.get("type") == "show":
                # Shows have no files of their own; a show counts once it has a non-bridge episode.
                show_md = {s["ratingKey"]: s for s in await _list(client, d["key"], 2)}
                found = {}
                for ep in await _list(client, d["key"], 4):
                    if _from_bridge(ep):
                        continue
                    show = found.setdefault(ep.get("grandparentRatingKey"), {"label": d["title"], "eps": set()})
                    season, number = _int(ep.get("parentIndex")), _int(ep.get("index"))
                    if season is not None and number is not None:
                        show["eps"].add((season, number))
                for key, show in found.items():
                    s = show_md.get(key) or {}
                    g = _guids(s)
                    shows.add(g.get("tmdb"), g.get("imdb"), s.get("title"), s.get("year"), show)
                    counts["plex_shows"] += 1
    return movies, shows, counts


def _join(labels) -> str:
    return ", ".join(dict.fromkeys(labels))


async def scan() -> dict:
    if _lock.locked():
        raise RuntimeError("A Plex library scan is already running")
    async with _lock:
        movies, shows, counts = await _read_plex()
        db = await get_db()

        movie_marks = []
        for r in await (await db.execute("SELECT id, name, year, tmdb_id, imdb_id FROM movies")).fetchall():
            t = parse_title(r["name"], _int(r["year"]))
            hits = movies.find(r["tmdb_id"], r["imdb_id"], t["title"], t["year"])
            if hits:
                movie_marks.append((_join(hits), r["id"]))

        series_marks, series_eps = [], {}
        for r in await (await db.execute("SELECT id, name, year, tmdb_id FROM series")).fetchall():
            t = parse_title(r["name"], _int(r["year"]))
            hits = shows.find(r["tmdb_id"], None, t["title"], t["year"])
            if hits:
                series_marks.append((_join(h["label"] for h in hits), r["id"]))
                series_eps[r["id"]] = (_join(h["label"] for h in hits), set().union(*(h["eps"] for h in hits)))

        episode_marks = []
        if series_eps:
            ids = list(series_eps)
            rows = await db.execute(
                f"SELECT id, series_id, season_number, episode_number FROM episodes "
                f"WHERE series_id IN ({','.join('?' * len(ids))})", ids)
            for r in await rows.fetchall():
                label, eps = series_eps[r["series_id"]]
                if (r["season_number"], r["episode_number"]) in eps:
                    episode_marks.append((label, r["id"]))

        for table, marks in (("movies", movie_marks), ("series", series_marks), ("episodes", episode_marks)):
            await db.execute(f"UPDATE {table} SET in_plex = NULL WHERE in_plex IS NOT NULL")
            await db.executemany(f"UPDATE {table} SET in_plex = ? WHERE id = ?", marks)
        await db.commit()

        result = dict(counts, movies_marked=len(movie_marks), series_marked=len(series_marks),
                      episodes_marked=len(episode_marks))
        logger.info("Plex presence scan: %s", result)
        return result
