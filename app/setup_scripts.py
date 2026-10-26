"""Setup tab: wizard answers, and the per-host scripts and agent routes they hand out.

Scripts are built from saved, validated answers (never from URL parameters). The routes under
/setup/<token>/ are open to the hosts running those scripts; the token is their only key."""
import json
import logging
import os
import re
import secrets
import shlex
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import config
import secret_box
from database import get_db

logger = logging.getLogger(__name__)

open_router = APIRouter(prefix="/setup")
router = APIRouter(prefix="/api/setup")

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_templates")
SCRIPTS = ("dispatcharr.sh", "media.sh", "dump_mappings.sh")

DUMP_TRIGGER_FILE = "/data/.dump-trigger"
# Uploaded by the Dispatcharr-host agent; category_mapping.json goes last because the
# Daily Run waits for its mtime to change (api._trigger_dump_regen).
DUMP_FILES = {
    "stream_mapping.json": os.environ.get("STREAM_MAPPING_FILE", "/data/stream_mapping.json"),
    "account_credentials.json": "/data/account_credentials.json",
    "series_category_mapping.json": os.environ.get("SERIES_CATEGORY_MAPPING_FILE",
                                                   "/data/series_category_mapping.json"),
    "movie_metadata.json": os.environ.get("MOVIE_METADATA_FILE", "/data/movie_metadata.json"),
    "category_mapping.json": os.environ.get("CATEGORY_MAPPING_FILE", "/data/category_mapping.json"),
}
MAX_DUMP_BYTES = 256 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024
RCLONE_MANAGER_PORT = 5573

KEY_TOKEN, KEY_ANSWERS = "SETUP_TOKEN", "SETUP_ANSWERS"
REPORT_KEYS = {"media": "SETUP_REPORT_MEDIA", "agent": "SETUP_REPORT_AGENT"}
# Media reports per machine id. SETUP_REPORT_MEDIA is the single report from before machines.
KEY_MEDIA_REPORTS = "SETUP_REPORTS_MEDIA"
KEY_LAST_DUMP = "SETUP_LAST_DUMP"

SERVERS = ("plex", "jellyfin", "emby")
MEDIA_OS = ("linux", "unraid", "windows", "macos", "nas")
MAX_MACHINES = 8
DEFAULT_MOUNTS = {
    "unraid": ("/mnt/addons/vod-bridge", "/mnt/addons/vod-bridge-series", "/mnt/addons/vod-bridge-movies"),
    "windows": ("V:\\", "W:\\", "X:\\"),
    "macos": ("/Users/Shared/vod-bridge", "/Users/Shared/vod-bridge-series", "/Users/Shared/vod-bridge-movies"),
}
DEFAULT_MACHINE = {
    "id": "", "label": "", "servers": ["plex"], "os": "linux", "docker": False, "container": "",
    "want_movies": True, "want_series": True,
    "movie_mount": "/mnt/vod-bridge", "series_mount": "/mnt/vod-bridge-series",
    "movie_cat_mount": "/mnt/vod-bridge-movies", "rclone_manager": False,
}
DEFAULT_ANSWERS = {"bridge_url": "", "dispatcharr_container": ""}
# Answers saved before machines existed, and the machine field each one became.
OLD_MACHINE_KEYS = {
    "media_os": "os", "media_docker": "docker", "media_container": "container",
    "want_movies": "want_movies", "want_series": "want_series", "movie_mount": "movie_mount",
    "series_mount": "series_mount", "rclone_manager": "rclone_manager",
}
OLD_SERVERS = {"plex": ["plex"], "jellyfin": ["jellyfin"], "both": ["plex", "jellyfin"]}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ID = re.compile(r"[a-z0-9]{1,16}")
_LABEL = re.compile(r"[A-Za-z0-9 ._()-]{0,40}")
_URL = re.compile(r"https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:\d{1,5})?/?")
_POSIX_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\([A-Za-z0-9._ -]+\\?)*")

# In memory only: the agent polls every minute, so this refills quickly after a restart.
_agent_seen = {"at": 0.0, "host": ""}


def default_answers() -> dict:
    return {**DEFAULT_ANSWERS, "machines": [{**DEFAULT_MACHINE, "id": "m1", "servers": ["plex"]}]}


def migrate_answers(raw: dict) -> dict:
    """Answers saved before machines -> one machine holding the old media-server answers."""
    if "machines" in raw:
        return raw
    machine = {**DEFAULT_MACHINE, "id": "m1", "servers": OLD_SERVERS.get(raw.get("media_server"), ["plex"])}
    for old, new in OLD_MACHINE_KEYS.items():
        if old in raw:
            machine[new] = raw[old]
    out = {k: raw[k] for k in DEFAULT_ANSWERS if k in raw}
    out["machines"] = [machine]
    return out


def _flag(v) -> bool:
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


def _machine_name(m: dict, n: int) -> str:
    return m["label"] or f"Machine {n}"


def validate_machine(body: dict, n: int) -> tuple[dict | None, str | None]:
    m = dict(DEFAULT_MACHINE)
    for k, default in DEFAULT_MACHINE.items():
        if k not in body:
            continue
        v = body[k]
        if isinstance(default, bool):
            m[k] = _flag(v)
        elif isinstance(default, list):
            m[k] = [x for x in SERVERS if x in (v if isinstance(v, list) else [])]
        else:
            m[k] = str(v or "").strip()
    if not _LABEL.fullmatch(m["label"]):
        return None, f"Machine {n}: names use letters, numbers, spaces, dots, dashes and brackets (up to 40)"
    name = _machine_name(m, n)
    if not m["servers"]:
        return None, f"{name}: tick Plex, Jellyfin or Emby"
    if m["container"] and not _NAME.fullmatch(m["container"]):
        return None, f"{name}: container names use letters, numbers, dots, dashes and underscores"
    if m["os"] not in MEDIA_OS:
        return None, f"{name}: pick the operating system"
    if not (m["want_movies"] or m["want_series"]):
        return None, f"{name}: pick movies, series or both"
    path = _WINDOWS_PATH if m["os"] == "windows" else _POSIX_PATH
    for k in ("movie_mount", "series_mount", "movie_cat_mount"):
        if not path.fullmatch(m[k]) or ".." in m[k]:
            example = "V:\\" if m["os"] == "windows" else "/mnt/vod-bridge"
            return None, f"{name}: mount folders look like {example}"
    if len({m[k].rstrip("/\\") for k in ("movie_mount", "series_mount", "movie_cat_mount")}) < 3:
        return None, f"{name}: movies, movie categories and series need different mount folders"
    return m, None


def validate_answers(body: dict) -> tuple[dict | None, str | None]:
    """Wizard form -> (clean answers, None) or (None, message)."""
    body = migrate_answers(body)
    a = {k: str(body.get(k) or "").strip() for k in DEFAULT_ANSWERS}
    url = a["bridge_url"].rstrip("/")
    if not _URL.fullmatch(url):
        return None, "Bridge address must look like http://192.168.1.20:8585"
    host = re.sub(r"^https?://|:\d+$", "", url).strip("[]").lower()
    if host in ("localhost", "0.0.0.0", "::1") or host.startswith("127."):
        return None, "Use the address other machines reach the bridge at, not localhost or 0.0.0.0"
    a["bridge_url"] = url
    if a["dispatcharr_container"] and not _NAME.fullmatch(a["dispatcharr_container"]):
        return None, "Container names use letters, numbers, dots, dashes and underscores"
    raw = body.get("machines")
    if not isinstance(raw, list) or not raw:
        return None, "Add the machine your media server runs on"
    if len(raw) > MAX_MACHINES:
        return None, f"Up to {MAX_MACHINES} media server machines"
    machines, ids = [], set()
    for n, item in enumerate(raw, 1):
        m, error = validate_machine(item if isinstance(item, dict) else {}, n)
        if error:
            return None, error
        # Ids name each machine's command and report, so they must stay stable and unique.
        if not _ID.fullmatch(m["id"]) or m["id"] in ids:
            m["id"] = next(f"m{i}" for i in range(1, 100) if f"m{i}" not in ids
                           and all(str(x.get("id")) != f"m{i}" for x in raw if isinstance(x, dict)))
        ids.add(m["id"])
        machines.append(m)
    # The bridge talks to one Plex server (Settings → Plex), so only one machine can run it.
    if sum("plex" in m["servers"] for m in machines) > 1:
        return None, "Only one machine can run Plex. The bridge connects to one Plex server."
    a["machines"] = machines
    return a, None


def plex_machine(answers: dict) -> dict | None:
    return next((m for m in answers["machines"] if "plex" in m["servers"]), None)


def find_machine(answers: dict, mid: str | None) -> dict | None:
    """The machine with this id; no id means the first, as commands from before machines were."""
    if not mid:
        return answers["machines"][0] if answers["machines"] else None
    return next((m for m in answers["machines"] if m["id"] == mid), None)


def script_header(answers: dict, token: str, machine: dict | None = None) -> str:
    """Shell variable lines rendered ahead of a script template; every value is quoted."""
    m = machine or answers["machines"][0]
    values = {
        "BRIDGE_URL": answers["bridge_url"], "SETUP_TOKEN": token,
        "DISPATCHARR_CONTAINER": answers["dispatcharr_container"],
        "MACHINE_ID": m["id"], "MEDIA_SERVERS": " ".join(m["servers"]), "MEDIA_OS": m["os"],
        "MEDIA_DOCKER": "1" if m["docker"] else "0",
        "MEDIA_CONTAINER": m["container"],
        "WANT_MOVIES": "1" if m["want_movies"] else "0",
        "WANT_SERIES": "1" if m["want_series"] else "0",
        "MOVIE_MOUNT": m["movie_mount"], "SERIES_MOUNT": m["series_mount"],
        "MOVIE_CAT_MOUNT": m["movie_cat_mount"],
        "RCLONE_MANAGER": "1" if m["rclone_manager"] else "0",
    }
    lines = ["#!/usr/bin/env bash", "# Generated by VOD Plex Bridge for this setup. Re-download it after changing the Setup tab."]
    lines += [f"{k}={shlex.quote(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def token_ok(given: str, expected: str) -> bool:
    return bool(expected) and secrets.compare_digest(given.encode(), expected.encode())


def dump_order(names) -> list[str]:
    """Known dump files in the order they are written; anything else is dropped."""
    return [n for n in DUMP_FILES if n in set(names)]


async def _get(key: str) -> str:
    db = await get_db()
    row = await (await db.execute("SELECT value FROM app_settings WHERE key=?", (key,))).fetchone()
    return row["value"] if row else ""


async def _put(key: str, value: str):
    db = await get_db()
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    await db.commit()


async def _token(create: bool = True) -> str:
    stored = await _get(KEY_TOKEN)
    if stored:
        try:
            return secret_box.decrypt(stored)
        except ValueError:
            logger.warning("Setup token could not be decrypted (settings key changed); making a new one")
    if not create:
        return ""
    token = secrets.token_urlsafe(32)
    await _put(KEY_TOKEN, secret_box.encrypt(token))
    return token


def _cat_mount_default(m: dict) -> dict:
    """Machines saved before movie categories get their system's default category folder."""
    if "movie_cat_mount" in m or m.get("os") not in DEFAULT_MOUNTS:
        return {}
    return {"movie_cat_mount": DEFAULT_MOUNTS[m["os"]][2]}


async def _answers() -> dict:
    try:
        raw = json.loads(await _get(KEY_ANSWERS) or "{}")
    except ValueError:
        raw = {}
    if not raw:
        return default_answers()
    raw = migrate_answers(raw)
    raw["machines"] = [{**DEFAULT_MACHINE, **_cat_mount_default(m), **m} for m in raw.get("machines") or []] \
        or default_answers()["machines"]
    return {**DEFAULT_ANSWERS, **raw}


async def _media_reports() -> dict:
    """Media reports by machine id; the report from before machines belongs to m1."""
    reports = await _json_setting(KEY_MEDIA_REPORTS)
    if isinstance(reports, dict):
        return reports
    old = await _json_setting(REPORT_KEYS["media"])
    return {"m1": old} if old else {}


async def _json_setting(key: str) -> dict | None:
    try:
        return json.loads(await _get(key) or "null")
    except ValueError:
        return None


async def _checked(token: str) -> bool:
    return token_ok(token, await _token(create=False))


def _forbidden():
    return JSONResponse({"error": "Unknown setup link; copy a fresh command from the Setup tab"}, status_code=403)


# ---- Routes the host scripts call (open; the token is the key) ----

@open_router.get("/{token}/media/{mid}.sh")
async def get_machine_script(token: str, mid: str):
    if not await _checked(token):
        return _forbidden()
    return await _render(token, "media.sh", mid)


@open_router.get("/{token}/{script}")
async def get_script(token: str, script: str, request: Request):
    if not await _checked(token):
        return _forbidden()
    if script == "dump-trigger":
        return await _dump_trigger(request)
    if script not in SCRIPTS:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if script == "dump_mappings.sh":
        with open(os.path.join(TEMPLATE_DIR, script), encoding="utf-8") as f:
            return PlainTextResponse(f.read(), media_type="text/plain")
    # A plain media.sh is the first machine's, so commands copied before machines still work.
    return await _render(token, script, None)


async def _render(token: str, script: str, mid: str | None):
    answers = await _answers()
    if not answers["bridge_url"]:
        return PlainTextResponse("echo 'Finish the Setup tab in the bridge first' >&2; exit 1\n",
                                 media_type="text/plain")
    machine = find_machine(answers, mid)
    if not machine:
        return PlainTextResponse("echo 'That machine is no longer on the Setup tab; copy its command again' >&2; exit 1\n",
                                 media_type="text/plain")
    with open(os.path.join(TEMPLATE_DIR, script), encoding="utf-8") as f:
        body = f.read()
    # Braces make bash read the whole script before running any of it, so a cut-off download
    # runs nothing and an early exit (--uninstall) doesn't leave curl writing to a closed pipe.
    shebang, rest = (script_header(answers, token, machine) + body).split("\n", 1)
    return PlainTextResponse(f"{shebang}\n{{\n{rest}\n}}\n", media_type="text/plain")


async def _dump_trigger(request: Request):
    _agent_seen.update(at=time.time(), host=request.client.host if request.client else "")
    # Agents from before this check send nothing, which leaves the install-time note alone.
    old_cron = request.query_params.get("old_cron")
    report = await _json_setting(REPORT_KEYS["agent"])
    if old_cron in ("0", "1") and report and bool(report.get("old_cron")) != (old_cron == "1"):
        report["old_cron"] = old_cron == "1"
        await _put(REPORT_KEYS["agent"], json.dumps(report))
    pending = os.path.exists(DUMP_TRIGGER_FILE)
    if pending:
        try:
            os.remove(DUMP_TRIGGER_FILE)
        except OSError:
            pass
    return {"pending": pending}


@open_router.post("/{token}/dump")
async def upload_dump(token: str, request: Request):
    if not await _checked(token):
        return _forbidden()
    form = await request.form(max_files=len(DUMP_FILES), max_fields=len(DUMP_FILES))
    parts = {name: form[name] for name in form if hasattr(form[name], "read")}
    try:
        staged = []
        for name in dump_order(parts):
            data = await parts[name].read(MAX_DUMP_BYTES + 1)
            if len(data) > MAX_DUMP_BYTES:
                return JSONResponse({"error": f"{name} is too large"}, status_code=413)
            try:
                json.loads(data)
            except ValueError:
                return JSONResponse({"error": f"{name} is not valid JSON"}, status_code=400)
            staged.append((name, data))
        if not staged:
            return JSONResponse({"error": "No dump files in the upload"}, status_code=400)
        for name, data in staged:
            path = DUMP_FILES[name]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.upload"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
    finally:
        await form.close()
    names = [n for n, _ in staged]
    await _put(KEY_LAST_DUMP, json.dumps({"at": time.time(), "files": names}))
    logger.info("Setup agent uploaded dump files: %s", ", ".join(names))
    return {"ok": True, "files": names}


@open_router.post("/{token}/report")
async def post_report(token: str, request: Request):
    if not await _checked(token):
        return _forbidden()
    raw = await request.body()
    if len(raw) > MAX_REPORT_BYTES:
        return JSONResponse({"error": "Report is too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "Report is not valid JSON"}, status_code=400)
    if not isinstance(body, dict) or body.get("kind") not in REPORT_KEYS:
        return JSONResponse({"error": "Unknown report kind"}, status_code=400)
    body["received_at"] = time.time()
    body["from"] = request.client.host if request.client else ""
    if body["kind"] == "media":
        # Scripts from before machines send no id; they were the first machine's.
        mid = str(body.get("machine") or "m1")
        if not _ID.fullmatch(mid):
            return JSONResponse({"error": "Unknown machine"}, status_code=400)
        reports = await _media_reports()
        reports[mid] = body
        await _put(KEY_MEDIA_REPORTS, json.dumps(reports))
        return {"ok": True}
    await _put(REPORT_KEYS[body["kind"]], json.dumps(body))
    return {"ok": True}


# ---- Routes the Setup tab calls (behind the login) ----

def _usable_host(host: str) -> bool:
    return bool(host) and host not in ("0.0.0.0", "localhost") and not host.startswith("127.")


def _default_bridge_url(request: Request) -> str:
    # The address the browser used is the one known to work right now. BRIDGE_HOST
    # can be stale after the bridge moves to another machine, so it is only a fallback.
    host = request.url.hostname or ""
    if _usable_host(host):
        return f"{request.url.scheme}://{request.url.netloc}"
    if _usable_host(config.BRIDGE_HOST):
        return f"http://{config.BRIDGE_HOST}:{config.BRIDGE_PORT}"
    return ""


@router.get("/state")
async def get_state(request: Request):
    answers = await _answers()
    if not answers["bridge_url"]:
        answers["bridge_url"] = _default_bridge_url(request)
    token = await _token()
    base = f"{answers['bridge_url']}/setup/{token}" if answers["bridge_url"] else ""
    reports = await _media_reports()
    machines = []
    for n, m in enumerate(answers["machines"], 1):
        report = reports.get(m["id"])
        rclone_manager = ""
        if report and report.get("rclone_manager") and report.get("from"):
            rclone_manager = f"http://{report['from']}:{RCLONE_MANAGER_PORT}"
        machines.append({
            "id": m["id"], "name": _machine_name(m, n), "report": report, "rclone_manager_url": rclone_manager,
            "command": f"curl -fsSL {base}/media/{m['id']}.sh | sudo bash" if base else "",
        })
    return {
        "answers": answers,
        "saved": bool(await _get(KEY_ANSWERS)),
        "default_mounts": DEFAULT_MOUNTS,
        "max_machines": MAX_MACHINES,
        "commands": {
            "dispatcharr": f"curl -fsSL {base}/dispatcharr.sh | sudo bash" if base else "",
            "media": machines[0]["command"] if machines else "",
        },
        "agent_seen_at": _agent_seen["at"] or None,
        "last_dump": await _json_setting(KEY_LAST_DUMP),
        "agent_report": await _json_setting(REPORT_KEYS["agent"]),
        "machines": machines,
        "dispatcharr": {"configured": bool(config.DISPATCHARR_API_KEY), "url": config.DISPATCHARR_URL},
        "plex": {"configured": bool(config.PLEX_URL and config.PLEX_TOKEN), "url": config.PLEX_URL,
                 "movie_library_id": config.PLEX_LIBRARY_ID,
                 "series_library_id": config.PLEX_SERIES_LIBRARY_ID},
    }


@router.post("/answers")
async def save_answers(request: Request):
    answers, error = validate_answers(await request.json())
    if error:
        return JSONResponse({"error": error}, status_code=400)
    await _put(KEY_ANSWERS, json.dumps(answers))
    reports = await _media_reports()
    ids = {m["id"] for m in answers["machines"]}
    await _put(KEY_MEDIA_REPORTS, json.dumps({k: v for k, v in reports.items() if k in ids}))
    # Library folders are the paths Plex sees, so they come from the Plex machine.
    plex = plex_machine(answers)
    if plex:
        mounts = {"PLEX_MOVIE_MOUNT_ROOT": plex["movie_mount"], "PLEX_SERIES_MOUNT_ROOT": plex["series_mount"],
                  "PLEX_MOVIE_CATEGORY_MOUNT_ROOT": plex["movie_cat_mount"]}
        for k, v in mounts.items():
            await _put(k, v)
        config.apply_settings(mounts)
    return await get_state(request)


@router.post("/token/regenerate")
async def regenerate_token(request: Request):
    db = await get_db()
    await db.execute("DELETE FROM app_settings WHERE key=?", (KEY_TOKEN,))
    await db.commit()
    return await get_state(request)


PLEX_PLATFORMS = {"linux": "linux", "windows": "windows", "macosx": "macos"}


@router.get("/plex-detect")
async def plex_detect():
    """Plex's own platform, so the wizard can pre-answer the OS question."""
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        return {"ok": False}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(config.PLEX_URL.rstrip("/") + "/",
                                    headers={"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"})
        info = resp.json().get("MediaContainer", {})
    except (httpx.HTTPError, ValueError):
        return {"ok": False}
    platform = str(info.get("platform") or "")
    return {"ok": True, "platform": platform, "os": PLEX_PLATFORMS.get(platform.lower(), ""),
            "host": httpx.URL(config.PLEX_URL).host}


@router.post("/plex-movie-library")
async def create_movie_library(request: Request):
    import plex_libraries
    if not plex_libraries.configured():
        return JSONResponse({"error": "Save the Plex URL and token in Settings first"}, status_code=400)
    name = str((await request.json()).get("name") or "").strip() or "Bridge Movies"
    try:
        section_id = await plex_libraries.create_section(
            name, config.PLEX_MOVIE_MOUNT_ROOT, kind="movie", prefs=plex_libraries.MOVIE_PREFS)
    except Exception as e:
        return JSONResponse({"error": f"Plex could not create the library: {plex_libraries.describe_error(e)}"},
                            status_code=502)
    await _put("PLEX_LIBRARY_ID", str(section_id))
    config.apply_settings({"PLEX_LIBRARY_ID": str(section_id)})
    return {"ok": True, "id": section_id, "name": name}
