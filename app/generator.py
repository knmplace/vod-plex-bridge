import os
import shutil
import re
import logging
import httpx
from datetime import datetime, timezone

from config import STRM_OUTPUT_DIR, BRIDGE_HOST, BRIDGE_PORT
from database import get_db

logger = logging.getLogger(__name__)

_LANG_PREFIXES = {
    'EN', 'FR', 'DE', 'IT', 'ES', 'PT', 'NL', 'PL', 'RU', 'AR', 'TR', 'SV', 'SE',
    'NO', 'DA', 'DK', 'FI', 'EL', 'GR', 'RO', 'CS', 'CZ', 'HU', 'HE', 'HI', 'JA',
    'KO', 'ZH', 'UK', 'BG', 'HR', 'SR', 'MULTI', 'MULTISUB', 'VOSTFR', 'VOST',
    'VOSE', 'LAT', 'VO', 'DUAL',
}

_QUAL_PREFIXES = {
    '4K', 'UHD', 'HD', 'FHD', 'SD', 'HDR', 'HDR10', 'DV', '3D', 'HEVC', 'H265',
    'X265', 'H264', 'X264', '1080P', '720P', '2160P', '480P', 'HDTS', 'HDCAM',
    'CAM', 'TS', 'WEB', 'WEBDL', 'WEBRIP', 'BLURAY', 'BRRIP', 'DVDRIP', 'REMUX',
    'D+', 'A+', 'N', 'P+', 'HBO', 'MAX',
}

_QUAL_INLINE = {
    '4K', 'UHD', 'FHD', 'QHD', 'HDR', 'HDR10', 'HEVC', 'H265', 'X265', 'H264',
    'X264', 'XVID', '1080P', '720P', '2160P', '480P', '4320P', 'HDTS', 'HDCAM',
    'WEBDL', 'WEBRIP', 'BRRIP', 'BLURAY', 'DVDRIP', 'REMUX',
}

_TAG_RE = re.compile(
    r'[\[(]\s*(?:MULTI[- ]?SUB|MULTISUB|SUB|DUAL|VOST(?:FR|E)?|HDTS|HDCAM|CAM|HDR|'
    r'HEVC|MAIN CARD|PRELIMS|EARLY PRELIMS|UNCUT|EXTENDED|REMASTERED|IMAX|3D|REPACK|'
    r'\d{3,4}P)\s*[\])]', re.IGNORECASE)

_YEAR_RE = re.compile(r'(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)')
_PAREN_YEAR_RE = re.compile(r'[(\[]\s*((?:19|20)\d{2})\s*[)\]]')


def _looks_like_prefix(token: str) -> bool:
    t = (token or '').strip()
    if not t or len(t) > 12:
        return False
    parts = [p for p in re.split(r'[-\s|]+', t.upper()) if p]
    if not parts:
        return False
    for p in parts:
        if p in _QUAL_PREFIXES or p in _LANG_PREFIXES:
            continue
        if re.fullmatch(r'[A-Z0-9]{1,4}\+?', p):
            continue
        return False
    return True


def _strip_inline_quality(name: str) -> str:
    return re.sub(
        r'\b[A-Za-z0-9]{2,7}\b',
        lambda m: ' ' if m.group(0).upper() in _QUAL_INLINE else m.group(0),
        name,
    )


def _finalize(name: str) -> str:
    name = _TAG_RE.sub(' ', name)
    name = re.sub(r'\[[^\]]*\]', ' ', name)
    name = _strip_inline_quality(name)
    name = re.sub(r'\s*\((?:[A-Za-z]{2})\)\s*$', ' ', name)
    name = re.sub(r'[\[(]\s*[\])]', ' ', name)
    return re.sub(r'\s+', ' ', name).strip(' -._')


def parse_title(raw: str, year_field: int | None = None) -> dict:
    """Clean a provider VOD title into {title, year}."""
    name = re.sub(r'\s+', ' ', (raw or '').strip())

    if '|' in name:
        left, right = name.split('|', 1)
        if _looks_like_prefix(left):
            name = right.strip()

    changed = True
    while changed and ' - ' in name:
        changed = False
        left, right = name.split(' - ', 1)
        if _looks_like_prefix(left):
            name = right.strip()
            changed = True

    name = re.sub(r'^\d{1,4}\.\s+', '', name)

    has_year_field = isinstance(year_field, int) and 1900 <= year_field <= 2100
    year = year_field if has_year_field else None

    if ' ' not in name and name.count('.') >= 2:
        name = name.replace('.', ' ')

    name_before_year = name
    paren = _PAREN_YEAR_RE.search(name)
    if paren:
        if year is None:
            year = int(paren.group(1))
        work = name[:paren.start()]
    else:
        matches = _YEAR_RE.findall(name)
        if year is None and matches:
            year = int(matches[-1])
        work = name
        if matches:
            # Only strip the year from the title if it matches the year we're using.
            # "Blade Runner 2049" (year_field=2017) should keep "2049" in the title.
            yr_to_strip = str(year) if year else matches[-1]
            work = re.sub(r'[\(\[]?\b' + yr_to_strip + r'\b[\)\]]?', ' ', name)

    work = _finalize(work)
    if not work:
        work = _finalize(name_before_year)
        if not has_year_field:
            year = None

    return {'title': work, 'year': year}


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip('. ')
    return name[:200]


async def generate_strm_files():
    """Generate .strm + .nfo + poster files based on active filter configs."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE sync_state SET status = 'generating', message = 'Generating STRM files...' WHERE id = 1"
        )
        await db.commit()

        filters = await db.execute(
            "SELECT genre, limit_count, sort_by, sort_order FROM filter_configs WHERE enabled = 1"
        )
        active_filters = await filters.fetchall()

        if not active_filters:
            logger.info("No active filters configured, nothing to generate")
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = 'No active filters', active_strm_count = 0 WHERE id = 1"
            )
            await db.commit()
            return 0

        # Get movies from selected categories
        sel_rows = await db.execute("SELECT category_id FROM selected_categories WHERE enabled = 1")
        selected_cats = [r["category_id"] for r in await sel_rows.fetchall()]

        selected_ids = set()

        if selected_cats and not active_filters:
            # Categories selected but no genre filters — include all movies from selected categories
            placeholders = ",".join("?" for _ in selected_cats)
            rows = await db.execute(
                f"SELECT DISTINCT m.id FROM movies m "
                f"JOIN movie_categories mc ON m.id = mc.movie_id "
                f"WHERE mc.category_id IN ({placeholders}) AND m.name != ''",
                selected_cats,
            )
            selected_ids = {row["id"] for row in await rows.fetchall()}
        elif active_filters:
            for f in active_filters:
                genre = f["genre"]
                limit = f["limit_count"]
                sort_by = f["sort_by"] if f["sort_by"] in ("rating", "year", "name") else "rating"
                sort_order = "DESC" if f["sort_order"] == "desc" else "ASC"

                sort_col = {"rating": "rating", "year": "year", "name": "name"}[sort_by]

                if selected_cats:
                    placeholders = ",".join("?" for _ in selected_cats)
                    rows = await db.execute(
                        f"SELECT m.id FROM movies m "
                        f"JOIN movie_categories mc ON m.id = mc.movie_id "
                        f"WHERE mc.category_id IN ({placeholders}) AND m.genre LIKE ? AND m.name != '' "
                        f"ORDER BY m.{sort_col} {sort_order} LIMIT ?",
                        selected_cats + [f"%{genre}%", limit],
                    )
                else:
                    rows = await db.execute(
                        f"SELECT id FROM movies WHERE genre LIKE ? AND name != '' ORDER BY {sort_col} {sort_order} LIMIT ?",
                        (f"%{genre}%", limit),
                    )
                ids = [row["id"] for row in await rows.fetchall()]
                selected_ids.update(ids)

        if not selected_ids:
            logger.warning("Filters matched no movies")
            await db.execute(
                "UPDATE sync_state SET status = 'idle', message = 'Filters matched 0 movies', active_strm_count = 0 WHERE id = 1"
            )
            await db.commit()
            return 0

        placeholders = ",".join("?" for _ in selected_ids)
        rows = await db.execute(
            f"SELECT * FROM movies WHERE id IN ({placeholders})",
            list(selected_ids),
        )
        movies = await rows.fetchall()

        os.makedirs(STRM_OUTPUT_DIR, exist_ok=True)

        existing_dirs = set()
        if os.path.exists(STRM_OUTPUT_DIR):
            existing_dirs = set(os.listdir(STRM_OUTPUT_DIR))

        generated_dirs = set()
        generated = 0

        async with httpx.AsyncClient(timeout=10.0) as client:
            for movie in movies:
                name = movie["name"]
                year = movie["year"]
                if not name:
                    continue

                parsed = parse_title(name, year)
                clean_name = parsed['title']
                clean_year = parsed['year']

                if clean_year:
                    folder_name = sanitize_filename(f"{clean_name} ({clean_year})")
                else:
                    folder_name = sanitize_filename(clean_name)

                file_name = sanitize_filename(folder_name)
                folder_path = os.path.join(STRM_OUTPUT_DIR, folder_name)
                os.makedirs(folder_path, exist_ok=True)
                generated_dirs.add(folder_name)

                strm_url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/stream/{movie['id']}.mp4"
                strm_path = os.path.join(folder_path, f"{file_name}.strm")
                with open(strm_path, "w", encoding="utf-8") as f:
                    f.write(strm_url)

                nfo_path = os.path.join(folder_path, f"{file_name}.nfo")
                nfo_content = build_nfo(movie)
                with open(nfo_path, "w", encoding="utf-8") as f:
                    f.write(nfo_content)

                poster_url = movie["poster_url"]
                poster_path = os.path.join(folder_path, "poster.jpg")
                if poster_url and not os.path.exists(poster_path):
                    try:
                        resp = await client.get(poster_url)
                        if resp.status_code == 200:
                            with open(poster_path, "wb") as f:
                                f.write(resp.content)
                    except Exception as e:
                        logger.debug(f"Failed to download poster for {name}: {e}")

                backdrop_url = movie["backdrop_url"] if "backdrop_url" in movie.keys() else None
                fanart_path = os.path.join(folder_path, "fanart.jpg")
                if backdrop_url and not os.path.exists(fanart_path):
                    try:
                        resp = await client.get(backdrop_url)
                        if resp.status_code == 200:
                            with open(fanart_path, "wb") as f:
                                f.write(resp.content)
                    except Exception as e:
                        logger.debug(f"Failed to download fanart for {name}: {e}")

                generated += 1

        stale_dirs = existing_dirs - generated_dirs
        for d in stale_dirs:
            full_path = os.path.join(STRM_OUTPUT_DIR, d)
            if os.path.isdir(full_path):
                shutil.rmtree(full_path)
                logger.info(f"Removed stale directory: {d}")

        await db.execute(
            "UPDATE sync_state SET last_strm_sync = ?, active_strm_count = ?, status = 'idle', message = ? WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(), generated, f"Generated {generated} STRM files, removed {len(stale_dirs)} stale"),
        )
        await db.commit()
        logger.info(f"Generated {generated} STRM files, removed {len(stale_dirs)} stale directories")
        return generated
    finally:
        pass


async def write_strm_for_movie(movie):
    """Write .strm + .nfo + poster for a single activated movie."""
    name = movie["name"]
    year = movie["year"]
    movie_id = movie["id"]
    if not name:
        return False

    parsed = parse_title(name, year)
    clean_name = parsed['title']
    clean_year = parsed['year']

    if clean_year:
        folder_name = sanitize_filename(f"{clean_name} ({clean_year})")
    else:
        folder_name = sanitize_filename(clean_name)
    file_name = sanitize_filename(folder_name)

    folder_path = os.path.join(STRM_OUTPUT_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    strm_url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/stream/{movie_id}.mp4"
    strm_path = os.path.join(folder_path, f"{file_name}.strm")
    with open(strm_path, "w", encoding="utf-8") as f:
        f.write(strm_url)

    nfo_path = os.path.join(folder_path, f"{file_name}.nfo")
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(build_nfo(movie))

    async with httpx.AsyncClient(timeout=10.0) as client:
        poster_url = movie.get("poster_url") or ""
        poster_path = os.path.join(folder_path, "poster.jpg")
        if poster_url and not os.path.exists(poster_path):
            try:
                resp = await client.get(poster_url)
                if resp.status_code == 200:
                    with open(poster_path, "wb") as f:
                        f.write(resp.content)
            except Exception:
                pass

        backdrop_url = movie.get("backdrop_url") or ""
        fanart_path = os.path.join(folder_path, "fanart.jpg")
        if backdrop_url and not os.path.exists(fanart_path):
            try:
                resp = await client.get(backdrop_url)
                if resp.status_code == 200:
                    with open(fanart_path, "wb") as f:
                        f.write(resp.content)
            except Exception:
                pass

    return True


def build_nfo(movie) -> str:
    genres = ""
    if movie["genre"]:
        for g in movie["genre"].split(","):
            g = g.strip()
            if g:
                genres += f"    <genre>{escape_xml(g)}</genre>\n"

    director_tag = ""
    director = movie.get("director") or ""
    if director:
        director_tag = f"    <director>{escape_xml(director)}</director>\n"

    actors = ""
    cast_info = movie.get("cast_info") or ""
    if cast_info:
        for a in cast_info.split(","):
            a = a.strip()
            if a:
                actors += f"    <actor>\n        <name>{escape_xml(a)}</name>\n    </actor>\n"

    country_tag = ""
    country = movie.get("country") or ""
    if country:
        country_tag = f"    <country>{escape_xml(country)}</country>\n"

    fanart_tag = ""
    backdrop = movie.get("backdrop_url") or ""
    if backdrop:
        fanart_tag = f"    <fanart>\n        <thumb>{escape_xml(backdrop)}</thumb>\n    </fanart>\n"

    trailer_tag = ""
    trailer_key = movie.get("trailer_key") or ""
    if trailer_key:
        trailer_tag = f"    <trailer>plugin://plugin.video.youtube/?action=play_video&amp;videoid={escape_xml(trailer_key)}</trailer>\n"

    release_tag = ""
    release_date = movie.get("release_date") or ""
    if release_date:
        release_tag = f"    <premiered>{escape_xml(release_date)}</premiered>\n"

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
    <title>{escape_xml(movie["name"])}</title>
    <year>{movie["year"] or ""}</year>
    <rating>{movie["rating"] or ""}</rating>
    <plot>{escape_xml(movie["description"] or "")}</plot>
    <tmdbid>{escape_xml(movie["tmdb_id"] or "")}</tmdbid>
    <imdbid>{escape_xml(movie["imdb_id"] or "")}</imdbid>
{genres}{director_tag}{country_tag}{release_tag}{trailer_tag}    <thumb aspect="poster">{escape_xml(movie["poster_url"] or "")}</thumb>
{fanart_tag}{actors}</movie>
"""


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
