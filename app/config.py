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


