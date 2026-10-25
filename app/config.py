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

MAX_CONCURRENT_PER_TITLE = int(os.environ.get("MAX_CONCURRENT_PER_TITLE", "2"))
REDIRECT_STREAMING = os.environ.get("REDIRECT_MODE", "false").lower() in ("1", "true", "yes")

STRM_SERIES_OUTPUT_DIR = os.environ.get("STRM_SERIES_OUTPUT_DIR", "/plex-vod/Series")
PLEX_SERIES_LIBRARY_ID = os.environ.get("PLEX_SERIES_LIBRARY_ID", "")
# Where the series folders appear on the Plex host (the rclone mount of /vod-series/).
PLEX_SERIES_MOUNT_ROOT = os.environ.get("PLEX_SERIES_MOUNT_ROOT", "/mnt/vod-bridge-series").rstrip("/")
PLEX_MOVIE_MOUNT_ROOT = os.environ.get("PLEX_MOVIE_MOUNT_ROOT", "/mnt/vod-bridge").rstrip("/")



# Connection settings saved from the Settings tab win over the compose environment above.
CONNECTION_MODE = os.environ.get("CONNECTION_MODE", "dispatcharr")
DISPATCHARR_XC_USERNAME = os.environ.get("DISPATCHARR_XC_USERNAME", "")
DISPATCHARR_XC_PASSWORD = os.environ.get("DISPATCHARR_XC_PASSWORD", "")
XC_SERVER_URL = os.environ.get("XC_SERVER_URL", "")
XC_USERNAME = os.environ.get("XC_USERNAME", "")
XC_PASSWORD = os.environ.get("XC_PASSWORD", "")
CONNECTION_KEYS = ("CONNECTION_MODE", "DISPATCHARR_URL", "DISPATCHARR_API_KEY", "DISPATCHARR_XC_USERNAME",
                   "DISPATCHARR_XC_PASSWORD", "XC_SERVER_URL", "XC_USERNAME", "XC_PASSWORD")


def apply_settings(values: dict):
    """Set saved values here and in every app module that imported them by name."""
    import sys
    values = {k: v for k, v in values.items() if k in CONNECTION_KEYS and v not in (None, "")}
    if "DISPATCHARR_URL" in values:
        values["DISPATCHARR_URL"] = values["DISPATCHARR_URL"].rstrip("/")
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in list(sys.modules.values()):
        if os.path.dirname(os.path.abspath(getattr(mod, "__file__", None) or "/")) != here:
            continue
        for k, v in values.items():
            if hasattr(mod, k):
                setattr(mod, k, v)


def _load_saved():
    import sqlite3
    from database import DB_PATH
    try:
        with sqlite3.connect(DB_PATH) as conn:
            apply_settings(dict(conn.execute("SELECT key, value FROM app_settings").fetchall()))
    except sqlite3.Error:
        pass  # first start: the table is created by init_db


_load_saved()
