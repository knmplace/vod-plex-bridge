import os

DISPATCHARR_URL = os.environ.get("DISPATCHARR_URL", "http://localhost:9191")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_READ_TOKEN = os.environ.get("TMDB_READ_TOKEN", "")
STRM_OUTPUT_DIR = os.environ.get("STRM_OUTPUT_DIR", "/plex-vod/Movies")
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8585"))
DISPATCHARR_API_KEY = os.environ.get("DISPATCHARR_API_KEY", "")

PLEX_URL = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_LIBRARY_ID = os.environ.get("PLEX_LIBRARY_ID", "7")

# Watched for now-playing sessions and health only; the bridge never changes their libraries.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
EMBY_URL = os.environ.get("EMBY_URL", "").rstrip("/")
EMBY_API_KEY = os.environ.get("EMBY_API_KEY", "")

MAX_CONCURRENT_PER_TITLE = int(os.environ.get("MAX_CONCURRENT_PER_TITLE", "2"))
REDIRECT_STREAMING = os.environ.get("REDIRECT_MODE", "false").lower() in ("1", "true", "yes")

STRM_SERIES_OUTPUT_DIR = os.environ.get("STRM_SERIES_OUTPUT_DIR", "/plex-vod/Series")
PLEX_SERIES_LIBRARY_ID = os.environ.get("PLEX_SERIES_LIBRARY_ID", "")
# Where the series folders appear on the Plex host (the rclone mount of /vod-series/).
PLEX_SERIES_MOUNT_ROOT = os.environ.get("PLEX_SERIES_MOUNT_ROOT", "/mnt/vod-bridge-series").rstrip("/")
PLEX_MOVIE_MOUNT_ROOT = os.environ.get("PLEX_MOVIE_MOUNT_ROOT", "/mnt/vod-bridge").rstrip("/")
# Where the movie category folders appear on the Plex host (the rclone mount of /vod-movies/).
PLEX_MOVIE_CATEGORY_MOUNT_ROOT = os.environ.get("PLEX_MOVIE_CATEGORY_MOUNT_ROOT", "/mnt/vod-bridge-movies").rstrip("/")

DISPATCHARR_XC_USERNAME = os.environ.get("DISPATCHARR_XC_USERNAME", "")
DISPATCHARR_XC_PASSWORD = os.environ.get("DISPATCHARR_XC_PASSWORD", "")

# Set from the primary Dispatcharr instance (Settings > Dispatcharr, see sources.py);
# the environment is only used while no instance exists.
INSTANCE_KEYS = ("DISPATCHARR_URL", "DISPATCHARR_API_KEY", "DISPATCHARR_XC_USERNAME", "DISPATCHARR_XC_PASSWORD")
INSTANCE_ENV = {k: os.environ.get(k, "") for k in INSTANCE_KEYS}
INSTANCE_ENV["DISPATCHARR_URL"] = os.environ.get("DISPATCHARR_URL", "http://localhost:9191")

# Values saved from the Settings tab win over the compose environment above.
SETTINGS_GROUPS = {
    "plex": ("PLEX_URL", "PLEX_TOKEN", "PLEX_LIBRARY_ID", "PLEX_SERIES_LIBRARY_ID",
             "PLEX_MOVIE_MOUNT_ROOT", "PLEX_SERIES_MOUNT_ROOT", "PLEX_MOVIE_CATEGORY_MOUNT_ROOT"),
    "media": ("JELLYFIN_URL", "JELLYFIN_API_KEY", "EMBY_URL", "EMBY_API_KEY"),
    "tmdb": ("TMDB_API_KEY", "TMDB_READ_TOKEN"),
    "streaming": ("MAX_CONCURRENT_PER_TITLE", "REDIRECT_MODE"),
}
SETTINGS_KEYS = tuple(k for keys in SETTINGS_GROUPS.values() for k in keys)
# Stored encrypted and never sent back to the browser.
SECRET_KEYS = ("PLEX_TOKEN", "TMDB_API_KEY", "TMDB_READ_TOKEN", "JELLYFIN_API_KEY", "EMBY_API_KEY")
# Read once at startup, so a saved change waits for the next container restart.
RESTART_KEYS = ("REDIRECT_MODE",)
DEFAULTS = {"PLEX_LIBRARY_ID": "7",
            "PLEX_MOVIE_MOUNT_ROOT": "/mnt/vod-bridge", "PLEX_SERIES_MOUNT_ROOT": "/mnt/vod-bridge-series",
            "PLEX_MOVIE_CATEGORY_MOUNT_ROOT": "/mnt/vod-bridge-movies",
            "MAX_CONCURRENT_PER_TITLE": "2", "REDIRECT_MODE": "false"}
# What the compose file (or the built-in default) says, as text, for the Settings form.
ENV_VALUES = {k: os.environ.get(k, DEFAULTS.get(k, "")) for k in SETTINGS_KEYS}
# Saved values that could not be decrypted (settings key changed); the compose value is used instead.
UNREADABLE = set()


def _converted(key: str, value: str):
    """Setting text -> (module attribute name, typed value)."""
    if key in ("DISPATCHARR_URL", "PLEX_URL", "JELLYFIN_URL", "EMBY_URL",
               "PLEX_MOVIE_MOUNT_ROOT", "PLEX_SERIES_MOUNT_ROOT", "PLEX_MOVIE_CATEGORY_MOUNT_ROOT"):
        return key, value.rstrip("/")
    if key == "MAX_CONCURRENT_PER_TITLE":
        return key, int(value)
    if key == "REDIRECT_MODE":
        return "REDIRECT_STREAMING", value.lower() in ("1", "true", "yes")
    return key, value


def apply_settings(values: dict):
    """Set values here and in every app module that imported them by name."""
    import sys
    attrs = dict(_converted(k, v) for k, v in values.items() if k in SETTINGS_KEYS or k in INSTANCE_KEYS)
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in list(sys.modules.values()):
        if os.path.dirname(os.path.abspath(getattr(mod, "__file__", None) or "/")) != here:
            continue
        for k, v in attrs.items():
            if hasattr(mod, k):
                setattr(mod, k, v)


def _load_saved():
    """Apply saved settings at startup: decrypt secrets, encrypt any still in plain text,
    and re-encrypt with SETTINGS_KEY once it is set."""
    import logging
    import sqlite3
    import secret_box
    import sources
    from database import DB_PATH
    log = logging.getLogger(__name__)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            instance, all_on_env_key = sources.prepare(conn)
            saved = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
            values, rewrite = {}, []
            for k in SETTINGS_KEYS:
                v = saved.get(k)
                if not v:
                    continue
                if k in SECRET_KEYS:
                    try:
                        plain = secret_box.decrypt(v)
                    except ValueError:
                        UNREADABLE.add(k)
                        all_on_env_key = False
                        log.warning("Saved %s could not be decrypted (settings key changed); using compose value", k)
                        continue
                    if not secret_box.is_encrypted(v):
                        rewrite.append((k, secret_box.encrypt(plain)))
                    elif not secret_box.on_primary_key(v):
                        rewrite.append((k, secret_box.rotate(v)))
                    v = plain
                try:
                    _converted(k, v)
                except ValueError:
                    log.warning("Ignoring saved %s=%r: not a valid value", k, v)
                    continue
                values[k] = v
            if rewrite:
                conn.executemany("UPDATE app_settings SET value=? WHERE key=?", [(s, k) for k, s in rewrite])
            if secret_box.primary_is_env() and all_on_env_key:
                secret_box.remove_key_file()
    except sqlite3.Error:
        return  # first start: the table is created by init_db
    apply_settings({**values, **instance})


_load_saved()
