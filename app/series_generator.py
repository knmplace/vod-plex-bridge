import os
import re
import logging
import asyncio

from config import STRM_SERIES_OUTPUT_DIR
from database import get_db
from generator import parse_title, sanitize_filename, escape_xml

logger = logging.getLogger(__name__)

EPISODE_STRM_BATCH_SIZE = 50
EPISODE_STRM_BATCH_DELAY_SECS = 5


def compute_series_folder_name(series) -> str:
    """Derive the STRM folder name for a series from its title/year (used only when no
    strm_folder is stored yet — once set, the stored value is authoritative)."""
    parsed = parse_title(series["name"], series["year"])
    clean_name = parsed['title']
    clean_year = parsed['year']
    if clean_year:
        return sanitize_filename(f"{clean_name} ({clean_year})")
    return sanitize_filename(clean_name)


def get_series_folder_name(series) -> str:
    """Return the series' stable STRM folder name, preferring the stored value."""
    try:
        stored = series["strm_folder"]
    except (IndexError, KeyError):
        stored = None
    return stored or compute_series_folder_name(series)


def build_tvshow_nfo(series) -> str:
    genres = ""
    genre = series["genre"] if "genre" in series.keys() else None
    if genre:
        for g in genre.split(","):
            g = g.strip()
            if g:
                genres += f"    <genre>{escape_xml(g)}</genre>\n"

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tvshow>
    <title>{escape_xml(series["name"])}</title>
    <year>{series["year"] or ""}</year>
    <rating>{series["rating"] or ""}</rating>
    <plot>{escape_xml(series["description"] or "")}</plot>
    <tmdbid>{escape_xml(series["tmdb_id"] or "")}</tmdbid>
{genres}    <thumb aspect="poster">{escape_xml(series["poster_url"] or "")}</thumb>
</tvshow>
"""


def build_episode_nfo(episode, series) -> str:
    # Episodes don't carry their own poster from the provider; reuse the parent
    # series poster as the episode thumb rather than omitting it entirely, since
    # Plex falls back to season/show art anyway when an episode thumb is absent.
    thumb_tag = ""
    poster_url = series["poster_url"] if "poster_url" in series.keys() else None
    if poster_url:
        thumb_tag = f"    <thumb>{escape_xml(poster_url)}</thumb>\n"

    season = episode["season_number"] if episode["season_number"] is not None else 0
    ep_num = episode["episode_number"] if episode["episode_number"] is not None else 0

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<episodedetails>
    <title>{escape_xml(episode["name"] or "")}</title>
    <season>{season}</season>
    <episode>{ep_num}</episode>
    <aired>{escape_xml(episode["air_date"] or "")}</aired>
    <rating>{episode["rating"] or ""}</rating>
    <plot>{escape_xml(episode["description"] or "")}</plot>
{thumb_tag}</episodedetails>
"""


def build_synthetic_episode_filename(episode, series) -> str:
    """f"{show_clean} - S{season:02d}E{episode:02d} - {title_clean} [{episode_id}].{ext}" """
    show_clean = sanitize_filename(parse_title(series["name"], series["year"])["title"])
    title_clean = sanitize_filename(episode["name"] or "")
    season = episode["season_number"] or 0
    ep_num = episode["episode_number"] or 0
    ext = episode["container_extension"] if "container_extension" in episode.keys() else ""
    ext = (ext or "mkv").lstrip(".")
    base = f"{show_clean} - S{season:02d}E{ep_num:02d} - {title_clean} [{episode['id']}]"
    return f"{base}.{ext}"


async def write_tvshow_and_season_dirs(series, category) -> str:
    """Ensure the series' base folder exists on disk and has a tvshow.nfo.
    Returns the series folder path."""
    folder_name = get_series_folder_name(series)
    series_path = os.path.join(STRM_SERIES_OUTPUT_DIR, category["strm_folder"], folder_name)
    os.makedirs(series_path, exist_ok=True)

    stored_folder = series["strm_folder"] if "strm_folder" in series.keys() else None
    if stored_folder != folder_name or series["category_id"] != category["id"]:
        db = await get_db()
        await db.execute("UPDATE series SET strm_folder = ?, category_id = ? WHERE id = ?",
                          (folder_name, category["id"], series["id"]))
        await db.commit()

    nfo_path = os.path.join(series_path, "tvshow.nfo")
    if not os.path.exists(nfo_path):
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(build_tvshow_nfo(series))

    return series_path


async def write_episode_nfo_batch(series, category, episodes: list, overwrite: bool = False) -> int:
    """Write Season NN/ dirs + .nfo files for the given episodes, batched to avoid
    triggering a Plex-scan-burst provider connection storm on large activations."""
    folder_name = get_series_folder_name(series)
    series_path = os.path.join(STRM_SERIES_OUTPUT_DIR, category["strm_folder"], folder_name)
    os.makedirs(series_path, exist_ok=True)

    written = 0
    total = len(episodes)

    for i in range(0, total, EPISODE_STRM_BATCH_SIZE):
        chunk = episodes[i:i + EPISODE_STRM_BATCH_SIZE]

        for episode in chunk:
            season = episode["season_number"] or 0
            season_dir = os.path.join(series_path, f"Season {season:02d}")
            os.makedirs(season_dir, exist_ok=True)

            filename = build_synthetic_episode_filename(episode, series)
            base_name = os.path.splitext(filename)[0]
            nfo_path = os.path.join(season_dir, f"{base_name}.nfo")

            if overwrite or not os.path.exists(nfo_path):
                try:
                    with open(nfo_path, "w", encoding="utf-8") as f:
                        f.write(build_episode_nfo(episode, series))
                    written += 1
                except Exception as e:
                    logger.warning("Failed to write NFO for episode %s: %s", episode["id"], e)

        if i + EPISODE_STRM_BATCH_SIZE < total:
            await asyncio.sleep(EPISODE_STRM_BATCH_DELAY_SECS)

    return written
