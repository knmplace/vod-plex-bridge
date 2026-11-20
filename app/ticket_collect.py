"""Gathers everything we'd look at if we were logged in to the user's bridge, as one text report.

Returns the raw report plus the secret values the bridge knows (for Scrubber). Each section is
independent: one that fails says "unavailable: reason" instead of failing the whole report. Only the
bridge's own data, the Plex API and the Dispatcharr API are read; no provider is contacted.
"""
import json
import os
import platform
import re
import sys
import time
from urllib.parse import urlparse

import httpx

import config
import log_ring

TIMEOUT = 10
MAX_BYTES = 1_500_000
PLEX_PREF_RE = re.compile(r"scan|analy|thumbnail|intro|credit|loudness|butler|chapter|generate", re.I)
_STARTED = time.time()


def _add(secrets: dict, kind: str, *values):
    for v in values:
        if v and str(v).strip():
            secrets.setdefault(kind, []).append(str(v).strip())


def _host(url: str) -> str:
    try:
        return urlparse(url if "//" in url else "//" + url).hostname or ""
    except ValueError:
        return ""


def _cap(lines: list[str], budget: int) -> list[str]:
    """Drop oldest lines until the text fits, noting how many went."""
    total, keep = 0, []
    for line in reversed(lines):
        total += len(line) + 1
        if total > budget:
            break
        keep.append(line)
    keep.reverse()
    dropped = len(lines) - len(keep)
    return ([f"[truncated {dropped} older lines]"] if dropped else []) + keep


def _app_version() -> str:
    mod = sys.modules.get("main")
    if mod is not None and hasattr(mod, "APP_VERSION"):
        return mod.APP_VERSION
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8") as f:
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read())
    return m.group(1) if m else "unknown"


async def _app(secrets: dict) -> str:
    APP_VERSION = _app_version()
    out = [f"version: {APP_VERSION}", f"uptime_seconds: {int(time.time() - _STARTED)}",
           f"python: {sys.version.split()[0]}  os: {platform.platform()}", "settings:"]
    for k in config.SETTINGS_KEYS:
        v = getattr(config, "REDIRECT_STREAMING" if k == "REDIRECT_MODE" else k, "")
        if k in config.SECRET_KEYS:
            _add(secrets, "token" if "TOKEN" in k else "api_key", v)
            out.append(f"  {k}: {'set' if v else 'unset'}")
        else:
            if k.endswith("_URL"):
                _add(secrets, "host", _host(str(v)))
            out.append(f"  {k}: {v}")
    from database import get_db
    db = await get_db()
    row = await (await db.execute("SELECT * FROM sync_state WHERE id = 1")).fetchone()
    if row:
        d = dict(row)
        out.append(f"daily_run: interval_hours={d.get('refresh_interval_hours')} last={d.get('last_scheduled_refresh')}")
        out.append("last_daily_run_report:\n" + str(d.get("last_refresh_report") or "(none)"))
    return "\n".join(out)


async def _counts(secrets: dict) -> str:
    from database import get_db
    db = await get_db()

    async def one(sql):
        return (await (await db.execute(sql)).fetchone())[0]
    out = [
        f"movies total={await one('SELECT COUNT(*) FROM movies')} "
        f"activated={await one('SELECT COUNT(*) FROM movies WHERE activated=1')} "
        f"dead={await one('SELECT COUNT(*) FROM movies WHERE dead=1')}",
        f"series={await one('SELECT COUNT(*) FROM series')} episodes={await one('SELECT COUNT(*) FROM episodes')}",
        f"activation_queue={await one('SELECT COUNT(*) FROM activation_queue')} "
        f"series_activation_queue={await one('SELECT COUNT(*) FROM series_activation_queue')}",
    ]
    r = await (await db.execute("SELECT kind, COUNT(*) c, COALESCE(SUM(LENGTH(data)),0) b FROM learned_ranges GROUP BY kind")).fetchall()
    out.append("learned_ranges: " + (", ".join(f"{x['kind']}: {x['c']} rows {x['b']} bytes" for x in r) or "none"))
    ev = await (await db.execute("SELECT * FROM series_watch_events ORDER BY id DESC LIMIT 30")).fetchall()
    out.append("series_watch_events (newest 30):")
    out += ["  " + json.dumps(dict(e), default=str) for e in ev]
    for a in await (await db.execute("SELECT id, name FROM m3u_accounts")).fetchall():
        _add(secrets, "account", a["name"])
    return "\n".join(out)


async def _streaming(secrets: dict) -> tuple[str, list[str]]:
    import proxy
    head = [f"MAX_CONCURRENT_PIPES: {proxy.MAX_CONCURRENT_PIPES}",
            f"circuit_breaker_tripped_streams: {sorted(proxy._failure_tracker)}",
            f"active_pipes: {json.dumps(proxy.get_all_pipes(), default=str)}",
            "archives: " + json.dumps([{k: a[k] for k in ('filename', 'entries')} for a in proxy.list_log_archives()[:5]])]
    # The in-memory log resets on restart; the 4-hourly archives bridge that. Oldest first, de-duplicated
    # (each archive is a snapshot of the same rolling log), then the live log.
    seen, log = set(), []
    entries = []
    for a in reversed(proxy.list_log_archives()[:8]):
        try:
            entries += proxy.get_log_archive(a["filename"]) or []
        except Exception:
            pass
    entries += proxy.get_proxy_log()
    for e in entries:
        line = json.dumps(e, default=str, sort_keys=True)
        if line not in seen:
            seen.add(line)
            log.append(line)
    return "\n".join(head), log[-4000:]


async def _library(secrets: dict) -> str:
    """What is activated, dead, queued and learned: the state we'd look at in the Browse/Dead tabs."""
    from database import get_db
    db = await get_db()
    out = []

    async def rows(title, sql, limit=None):
        try:
            r = await (await db.execute(sql)).fetchall()
        except Exception as e:
            out.append(f"{title}: unavailable: {type(e).__name__}: {e}")
            return
        out.append(f"{title} ({len(r)}):")
        out.extend("  " + json.dumps(dict(x), default=str) for x in r)
    await rows("activated movies", "SELECT id, name, year, category_id, stream_id, account_id, dead, stream_dead, "
               "strm_folder, in_plex FROM movies WHERE activated=1 ORDER BY id DESC LIMIT 200")
    await rows("dead movies", "SELECT id, name, year, stream_id, account_id, dead, stream_dead, catalog_refresh_note "
               "FROM movies WHERE dead=1 OR stream_dead=1 ORDER BY id DESC LIMIT 100")
    await rows("activation queue", "SELECT * FROM activation_queue ORDER BY queued_at DESC LIMIT 100")
    await rows("series activation queue", "SELECT * FROM series_activation_queue ORDER BY rowid DESC LIMIT 100")
    await rows("activated series (episodes activated)", "SELECT s.id, s.name, s.year, COUNT(*) AS episodes "
               "FROM episodes e JOIN series s ON s.id=e.series_id WHERE e.activated=1 GROUP BY s.id LIMIT 100")
    await rows("series watch prefs", "SELECT * FROM series_watch_prefs LIMIT 100")
    await rows("learned ranges, movies (what Plex read; bytes = stored)",
               "SELECT l.item_id, m.name, m.year, l.start, LENGTH(l.data) AS bytes, l.created_at, "
               "(l.sig LIKE m.uuid || ':%') AS sig_matches_current_file FROM learned_ranges l "
               "LEFT JOIN movies m ON m.id=l.item_id WHERE l.kind='movie' ORDER BY l.created_at DESC LIMIT 200")
    await rows("learned ranges, episodes",
               "SELECT l.item_id, s.name AS series, e.season_number, e.episode_number, l.start, "
               "LENGTH(l.data) AS bytes, l.created_at FROM learned_ranges l "
               "LEFT JOIN episodes e ON e.id=l.item_id LEFT JOIN series s ON s.id=e.series_id "
               "WHERE l.kind='episode' ORDER BY l.created_at DESC LIMIT 200")
    try:
        import range_learn
        out.append("range learning in-memory state: " + json.dumps(range_learn.snapshot(), default=str))
    except Exception as e:
        out.append(f"range learning state: unavailable: {type(e).__name__}: {e}")
    await rows("user movie categories", "SELECT id, name, strm_folder, plex_library_section FROM user_movie_categories")
    return "\n".join(out)


async def _system(secrets: dict) -> str:
    import shutil
    from database import DB_PATH
    out = []
    for label, path in (("data volume", "/data"), ("movies output", config.STRM_OUTPUT_DIR)):
        try:
            u = shutil.disk_usage(path)
            out.append(f"{label} {path}: total={u.total >> 20}MB used={u.used >> 20}MB free={u.free >> 20}MB")
        except Exception as e:
            out.append(f"{label}: unavailable: {type(e).__name__}: {e}")
    try:
        out.append(f"database: {os.path.getsize(DB_PATH) >> 20}MB")
    except OSError as e:
        out.append(f"database: unavailable: {e}")
    try:
        import proxy
        files = [os.path.join(proxy.BUFFER_DIR, f) for f in os.listdir(proxy.BUFFER_DIR)]
        out.append(f"buffers: {len(files)} files, {sum(os.path.getsize(f) for f in files) >> 20}MB")
    except Exception as e:
        out.append(f"buffers: unavailable: {type(e).__name__}: {e}")
    try:
        out.append(f"load average: {os.getloadavg()}  cpus: {os.cpu_count()}")
        with open("/proc/meminfo") as f:
            out.append("memory: " + ", ".join(l.strip() for l in f.readlines()[:3]))
    except Exception:
        pass
    keys = ("TZ", "PUID", "PGID", "STRM_OUTPUT_DIR", "LOG_LEVEL")
    out.append("env: " + ", ".join(f"{k}={os.environ[k]}" for k in keys if k in os.environ))
    return "\n".join(out)


async def _health(secrets: dict) -> str:
    from health import get_health_log, get_health_status
    out = ["status: " + json.dumps(get_health_status(), default=str)[:6000], "log (newest last):"]
    out += ["  " + json.dumps(e, default=str) for e in get_health_log()[-150:]]
    return "\n".join(out)


async def _media_servers(secrets: dict) -> str:
    """Jellyfin/Emby sessions and system info, when configured."""
    out = []
    for label, url, key in (("jellyfin", config.JELLYFIN_URL, config.JELLYFIN_API_KEY),
                            ("emby", config.EMBY_URL, config.EMBY_API_KEY)):
        if not (url and key):
            out.append(f"{label}: not configured")
            continue
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"X-Emby-Token": key}) as c:
            for path in ("/System/Info", "/Sessions", "/ScheduledTasks"):
                try:
                    r = await c.get(url.rstrip("/") + path)
                    out.append(f"{label} {path}: HTTP {r.status_code} {r.text[:20000]}")
                except Exception as e:
                    out.append(f"{label} {path}: unavailable: {type(e).__name__}: {e}")
    return "\n".join(out)


async def _mounts(secrets: dict) -> str:
    import plex_mounts
    return json.dumps(plex_mounts.status(), indent=1, default=str)


async def _plex(secrets: dict) -> str:
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        return "unavailable: Plex URL/token not set in Settings"
    out = []
    headers = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as c:
        async def get(path, **kw):
            try:
                r = await c.get(config.PLEX_URL + path, **kw)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                out.append(f"{path}: unavailable: {type(e).__name__}: {e}")
        root = await get("/")
        if root:
            mc = root.get("MediaContainer", {})
            out.append(f"plex version: {mc.get('version')} platform: {mc.get('platform')}")
        for path in ("/identity", "/transcode/sessions", "/status/sessions", "/activities"):
            d = await get(path)
            if d is not None:
                out.append(f"{path}:\n" + json.dumps(d.get("MediaContainer", d), indent=1)[:60000])
        d = await get("/status/sessions/history/all", params={"sort": "viewedAt:desc"},
                      headers={"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "50"})
        if d is not None:
            out.append("recent play history (newest first):")
            for h in (d.get("MediaContainer", {}).get("Metadata", []) or [])[:50]:
                out.append(f"  viewedAt={h.get('viewedAt')} type={h.get('type')} "
                           f"title={h.get('grandparentTitle', '')} {h.get('title')} accountID={h.get('accountID')}")
        d = await get("/library/sections")
        if d is not None:
            for s in d.get("MediaContainer", {}).get("Directory", []) or []:
                out.append(f"library {s.get('key')} '{s.get('title')}' type={s.get('type')} "
                           f"paths={[l.get('path') for l in s.get('Location', [])]} scanner={s.get('scanner')} agent={s.get('agent')}")
        d = await get("/:/prefs")
        if d is not None:
            out.append("plex prefs:")
            for p in d.get("MediaContainer", {}).get("Setting", []) or []:
                if PLEX_PREF_RE.search(p.get("id", "")):
                    out.append(f"  {p.get('id')} = {p.get('value')}")
    return "\n".join(out)


async def _dispatcharr(secrets: dict) -> str:
    headers = {"X-API-Key": config.DISPATCHARR_API_KEY} if config.DISPATCHARR_API_KEY else {}
    base = config.DISPATCHARR_URL
    out = []
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as c:
        for label, path in (("version", "/api/core/version/"), ("vod proxy stats", "/proxy/vod/stats/")):
            try:
                r = await c.get(base + path)
                out.append(f"{label}: HTTP {r.status_code} {r.text[:4000]}")
            except Exception as e:
                out.append(f"{label}: unavailable: {type(e).__name__}: {e}")
        try:
            r = await c.get(base + "/api/m3u/accounts/")
            data = r.json()
            for a in (data.get("results", data) if isinstance(data, dict) else data):
                _add(secrets, "account", a.get("name"), a.get("username"))
                _add(secrets, "host", _host(a.get("server_url") or ""))
                out.append(f"m3u account {a.get('id')}: name={a.get('name')} status={a.get('status')} "
                           f"max_streams={a.get('max_streams')} active={a.get('is_active')}")
        except Exception as e:
            out.append(f"m3u accounts: unavailable: {type(e).__name__}: {e}")
    return "\n".join(out)


def _known_secrets() -> dict:
    s: dict = {}
    _add(s, "token", config.PLEX_TOKEN, config.TMDB_READ_TOKEN)
    _add(s, "api_key", config.TMDB_API_KEY, config.JELLYFIN_API_KEY, config.EMBY_API_KEY, config.DISPATCHARR_API_KEY)
    _add(s, "password", config.DISPATCHARR_XC_PASSWORD)
    _add(s, "account", config.DISPATCHARR_XC_USERNAME)
    for u in (config.PLEX_URL, config.JELLYFIN_URL, config.EMBY_URL, config.DISPATCHARR_URL):
        _add(s, "host", _host(u))
    return s


def _more_secrets(secrets: dict):
    """Instances, provider accounts and the admin hash from the database."""
    import sqlite3
    import secret_box
    from database import DB_PATH
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for url, key, xcu, xcp in conn.execute("SELECT url, api_key, xc_username, xc_password FROM dispatcharr_instances"):
                _add(secrets, "host", _host(url or ""))
                _add(secrets, "account", xcu)
                for kind, v in (("api_key", key), ("password", xcp)):
                    try:
                        _add(secrets, kind, secret_box.decrypt(v) if v else "")
                    except Exception:
                        pass
            for name, url, user, pw in conn.execute("SELECT name, base_url, username, password FROM providers"):
                _add(secrets, "provider", name)
                _add(secrets, "host", _host(url or ""))
                _add(secrets, "account", user)
                try:
                    _add(secrets, "password", secret_box.decrypt(pw) if pw else "")
                except Exception:
                    pass
            for (h,) in conn.execute("SELECT password_hash FROM app_user"):
                _add(secrets, "password", h)
    except sqlite3.Error:
        pass


async def collect_report() -> tuple[str, dict]:
    secrets = _known_secrets()
    parts: list[tuple[str, str]] = []

    async def section(title, fn):
        try:
            text = await fn(secrets)
        except Exception as e:
            text = f"unavailable: {type(e).__name__}: {e}"
        parts.append((title, text))

    await section("App", _app)
    await section("Database counts", _counts)
    log_lines: list[str] = []
    proxy_lines: list[str] = []

    async def _stream(s):
        nonlocal proxy_lines
        head, proxy_lines = await _streaming(s)
        return head
    await section("Streaming", _stream)
    await section("Mounts", _mounts)
    await section("System", _system)
    await section("Health", _health)
    await section("Library state", _library)
    await section("Plex", _plex)
    await section("Jellyfin / Emby", _media_servers)
    await section("Dispatcharr", _dispatcharr)
    try:
        log_lines = log_ring.tail()
    except Exception as e:
        log_lines = [f"unavailable: {type(e).__name__}: {e}"]
    _more_secrets(secrets)

    fixed = "".join(f"=== {t} ===\n{x}\n\n" for t, x in parts)
    budget = max(MAX_BYTES - len(fixed) - 200, 20000) // 2
    fixed += "=== Proxy log (newest last) ===\n" + "\n".join(_cap(proxy_lines, budget)) + "\n\n"
    fixed += "=== Container log tail ===\n" + "\n".join(_cap(log_lines, budget)) + "\n"
    return fixed, secrets
