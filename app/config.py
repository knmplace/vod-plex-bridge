import os

DISPATCHARR_URL = os.environ.get("DISPATCHARR_URL", "http://localhost:9191")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_READ_TOKEN = os.environ.get("TMDB_READ_TOKEN", "")
STRM_OUTPUT_DIR = os.environ.get("STRM_OUTPUT_DIR", "/plex-vod/Movies")
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "192.168.1.94")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8585"))
DISPATCHARR_API_KEY = os.environ.get("DISPATCHARR_API_KEY", "")

PLEX_URL = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_LIBRARY_ID = os.environ.get("PLEX_LIBRARY_ID", "7")

CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
CACHE_MAX_GB = float(os.environ.get("CACHE_MAX_GB", "25"))
CACHE_MAX_BYTES = int(CACHE_MAX_GB * 1024 * 1024 * 1024)
CACHE_IDLE_MINUTES = int(os.environ.get("CACHE_IDLE_MINUTES", "15"))
CACHE_IDLE_SECONDS = CACHE_IDLE_MINUTES * 60
