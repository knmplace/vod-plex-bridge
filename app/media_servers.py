"""Jellyfin and Emby: connection test, health check and now-playing sessions.
Both speak the same API, so one client serves either; sessions come out in the same shape as Plex's."""
import ipaddress
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)

SERVERS = {"jellyfin": "Jellyfin", "emby": "Emby"}


def configured() -> list[str]:
    """The servers with both a URL and an API key set."""
    return [s for s in SERVERS if _conf(s)[0] and _conf(s)[1]]


def _conf(server: str) -> tuple[str, str]:
    key = server.upper()
    return getattr(config, f"{key}_URL", ""), getattr(config, f"{key}_API_KEY", "")


def _headers(api_key: str) -> dict:
    return {"X-Emby-Token": api_key, "Authorization": f'MediaBrowser Token="{api_key}"',
            "Accept": "application/json"}


async def get(client: httpx.AsyncClient, server: str, path: str, url: str = "", api_key: str = ""):
    """GET a server path, using the saved URL/key unless given (the Settings form tests unsaved values)."""
    if not url:
        url, api_key = _conf(server)
    return await client.get(f"{url.rstrip('/')}{path}", headers=_headers(api_key))


async def test(client: httpx.AsyncClient, server: str, url: str, api_key: str) -> dict:
    name = SERVERS[server]
    if not url or not api_key:
        return {"ok": False, "message": f"{name}: enter the URL and API key first"}
    resp = await get(client, server, "/System/Info", url, api_key)
    if resp.status_code in (401, 403):
        return {"ok": False, "message": f"{name} refused the API key"}
    if resp.status_code != 200:
        return {"ok": False, "message": f"{name} answered HTTP {resp.status_code}"}
    info = resp.json()
    libraries = []
    folders = await get(client, server, "/Library/VirtualFolders", url, api_key)
    if folders.status_code == 200:
        libraries = [{"id": f.get("ItemId", ""), "title": f"{name}: {f.get('Name', '')}",
                      "type": f.get("CollectionType") or "mixed"} for f in folders.json()]
    return {"ok": True, "libraries": libraries,
            "message": f"Connected to {info.get('ServerName') or name} ({info.get('ProductName') or name} {info.get('Version', '')})".strip()}


def _resolution(height: int) -> str:
    if not height:
        return ""
    if height >= 1500:
        return "4k"
    if height >= 900:
        return "1080"
    if height >= 600:
        return "720"
    return "sd"


def _is_local(endpoint: str) -> bool:
    host = endpoint or ""
    if host.count(":") == 1:  # ipv4:port
        host = host.split(":")[0]
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback


def _session(s: dict, server: str) -> dict:
    item = s.get("NowPlayingItem") or {}
    play = s.get("PlayState") or {}
    trans = s.get("TranscodingInfo") or {}
    streams = item.get("MediaStreams") or []
    video = next((m for m in streams if m.get("Type") == "Video"), {})
    audio = next((m for m in streams if m.get("Type") == "Audio" and m.get("IsDefault")), None) \
        or next((m for m in streams if m.get("Type") == "Audio"), {})
    duration_ms = int(item.get("RunTimeTicks") or 0) // 10000
    view_offset_ms = int(play.get("PositionTicks") or 0) // 10000
    title = item.get("Name", "Unknown")
    if item.get("Type") == "Episode" and item.get("SeriesName"):
        title = f"{item['SeriesName']} - S{item.get('ParentIndexNumber') or 0:02d}E{item.get('IndexNumber') or 0:02d} - {title}"
    method = play.get("PlayMethod", "")
    transcoding = method == "Transcode"
    path = item.get("Path", "") or ""
    return {
        "server": server,
        "title": title,
        "year": item.get("ProductionYear"),
        "type": {"Episode": "episode", "Audio": "track"}.get(item.get("Type"), "movie"),
        "thumb": "",
        "state": "paused" if play.get("IsPaused") else "playing",
        "player_title": s.get("DeviceName") or s.get("Client") or "Unknown Player",
        "player_device": s.get("DeviceName", ""),
        "player_platform": s.get("Client", ""),
        "player_local": _is_local(s.get("RemoteEndPoint", "")),
        "player_address": s.get("RemoteEndPoint", ""),
        "user_title": s.get("UserName", "Unknown"),
        "duration_ms": duration_ms,
        "view_offset_ms": view_offset_ms,
        "progress_pct": round(view_offset_ms / duration_ms * 100, 1) if duration_ms > 0 else 0,
        "video_codec": video.get("Codec", ""),
        "video_resolution": _resolution(int(video.get("Height") or 0)),
        "audio_codec": audio.get("Codec", ""),
        "audio_channels": audio.get("Channels", 0),
        "video_decision": "transcode" if transcoding and not trans.get("IsVideoDirect") else ("directplay" if method == "DirectPlay" else "copy"),
        "audio_decision": "transcode" if transcoding and not trans.get("IsAudioDirect") else ("directplay" if method == "DirectPlay" else "copy"),
        "bandwidth_kbps": int(trans.get("Bitrate") or 0) // 1000,
        "is_bridge": "vod-bridge" in path or "vod_bridge" in path,
        "file_path": path,
        "container": item.get("Container", ""),
        "bitrate_kbps": int(video.get("BitRate") or 0) // 1000,
    }


async def sessions(client: httpx.AsyncClient, server: str) -> list[dict]:
    """What is playing right now; raises httpx errors and ValueError on a bad answer."""
    resp = await get(client, server, "/Sessions")
    if resp.status_code != 200:
        raise ValueError(f"{SERVERS[server]} returned {resp.status_code}")
    return [_session(s, server) for s in resp.json() if s.get("NowPlayingItem")]


async def check_health(server: str) -> tuple[str, float | None]:
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await get(client, server, "/System/Info")
        elapsed = time.time() - start
        if resp.status_code == 200:
            return ("ok" if elapsed < 3 else "slow"), elapsed
        logger.warning("%s health check failed: HTTP %s", SERVERS[server], resp.status_code)
        return "down", elapsed
    except Exception as e:
        logger.warning("%s health check failed: %s", SERVERS[server], e)
        return "down", None
