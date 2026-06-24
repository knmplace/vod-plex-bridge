# VOD Plex Bridge — Build & Deploy SOP

Complete guide for deploying VOD Plex Bridge from scratch. This document serves as the system prompt for AI assistants building this application for new users.

---

## Pre-Deployment Checklist (Probing Questions)

Before writing any code or deploying, gather this information from the user:

### Infrastructure
- [ ] **Where is Dispatcharr running?** (IP/hostname and port — e.g., `192.168.1.94:9191`)
- [ ] **Is Dispatcharr behind a VPN container (gluetun)?** If yes, all restarts must go through Portainer/compose, never `docker restart`.
- [ ] **What is the Dispatcharr container name?** (e.g., `dispatcharr-IPTV2-94`) — needed for dump scripts.
- [ ] **Where will the bridge run?** (same host as Dispatcharr, or separate?) — needs Docker installed.
- [ ] **Where is the Plex server?** (IP and port — typically `<IP>:32400`)
- [ ] **Can the bridge host reach Dispatcharr AND Plex?** Verify network connectivity before proceeding.

### Credentials
- [ ] **Dispatcharr API key** — generate from Dispatcharr admin panel → Settings → API Keys.
- [ ] **Dispatcharr XC username/password** — a Dispatcharr User with `xc_password` in `custom_properties`. Used for XC endpoint routing (session-based playback). Set via env vars `DISPATCHARR_XC_USERNAME` / `DISPATCHARR_XC_PASSWORD`.
- [ ] **Plex token** — from Plex account XML endpoint or browser dev tools.
- [ ] **Plex library section ID** — which library will host streamed movies? Find via: `curl http://<PLEX>:32400/library/sections?X-Plex-Token=<TOKEN>`
- [ ] **TMDB API key** (optional but recommended) — for genre enrichment, posters, and language detection. Get from themoviedb.org.

### M3U Accounts / Providers
- [ ] **Which M3U accounts have VOD content?** List account IDs and names.
- [ ] **Do any accounts share the same stream_ids?** (Provider groups — e.g., Amber Baby 1/2/3 all share IDs, WarpTV LIVE 2/3 share IDs). Define in `stream_mapper.py` → `PROVIDER_GROUPS`.
- [ ] **Any known dead accounts?** (e.g., WarpTV LIVE 1 has 0 streams — exclude from provider groups).

### Container Management
- [ ] **Using Portainer or docker-compose CLI?** If Portainer, never `docker stop/rm/run` manually.
- [ ] **Where should bridge data live on disk?** (e.g., `/etc/docker/plexbridge/data`)
- [ ] **Is there a shared NAS mount?** Only needed for legacy .strm mode.

---

## Architecture Overview

```
User activates movie in Bridge UI
    ↓
Bridge hits Dispatcharr XC endpoint → 301 redirect with session_id
    ↓
Session cached. Head+tail fetched via same session (20MB each)
    ↓
Cached in SQLite as BLOBs, provider connection closed
    ↓
Plex scans library via rclone HTTP mount → Bridge serves from cache
    ↓
User plays in Plex → Bridge proxies via same session_id (1 connection)
    ↓
User stops → Bridge detects disconnect, closes upstream immediately
```

**Key principles:**
- Plex NEVER talks to the provider directly. The bridge is the only gateway.
- Provider connections happen only during activation (2 requests) and playback (1 streaming connection).
- All requests for a movie reuse the same Dispatcharr session_id, preventing connection flooding.

---

## Project Structure

```
vod-plex-bridge/
├── .env.example              # Template — copy to .env, fill in values
├── .env                      # YOUR credentials (never commit)
├── .gitignore                # Excludes .env, *.db, data/
├── Dockerfile
├── docker-compose.yml        # References .env via env_file directive
├── docker-compose.example.yml
├── requirements.txt
├── dump_vod_data.sh          # Host cron script for Dispatcharr data dumps
├── BUILD_SOP.md              # This file
└── app/
    ├── main.py               # FastAPI app, version, lifespan, schedulers
    ├── config.py             # Environment variable configuration
    ├── database.py           # SQLite schema + migrations
    ├── proxy.py              # Stream proxy, circuit breaker, cache serving, disconnect detection
    ├── api.py                # REST API: catalog, activation, dead scan, validation, health
    ├── scraper.py            # VOD catalog scraper from Dispatcharr API
    ├── stream_mapper.py      # Multi-provider stream_id mapping + provider groups
    ├── health.py             # Health check system (bridge, Dispatcharr, rclone)
    ├── generator.py          # .strm + .nfo file generator
    ├── cache.py              # Legacy disk cache (not used in stream-through mode)
    └── templates/
        └── index.html        # Web UI (Browse, Catalog, Dead Movies, Proxy Logs, Health)
```

---

## Step 1: Environment Configuration

**All secrets go in `.env` — never in docker-compose.yml or code.**

```bash
cp .env.example .env
# Edit .env with your values
```

Required `.env` variables:
| Variable | Description | Example |
|----------|-------------|---------|
| `DISPATCHARR_URL` | Dispatcharr base URL (no trailing slash) | `http://192.168.1.94:9191` |
| `DISPATCHARR_API_KEY` | API key from Dispatcharr admin | `TRAr3dDa...` |
| `DISPATCHARR_XC_USERNAME` | Dispatcharr User for XC endpoint auth | `Claude` |
| `DISPATCHARR_XC_PASSWORD` | Dispatcharr User's XC password | `Peaches` |
| `BRIDGE_HOST` | IP that Plex/rclone will use to reach bridge | `192.168.1.94` |
| `BRIDGE_PORT` | Bridge HTTP port | `8585` |
| `PLEX_URL` | Plex server URL | `http://192.168.1.109:32400` |
| `PLEX_TOKEN` | Plex authentication token | `abc123...` |
| `PLEX_LIBRARY_ID` | Plex library section ID for streamed movies | `7` |

Optional:
| Variable | Description |
|----------|-------------|
| `TMDB_API_KEY` | TMDB API key for enrichment |
| `TMDB_READ_TOKEN` | TMDB read access token (alternative to API key) |
| `DATA_DIR` | Host path for bridge data (default: `/etc/docker/plexbridge/data`) |
| `PLEX_VOD_DIR` | Host path for .strm output (default: `/mnt/media50/plex-vod`) |

---

## Step 2: Validate Connectivity

**Do this before building anything.**

```bash
# From bridge host → Dispatcharr API
curl -s "http://<DISPATCHARR_HOST>:<PORT>/api/vod/movies/?page=1&page_size=1" \
  -H "X-API-Key: <YOUR_API_KEY>" | head -c 200

# From bridge host → Dispatcharr VOD proxy (range request support)
curl -sI "http://<DISPATCHARR_HOST>:<PORT>/proxy/vod/movie/<ANY_UUID>?stream_id=<ANY_ID>" \
  -H "X-API-Key: <YOUR_API_KEY>" -H "Range: bytes=0-0"
# Expect: HTTP 206 with Content-Range header

# From Plex server → Bridge (after deployment)
curl -s http://<BRIDGE_HOST>:<BRIDGE_PORT>/version
# Expect: {"version":"0.15.0"}

# Plex API access
curl -s "http://<PLEX_HOST>:32400/identity?X-Plex-Token=<TOKEN>"
# Expect: XML with machineIdentifier
```

**If any fail, fix networking/firewall/VPN before proceeding.**

---

## Step 3: Configure Provider Groups

Edit `app/stream_mapper.py` → `PROVIDER_GROUPS` to match your M3U accounts:

```python
PROVIDER_GROUPS = {
    "provider_a": [10, 13, 14],   # Accounts that share the same stream_ids
    "provider_b": [2, 11, 12],    # Another provider group
}
```

Accounts in the same group share stream_ids. When a user selects any account from a group, the bridge picks the stream_id from that group. This enables multi-provider redundancy without duplicate streams.

---

## Step 4: Build & Deploy

### First-time build

```bash
# On bridge host
mkdir -p /etc/docker/plexbridge/data
mkdir -p /tmp/vod-build

# Copy project files to build directory
scp -r app/ templates/ Dockerfile requirements.txt user@<BRIDGE_HOST>:/tmp/vod-build/

# Build Docker image
cd /tmp/vod-build
docker build --no-cache -t vod-plex-bridge:latest .

# Deploy
cp .env /etc/docker/plexbridge/.env
cp docker-compose.yml /etc/docker/plexbridge/
cd /etc/docker/plexbridge
docker compose up -d
```

### Code updates (subsequent deploys)

```bash
# 1. Upload ALL app files (partial uploads can cause stale cached layers)
scp -r app/* user@<BRIDGE_HOST>:/tmp/vod-build/app/

# 2. Rebuild with --no-cache
ssh user@<BRIDGE_HOST> "cd /tmp/vod-build && docker build --no-cache -t vod-plex-bridge:latest ."

# 3. Redeploy
#    Portainer: Stack → Redeploy (pull_policy:never uses local image)
#    CLI: docker compose up -d --force-recreate
```

**IMPORTANT:** If using Portainer, never `docker stop/rm/run` the container manually.

---

## Step 5: Dispatcharr Data Dumps

The bridge needs movie→stream_id mappings from Dispatcharr's Django database.

### Deploy the dump script

```bash
# Copy to bridge host
scp dump_vod_data.sh user@<BRIDGE_HOST>:/etc/docker/plexbridge/

# Edit script — set container name and data directory:
DISPATCHARR_CONTAINER="your-dispatcharr-container-name"
BRIDGE_DATA_DIR="/etc/docker/plexbridge/data"

# Test it
bash /etc/docker/plexbridge/dump_vod_data.sh

# Add to cron (every 6 hours)
crontab -e
0 */6 * * * /etc/docker/plexbridge/dump_vod_data.sh
```

### What the dump produces
- `stream_mapping.json` — `{movie_id: [{stream_id, ext, account_id, account_name}, ...]}`
  - Multi-provider: each movie has a LIST of entries, one per provider
- `category_mapping.json` — `[{id, name, movie_ids: [...]}]`

---

## Step 6: rclone HTTP Mount on Plex Server

This is how Plex accesses bridge movies as local files.

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure remote
rclone config
# → New remote → name: vodbridge → type: HTTP → URL: http://<BRIDGE_HOST>:8585/vod/

# Create mount point and mount
mkdir -p /mnt/vod-bridge
rclone mount vodbridge: /mnt/vod-bridge \
  --read-only --allow-other --dir-cache-time 30s \
  --vfs-cache-mode off --no-modtime --daemon

# Add Plex library: Movie library → /mnt/vod-bridge
```

### Systemd service (auto-start on boot)

```ini
# /etc/systemd/system/vod-bridge-mount.service
[Unit]
Description=VOD Bridge rclone mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/rclone mount vodbridge: /mnt/vod-bridge \
  --read-only --allow-other --dir-cache-time 30s --vfs-cache-mode off --no-modtime
ExecStop=/bin/fusermount -u /mnt/vod-bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now vod-bridge-mount.service
```

---

## Step 7: Initial Data Load

1. Open bridge UI: `http://<BRIDGE_HOST>:<BRIDGE_PORT>/`
2. Click **Reload Categories** — loads category + account data from Dispatcharr
3. Select **Providers** (M3U accounts) in left panel
4. Select **Categories** in right panel
5. Click **Sync Selected** — scrapes movie metadata from Dispatcharr API
6. Language detection runs automatically in parallel during sync

---

## Step 8: Activate Movies & Test

1. Browse movies, select ones to make available in Plex
2. Click **Activate** — this:
   - Refreshes stream_ids from current mapping (picks correct provider)
   - Invalidates stale cache if stream_id changed
   - Fetches 20MB head + 20MB tail from provider via Dispatcharr
   - Stores as BLOBs in SQLite — provider connection closed
3. Scan Plex library — movies appear via rclone mount
4. **Verify in Proxy Logs tab:**
   - `Header+tail cached` — activation fetched successfully
   - `Header cache hit` / `Tail cache hit` — Plex scan served from cache
   - Zero `Stream proxy request` entries during scan = no provider hits

---

## How the Cache System Works

### Problem it solves
Plex reads specific byte ranges when scanning metadata: container headers (start of file) and moov atoms (start or end of file for MP4). Without caching, every Plex scan would open a live connection to the provider, consuming concurrent connection slots and risking rate limits.

### Cache strategy
| Cache | Size | Purpose |
|-------|------|---------|
| Head | 20MB | MKV container headers, optimized MP4 moov atoms |
| Tail | 20MB | Non-optimized MP4 moov atoms at end of file |
| Total | 40MB per movie | Covers everything Plex needs for metadata scanning |

### Request flow
1. **Activation**: 301 redirect → session_id captured → head + tail fetched via same session, then disconnect
2. **Plex scan**: Served entirely from SQLite cache — zero upstream requests
3. **Playback**: Reuses cached session_id → 1 streaming proxy connection through Dispatcharr
4. **Stop playback**: Bridge detects disconnect, closes upstream immediately
5. **Session expiry**: If session_id returns 401/403/404, cache cleared, new session auto-resolved on next request

### Cache invalidation
- When stream_id changes (provider switch), cached bytes are cleared
- Re-activation fetches fresh head+tail for the new stream

---

## Operational Systems

### Session Reuse (v0.17.1 — prevents connection flooding)
- First XC request for a movie → Dispatcharr returns 301 with `session_id` in Location header
- Bridge captures and caches `movie_id → (session_id, resolved_url)` in memory
- All subsequent requests append `?session_id=XXX` → Dispatcharr reuses same session/provider connection
- Per-movie asyncio locks prevent concurrent first-requests from racing
- Session TTL: 1 hour. Auto-clears on 401/403/404/410 and retries fresh
- Without this: every Plex range request creates a new provider connection → 509 bandwidth exceeded

### Circuit Breaker
- 3 consecutive failures for a stream_id → HTTP 503 for 30 seconds
- Prevents hammering when providers rate-limit (509) or error (500)

### Dead Movie Detection
- **Immediate deactivation**: HTTP 500+ during playback → movie deactivated + removed from Plex
- **3-strike system**: After 3 total strikes, movie marked `stream_dead` and hidden from browse
- **Catalog validation**: Every 4h, probes 500 activated movies. "Full Sweep" checks all.
- **Resurrection**: Every 8h, re-tests 50 dead streams. Clears dead flag if provider restored them.

### Zombie Connection Prevention
- `request.is_disconnected()` check during streaming
- 60-second read timeout (down from 300s)
- When Plex stops, upstream closes in seconds, not minutes

### Multi-Provider Stream Mapping
- Movies can have stream_ids from multiple providers
- `pick_stream_for_account()` selects the correct stream based on user's provider selection
- Provider groups define which accounts share stream_ids
- Switching providers invalidates cache and re-fetches for the new stream

---

## CRITICAL: Plex Library Settings

**Disable these in the Stream-Movies library to prevent Plex from downloading entire movies:**

In Plex → Library → Stream-Movies → Edit → Advanced:
- [ ] **Generate video preview thumbnails**: OFF
- [ ] **Generate intro video markers**: OFF
- [ ] **Generate credits video markers**: OFF

In Plex → Settings → Scheduled Tasks:
- [ ] Disable any deep analysis tasks for the Stream-Movies library

These features download the full movie file for analysis, which hammers the provider and consumes connection slots.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Movies not in Plex | rclone mount down or Plex not scanning | `ls /mnt/vod-bridge`, trigger scan |
| Plex scan hammers provider | Missing head/tail cache | Re-activate movie, check Proxy Logs for cache hits |
| Playback fails on mobile | Plex app transcoding | Set quality to Original/Maximum |
| HTTP 503 from bridge | Circuit breaker open | Wait 30s, check provider |
| HTTP 502 from bridge | Provider returning errors | Check Dispatcharr, provider may be down |
| "No stream mapping" | Dump script hasn't run | `bash dump_vod_data.sh` |
| Wrong provider on activation | Stale stream mapping | Reload Categories (re-runs dump) |
| HTTP 509 bandwidth exceeded | Multiple sessions opened per movie | Verify session reuse: logs should show "Reusing session". If not, check `_movie_sessions` dict. |
| Provider kills stream after activation | Too many requests | Should not happen with 2-request pattern. Check Proxy Logs. |
| Zombie connections in Dispatcharr | Bridge not detecting disconnect | Restart bridge container. Check for old version without disconnect detection. |

---

## GitHub Distribution Notes

When preparing for public GitHub release:
1. Ensure `.gitignore` excludes: `.env`, `*.db`, `data/`, `__pycache__/`
2. Remove any hardcoded IPs, API keys, or passwords from all files
3. `docker-compose.yml` must use `env_file: .env` — never inline secrets
4. `PROVIDER_GROUPS` in `stream_mapper.py` should have placeholder examples
5. Include `.env.example` with all required variables documented
6. This BUILD_SOP.md serves as the README for deployment

---

## Version History

| Version | Changes |
|---------|---------|
| 0.17.1 | Session reuse — captures session_id from Dispatcharr 301, reuses for all requests per movie. Fixes connection flooding (509 errors) |
| 0.17.0 | XC endpoint routing via Dispatcharr user creds (not provider creds), movie_id (not stream_id) |
| 0.16.0 | Health check system, catalog validation scheduler, resurrection scheduler |
| 0.15.0 | Tail cache restored (20MB), head cache increased to 20MB, cache invalidation on provider switch, .env enforcement |
| 0.14.0 | Zombie connection fix (disconnect detection), single-request activation, playback auto-deactivation with Plex cleanup, multi-provider stream mapping |
| 0.13.0 | 3-strike dead detection, catalog validation, resurrection checks, stream health dashboard |
| 0.12.1 | Dead stream marking, HTTP 410 for dead streams, health check system |
| 0.11.1 | Auto-refresh stream_ids on startup + activation |
| 0.11.0 | Circuit breaker, proxy activity log viewer |
| 0.10.0 | Head/tail caching, stream-through proxy |
| 0.9.x | Dead movie scanning, language detection, bulk operations |
| 0.8.x | Multi-provider support, category filtering, rclone mount mode |
| 0.7.x | TMDB enrichment, genre filtering |
| 0.1-0.6 | Initial scraper, proxy, basic UI |
