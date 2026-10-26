"""Keep Plex libraries in step with the bridge's series and movie categories."""
import logging

import httpx

from config import PLEX_URL, PLEX_TOKEN, PLEX_SERIES_MOUNT_ROOT, PLEX_MOVIE_CATEGORY_MOUNT_ROOT

logger = logging.getLogger(__name__)

def _headers() -> dict:
    # Built per call so a token saved in Settings applies without a restart.
    return {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"}
SHOW_AGENT = "tv.plex.agents.series"
SHOW_SCANNER = "Plex TV Series"
MOVIE_AGENT = "tv.plex.agents.movie"
MOVIE_SCANNER = "Plex Movie"
_KINDS = {"show": (SHOW_AGENT, SHOW_SCANNER), "movie": (MOVIE_AGENT, MOVIE_SCANNER)}
# Every thumbnail or marker pass opens the stream, so a big catalog would hit the provider.
MOVIE_PREFS = {"enableBIFGeneration": "0", "enableCreditsMarkerGeneration": "0"}


def configured() -> bool:
    return bool(PLEX_URL and PLEX_TOKEN)


def is_windows_path(root: str) -> bool:
    return "\\" in root or (len(root) >= 2 and root[1] == ":")


def join_mount(root: str, name: str) -> str:
    """A folder under a mount root, as the Plex host spells it (V:\\Show on Windows)."""
    sep = "\\" if is_windows_path(root) else "/"
    return f"{root.rstrip(sep)}{sep}{name}"


def category_location(strm_folder: str, kind: str = "show") -> str:
    root = PLEX_MOVIE_CATEGORY_MOUNT_ROOT if kind == "movie" else PLEX_SERIES_MOUNT_ROOT
    return join_mount(root, strm_folder)


async def list_show_sections() -> list[dict]:
    return await list_sections("show")


async def list_sections(kind: str) -> list[dict]:
    agent, scanner = _KINDS[kind]
    async with httpx.AsyncClient(timeout=10, headers=_headers()) as client:
        resp = await client.get(f"{PLEX_URL}/library/sections")
        resp.raise_for_status()
    sections = []
    for d in resp.json().get("MediaContainer", {}).get("Directory", []):
        if d.get("type") != kind:
            continue
        sections.append({
            "id": int(d["key"]),
            "title": d.get("title", ""),
            "agent": d.get("agent", agent),
            "scanner": d.get("scanner", scanner),
            "language": d.get("language", "en-US"),
            "locations": [loc["path"] for loc in d.get("Location", [])],
        })
    return sections


async def _get_section(section_id: int) -> dict | None:
    for s in await list_sections("show") + await list_sections("movie"):
        if s["id"] == int(section_id):
            return s
    return None


def describe_error(e: Exception) -> str:
    # httpx timeouts stringify to "", so always lead with the type.
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def _set_locations(section: dict, locations: list[str]):
    # Plex replaces the whole location list on edit, so always send every path.
    params = [("agent", section["agent"]), ("language", section["language"]), ("name", section["title"])]
    params += [("location", p) for p in locations]
    try:
        # Plex walks the new folders before answering, which can take well over 15s.
        async with httpx.AsyncClient(timeout=60, headers=_headers()) as client:
            resp = await client.put(f"{PLEX_URL}/library/sections/{section['id']}", params=params)
            resp.raise_for_status()
    except httpx.TimeoutException:
        # Plex often finishes the edit after we stop waiting; trust what it reports now.
        current = await _get_section(section["id"])
        if not current or sorted(current["locations"]) != sorted(locations):
            raise


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


async def create_section(name: str, path: str, kind: str = "show", prefs: dict | None = None) -> int:
    agent, scanner = _KINDS[kind]
    params = [
        ("name", name), ("type", kind), ("agent", agent),
        ("scanner", scanner), ("language", "en-US"), ("location", path),
    ]
    params += [(f"prefs[{k}]", v) for k, v in (prefs or {}).items()]
    dirs = []
    try:
        async with httpx.AsyncClient(timeout=60, headers=_headers()) as client:
            resp = await client.post(f"{PLEX_URL}/library/sections", params=params)
            resp.raise_for_status()
        if resp.content:
            dirs = resp.json().get("MediaContainer", {}).get("Directory", [])
    except httpx.TimeoutException:
        pass  # Plex may still have created it; look it up by folder below
    if dirs:
        section_id = int(dirs[0]["key"])
    else:
        # Empty body or slow reply; find the new library by its folder.
        match = [s for s in await list_sections(kind) if path in s["locations"]]
        if not match:
            raise RuntimeError("Plex created the library but it could not be found")
        section_id = match[0]["id"]
    logger.info("Plex: created library %s (%s) at %s", section_id, name, path)
    return section_id


async def delete_section(section_id: int):
    # Plex can take well over 15s to drop a library (seen live), like it does to create one.
    async with httpx.AsyncClient(timeout=60, headers=_headers()) as client:
        resp = await client.delete(f"{PLEX_URL}/library/sections/{section_id}")
        resp.raise_for_status()
    logger.info("Plex: deleted library %s", section_id)
