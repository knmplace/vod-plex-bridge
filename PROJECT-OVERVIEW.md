# VOD Plex Bridge — Project Overview

## What It Does

VOD Plex Bridge is a self-hosted Docker application that brings Video On Demand (VOD) content from an IPTV provider into Plex Media Server. It acts as an intermediary between Dispatcharr (an IPTV stream management platform) and Plex, allowing users to browse, select, and stream VOD movies directly through the Plex interface — just like any other movie in their library.

## The Problem It Solves

IPTV providers offer thousands of VOD movies, but there's no native way to watch them through Plex. Users are typically limited to the provider's own player or third-party IPTV apps. VOD Plex Bridge solves this by:

- Importing the VOD catalog from Dispatcharr with full metadata (titles, years, ratings, genres, posters, trailers)
- Letting users filter and select which movies they want available in Plex
- Proxying video streams so Plex sees real video files it can play natively
- Managing the full lifecycle: activate movies to add them to Plex, deactivate to remove them

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌──────────────┐
│              │     │                  │     │             │     │              │
│  Plex Media  │◄────│   rclone HTTP    │◄────│  VOD Plex   │◄────│  Dispatcharr  │◄──── Provider
│   Server     │     │   Mount (fuse)   │     │   Bridge    │     │  (via VPN)    │     (upstream)
│              │     │                  │     │             │     │              │
└──────────────┘     └──────────────────┘     └─────────────┘     └──────────────┘
```

### Components

1. **VOD Plex Bridge** — FastAPI Python application running in Docker
2. **Dispatcharr** — IPTV stream management platform with VOD proxy support, running behind a VPN container (gluetun) for provider privacy
3. **rclone** — Mounts the bridge's HTTP endpoint as a local filesystem on the Plex server
4. **Plex Media Server** — Sees the rclone mount as a standard movie library folder

### How the Stream Flows

1. User activates a movie in the Bridge UI
2. Plex scans the rclone mount and discovers the new "file"
3. When a user plays the movie in Plex, rclone requests it from the Bridge
4. The Bridge opens a single persistent connection to Dispatcharr and downloads the full movie to a local disk cache
5. All subsequent range requests from Plex are served directly from the cache
6. Dispatcharr proxies the stream from the IPTV provider through a VPN tunnel
7. The movie plays in Plex like any local file — with seeking, pause/resume, and progress tracking

## Key Features

### Catalog Management
- **Provider filtering** — Select which IPTV accounts/providers to pull movies from
- **Category filtering** — Choose specific genres or categories (Action, Comedy, Drama, etc.)
- **Targeted sync** — Only fetches movies matching your selected providers + categories, not the entire catalog
- **Search and browse** — Full-text search with sortable grid (by rating, year, or name)

### Stream Caching
- When Plex first requests a movie, the Bridge downloads the entire file in one persistent connection and caches it to disk
- Subsequent playback requests are served instantly from the cache — no repeated round-trips to the provider
- Dispatcharr sees one clean VOD session per movie instead of hundreds of short-lived connections
- Cache auto-evicts after 15 minutes of inactivity (configurable)
- Maximum cache size is configurable (default 25 GB) with LRU eviction

### Plex Integration
- **Automatic library updates** — Activating movies makes them appear in Plex after a library scan
- **Automatic cleanup** — Deactivating movies sends a DELETE to the Plex API, removing them immediately
- **Metadata matching** — Plex automatically enriches movies with its own metadata (posters, descriptions, cast, trailers)
- **Continue Watching** — Plex tracks playback progress across sessions

### UI Features
- Dark-themed responsive web interface
- Provider sidebar with multi-select
- Category grid with movie counts and search
- Movie browser with poster grid, pagination (60/100/200/300 per page), and activation controls
- Sync overlay with progress indicator and stop button
- YouTube trailer playback (embedded modal)
- Version display in header

## Infrastructure Setup

### Prerequisites

- **Dispatcharr** instance with VOD proxy enabled, running behind a VPN container (e.g., gluetun)
- **Plex Media Server** on a separate host or LXC
- **Docker** and **Portainer** (or docker-compose) for container management
- **rclone** installed on the Plex host

### 1. Deploy VOD Plex Bridge

The Bridge runs as a Docker container. Example `docker-compose.yml`:

```yaml
services:
  vod-plex-bridge:
    image: vod-plex-bridge:latest
    pull_policy: never          # locally built image
    container_name: vod-plex-bridge
    restart: unless-stopped
    ports:
      - "8585:8585"
    environment:
      - DISPATCHARR_URL=http://<dispatcharr-host>:<port>
      - BRIDGE_HOST=<bridge-host-ip>
      - BRIDGE_PORT=8585
      - STRM_OUTPUT_DIR=/plex-vod/Movies
      - DB_PATH=/data/vod_bridge.db
      - CATEGORY_MAPPING_FILE=/data/category_mapping.json
      - STREAM_MAPPING_FILE=/data/stream_mapping.json
      - DISPATCHARR_API_KEY=<your-dispatcharr-api-key>
      - PLEX_URL=http://<plex-host>:32400
      - PLEX_TOKEN=<your-plex-token>
      - PLEX_LIBRARY_ID=<plex-library-section-id>
      - CACHE_MAX_GB=25
      - CACHE_IDLE_MINUTES=15
    volumes:
      - /path/to/data:/data
      - /path/to/plex-vod:/plex-vod
```

**Environment Variables:**

| Variable | Description | Default |
|----------|-------------|---------|
| `DISPATCHARR_URL` | URL of your Dispatcharr instance | `http://localhost:9191` |
| `DISPATCHARR_API_KEY` | API key for Dispatcharr authentication | — |
| `BRIDGE_HOST` | Host IP the bridge is accessible on | `192.168.1.94` |
| `BRIDGE_PORT` | Port the bridge listens on | `8585` |
| `DB_PATH` | Path to SQLite database | `/data/vod_bridge.db` |
| `CATEGORY_MAPPING_FILE` | Path to category-to-movie mapping JSON | `/data/category_mapping.json` |
| `STREAM_MAPPING_FILE` | Path to stream mapping JSON (movie ID → stream ID + provider) | `/data/stream_mapping.json` |
| `PLEX_URL` | Plex server URL | — |
| `PLEX_TOKEN` | Plex authentication token | — |
| `PLEX_LIBRARY_ID` | Plex library section ID for the stream-movies library | `7` |
| `CACHE_DIR` | Directory for cached movie files | `/data/cache` |
| `CACHE_MAX_GB` | Maximum cache size in GB | `25` |
| `CACHE_IDLE_MINUTES` | Minutes before idle cache files are evicted | `15` |

### 2. Generate Mapping Files

The Bridge requires two JSON mapping files from Dispatcharr:

**`category_mapping.json`** — Maps VOD category IDs to movie IDs:
```json
[
  { "id": 101, "name": "Action", "movie_ids": [12345, 12346, ...] },
  { "id": 102, "name": "Comedy", "movie_ids": [23456, 23457, ...] }
]
```

**`stream_mapping.json`** — Maps movie IDs to stream IDs and provider account info:
```json
{
  "12345": { "stream_id": 54321, "account_id": 2, "account_name": "Provider A" },
  "12346": { "stream_id": 54322, "account_id": 2, "account_name": "Provider A" }
}
```

These are generated by querying Dispatcharr's Django ORM. Example (run inside the Dispatcharr container):

```bash
# Category mapping
python3 -c "
import json, os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dispatcharr.settings'
django.setup()
from apps.vod.models import VodCategory
cats = []
for c in VodCategory.objects.filter(category_type='movie'):
    ids = list(c.movies.values_list('id', flat=True))
    cats.append({'id': c.id, 'name': c.name, 'movie_ids': ids})
print(json.dumps(cats))
" > /data/category_mapping.json

# Stream mapping
python3 -c "
import json, os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'dispatcharr.settings'
django.setup()
from apps.vod.models import VodStream
mapping = {}
for s in VodStream.objects.select_related('m3u_account').filter(movie__isnull=False):
    mapping[str(s.movie_id)] = {
        'stream_id': s.id,
        'account_id': s.m3u_account_id,
        'account_name': s.m3u_account.name if s.m3u_account else ''
    }
print(json.dumps(mapping))
" > /data/stream_mapping.json
```

### 3. Set Up rclone Mount on Plex Host

rclone creates a FUSE mount that presents the Bridge's HTTP listing as a local directory.

**Create rclone config** (`/root/.config/rclone/rclone.conf`):
```ini
[vod-bridge]
type = http
url = http://<bridge-host>:8585/vod/
```

**Create a systemd service** (`/etc/systemd/system/rclone-vod.service`):
```ini
[Unit]
Description=rclone VOD Bridge mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p /mnt/vod-bridge
ExecStart=/usr/bin/rclone mount vod-bridge: /mnt/vod-bridge \
    --allow-other \
    --dir-cache-time 30s \
    --poll-interval 30s \
    --vfs-cache-mode off \
    --read-only \
    --no-modtime
ExecStop=/bin/fusermount -uz /mnt/vod-bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now rclone-vod
```

### 4. Add Library in Plex

1. Open Plex → Settings → Manage → Libraries
2. Add Library → Movies
3. Name: `Stream-Movies` (or your preference)
4. Add folder: `/mnt/vod-bridge`
5. Advanced: Disable "Enable video preview thumbnails" (these are streamed files, not local)

### 5. Get Your Plex Token

To find your Plex authentication token:
```bash
grep -oP 'PlexOnlineToken="\K[^"]+' \
  '/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml'
```

## How to Use

1. **Open the Bridge UI** at `http://<bridge-host>:8585`
2. **Select providers** — Click the providers you want movies from in the left sidebar
3. **Select categories** — Click the genre/category tiles you want (Action, Comedy, etc.)
4. **Sync Selected** — Fetches movie metadata from Dispatcharr for your selections
5. **Browse** — Scroll through the movie grid, search, sort by rating/year/name
6. **Activate** — Select movies and click Activate to make them available in Plex
7. **Scan in Plex** — Plex will discover new movies on its next library scan (or trigger manually)
8. **Play** — Open Plex and play the movie like any other title
9. **Deactivate** — Select movies and click Deactivate; they'll be automatically removed from Plex

## Cache Monitoring

The Bridge exposes a cache stats endpoint:

```
GET http://<bridge-host>:8585/api/cache/stats
```

Returns:
```json
{
  "entries": 2,
  "total_cached_mb": 1960.6,
  "max_cache_mb": 25600.0,
  "active_downloads": 0,
  "movies": {
    "961613": {
      "downloaded_mb": 893.3,
      "total_mb": 893.3,
      "complete": true,
      "failed": false,
      "idle_seconds": 35
    }
  }
}
```

## File Structure

```
vod-plex-bridge/
├── Dockerfile
├── requirements.txt
├── docker-compose.yml
├── app/
│   ├── main.py          # FastAPI app, version, lifespan (cache start/stop)
│   ├── api.py           # REST API endpoints (sync, browse, activate, deactivate)
│   ├── proxy.py         # Video proxy — serves cached movies to rclone/Plex
│   ├── cache.py         # Stream cache — persistent downloads, LRU eviction
│   ├── scraper.py       # Catalog scraper — fetches movies from Dispatcharr API
│   ├── database.py      # SQLite schema, migrations, init
│   ├── config.py        # Environment variable configuration
│   ├── generator.py     # STRM file generator (legacy)
│   ├── stream_mapper.py # Applies stream_mapping.json to movie records
│   └── templates/
│       └── index.html   # Single-page web UI
```

## Technology Stack

- **Backend**: Python 3.12, FastAPI, uvicorn
- **Database**: SQLite (via aiosqlite)
- **HTTP Client**: httpx (async, streaming support)
- **Frontend**: Vanilla HTML/CSS/JavaScript (single file, no build step)
- **Container**: Docker (locally built image, managed via Portainer)
- **Mount**: rclone (HTTP remote → FUSE mount)
- **VPN**: gluetun (WireGuard/OpenVPN container — Dispatcharr routes through this)

## Version History

| Version | Changes |
|---------|---------|
| 0.5.0 | Initial release — catalog sync, STRM generation, basic proxy |
| 0.5.1 | Fixed clear-catalog wiping category mappings; auto-reload from dump file |
| 0.5.2 | Removed TMDB enrichment (Dispatcharr metadata is sufficient) |
| 0.5.3 | Fixed browse showing 0 movies; auto-apply stream mapping after sync |
| 0.6.0 | YouTube trailer playback; trailer_key column |
| 0.6.1 | STRM folder cleanup on deactivation |
| 0.7.0 | Provider filtering for sync and browse |
| 0.8.0 | Stream caching — persistent downloads, LRU eviction, 15-min idle cleanup |
| 0.8.1 | Page size selector (60/100/200/300) at top and bottom of browse |
| 0.8.2 | Plex auto-delete on deactivation; content-length fix for seeks; cache idle reduced to 15 min |
