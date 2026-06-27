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

DISPATCHARR_XC_USERNAME = os.environ.get("DISPATCHARR_XC_USERNAME", "")
DISPATCHARR_XC_PASSWORD = os.environ.get("DISPATCHARR_XC_PASSWORD", "")

