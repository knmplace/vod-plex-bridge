"""Keep Plex TV libraries in step with the bridge's series categories."""
import logging

import httpx

from config import PLEX_URL, PLEX_TOKEN, PLEX_SERIES_MOUNT_ROOT

logger = logging.getLogger(__name__)

_HEADERS = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"}
SHOW_AGENT = "tv.plex.agents.series"
SHOW_SCANNER = "Plex TV Series"


def configured() -> bool:
    return bool(PLEX_URL and PLEX_TOKEN)


def category_location(strm_folder: str) -> str:
    return f"{PLEX_SERIES_MOUNT_ROOT}/{strm_folder}"


async def list_show_sections() -> list[dict]:
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        resp = await client.get(f"{PLEX_URL}/library/sections")
        resp.raise_for_status()
    sections = []
    for d in resp.json().get("MediaContainer", {}).get("Directory", []):
        if d.get("type") != "show":
            continue
        sections.append({
            "id": int(d["key"]),
            "title": d.get("title", ""),
            "agent": d.get("agent", SHOW_AGENT),
            "scanner": d.get("scanner", SHOW_SCANNER),
            "language": d.get("language", "en-US"),
            "locations": [loc["path"] for loc in d.get("Location", [])],
        })
    return sections


async def _get_section(section_id: int) -> dict | None:
    for s in await list_show_sections():
        if s["id"] == int(section_id):
            return s
    return None


async def _set_locations(section: dict, locations: list[str]):
    # Plex replaces the whole location list on edit, so always send every path.
    params = [("agent", section["agent"]), ("language", section["language"]), ("name", section["title"])]
    params += [("location", p) for p in locations]
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        resp = await client.put(f"{PLEX_URL}/library/sections/{section['id']}", params=params)
        resp.raise_for_status()


async def add_location(section_id: int, path: str) -> bool:
    section = await _get_section(section_id)
    if not section:
        raise ValueError(f"Plex library {section_id} not found")
    if path in section["locations"]:
        return False
    await _set_locations(section, section["locations"] + [path])
    logger.info("Plex: added %s to library %s (%s)", path, section_id, section["title"])
    return True


async def remove_location(section_id: int, path: str, delete_if_empty: bool = False):
    section = await _get_section(section_id)
    if not section or path not in section["locations"]:
        return
    remaining = [p for p in section["locations"] if p != path]
    if not remaining:
        # Plex won't keep a library with no folders; only drop libraries the bridge made.
        if delete_if_empty:
            await delete_section(section_id)
        return
    await _set_locations(section, remaining)
    logger.info("Plex: removed %s from library %s (%s)", path, section_id, section["title"])


async def create_section(name: str, path: str) -> int:
    params = [
        ("name", name), ("type", "show"), ("agent", SHOW_AGENT),
        ("scanner", SHOW_SCANNER), ("language", "en-US"), ("location", path),
    ]
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        resp = await client.post(f"{PLEX_URL}/library/sections", params=params)
        resp.raise_for_status()
    dirs = resp.json().get("MediaContainer", {}).get("Directory", [])
    if dirs:
        section_id = int(dirs[0]["key"])
    else:
        # Older servers return an empty body; find the new library by its folder.
        match = [s for s in await list_show_sections() if path in s["locations"]]
        if not match:
            raise RuntimeError("Plex created the library but it could not be found")
        section_id = match[0]["id"]
    logger.info("Plex: created library %s (%s) at %s", section_id, name, path)
    return section_id


async def delete_section(section_id: int):
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        resp = await client.delete(f"{PLEX_URL}/library/sections/{section_id}")
        resp.raise_for_status()
    logger.info("Plex: deleted library %s", section_id)
