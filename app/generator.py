import os
import shutil
import re
import logging
import httpx
from datetime import datetime, timezone

from config import STRM_OUTPUT_DIR, BRIDGE_HOST, BRIDGE_PORT
from database import get_db

logger = logging.getLogger(__name__)


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

                clean_name = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', name).strip()
                clean_name = re.sub(r'\s*\(\d{4}\)\s*$', '', clean_name).strip()

                if year:
                    folder_name = sanitize_filename(f"{clean_name} ({year})")
                    file_name = f"{clean_name} ({year})"
                else:
                    folder_name = sanitize_filename(clean_name)
                    file_name = clean_name

                file_name = sanitize_filename(file_name)
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

    clean_name = re.sub(r'\s*[-–]\s*\d{4}\s*$', '', name).strip()
    clean_name = re.sub(r'\s*\(\d{4}\)\s*$', '', clean_name).strip()

    if year:
        folder_name = sanitize_filename(f"{clean_name} ({year})")
        file_name = sanitize_filename(f"{clean_name} ({year})")
    else:
        folder_name = sanitize_filename(clean_name)
        file_name = sanitize_filename(clean_name)

    folder_path = os.path.join(STRM_OUTPUT_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    strm_url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/stream/{movie_id}.mp4"
    strm_path = os.path.join(folder_path, f"{file_name}.strm")
    with open(strm_path, "w", encoding="utf-8") as f:
        f.write(strm_url)

    nfo_path = os.path.join(folder_path, f"{file_name}.nfo")
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(build_nfo(movie))

    poster_url = movie.get("poster_url") or ""
    poster_path = os.path.join(folder_path, "poster.jpg")
    if poster_url and not os.path.exists(poster_path):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(poster_url)
                if resp.status_code == 200:
                    with open(poster_path, "wb") as f:
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

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
    <title>{escape_xml(movie["name"])}</title>
    <year>{movie["year"] or ""}</year>
    <rating>{movie["rating"] or ""}</rating>
    <plot>{escape_xml(movie["description"] or "")}</plot>
    <tmdbid>{escape_xml(movie["tmdb_id"] or "")}</tmdbid>
    <imdbid>{escape_xml(movie["imdb_id"] or "")}</imdbid>
{genres}    <thumb aspect="poster">{escape_xml(movie["poster_url"] or "")}</thumb>
</movie>
"""


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
