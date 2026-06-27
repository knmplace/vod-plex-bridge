# VOD Plex Bridge — Interactive Setup Guide

**Purpose:** Drop this file into a fresh Claude Code chat to get step-by-step help deploying VOD Plex Bridge on your network. Claude will ask the right questions, validate your setup, and walk you through each step.

---

## For Claude: How to Use This Document

You are helping a user deploy VOD Plex Bridge — a Docker application that bridges VOD movies from Dispatcharr (an M3U IPTV proxy/management tool) into Plex. Your job is to guide them interactively through the setup process, asking questions at each step to gather their specific environment details.

**Your approach:**
1. Read this entire document first to understand the architecture
2. Start by explaining what the app does and what's needed (the Prerequisites below)
3. Confirm the user has all prerequisites before proceeding
4. Ask the user the discovery questions in Phase 1 — don't skip any
5. Work through each phase in order, confirming success before moving on
6. If something fails, troubleshoot before continuing
7. Adapt commands to the user's specific IPs, paths, and container names
8. Never hardcode IPs or credentials in code — everything goes in `.env`
9. If the user gives you SSH access to their servers, you can run commands directly. If not, provide them the exact commands to run and ask them to paste the output back to you.

**Tone:** Conversational but precise. Explain WHY each step matters, not just what to type. The user knows basic networking and Docker but may not know the specifics of these applications.

**First message to user:** Start by briefly explaining what the bridge does (2-3 sentences), then ask: "Before we start, let me make sure you have the prerequisites ready. Do you have these set up already?" and list the prerequisites below.

---

## Prerequisites (Must Have Before Starting)

These must be in place BEFORE you begin. If any are missing, help the user get them set up first:

1. **Dispatcharr** — An M3U IPTV proxy tool that manages IPTV provider accounts and streams. This is what the bridge reads VOD movie catalogs from. The user must have a running Dispatcharr instance with at least one active M3U account that includes VOD content. Dispatcharr repo: https://github.com/Dispatcharr/Dispatcharr
2. **Plex Media Server** — Running and accessible on the network. The bridge creates a Plex movie library from VOD content.
3. **Docker & Docker Compose** — Installed on the machine where the bridge will run. The bridge runs as a Docker container.
4. **Git** — To clone the repository.
5. **Shell access** to the machine running Dispatcharr — The dump script uses `docker exec` to extract movie/category/account data from Dispatcharr's Django database directly. The user needs to be able to run `docker exec` commands against the Dispatcharr container (typically via SSH to that host, or locally if same machine).

**Nice to have (optional):**
- **TMDB API key** — Free, gives movie posters, genres, descriptions. Get one at https://www.themoviedb.org/settings/api
- **Portainer** — If the user manages containers via Portainer, remind them to always redeploy through Portainer rather than `docker stop/rm/run` from CLI.

---

## What This Application Does

```
┌──────────┐     ┌──────────────┐     ┌───────────────┐     ┌──────┐
│   IPTV   │◄───►│  Dispatcharr  │◄───►│  VOD Plex     │◄───►│ Plex │
│ Provider  │     │  (M3U proxy)  │     │  Bridge       │     │      │
└──────────┘     └──────────────┘     └───────┬───────┘     └──┬───┘
                                              │                 │
                                         ┌────▼─────────────────▼────┐
                                         │   Shared Storage          │
                                         │   (.strm + .nfo + poster) │
                                         └───────────────────────────┘
```

**Key facts:**
- The bridge NEVER connects directly to the IPTV provider — all traffic goes through Dispatcharr
- Dispatcharr is the M3U proxy that manages IPTV accounts and streams. It handles the upstream provider connection, VPN routing, and failover. The bridge talks to Dispatcharr's API only.
- The bridge generates `.strm` files (text files with a URL) that Plex reads to stream movies
- Both the bridge container AND Plex must be able to access the same `.strm` directory
- Head/tail caching (8 MB + 256 KB) lets Plex probe movies without opening provider connections
- Probe throttle prevents Plex library scans from burning movies via rapid connection cycling
- "Burning" a movie = the IPTV provider temporarily blocks access because it detected too many rapid connections to the same stream. The bridge's caching and throttling features are designed to prevent this.

---

## Phase 1: Discovery Questions

Ask ALL of these before doing anything else. Fill in the answers as you go.

### Infrastructure
Ask the user:
1. **"Where is your Dispatcharr instance running? (IP address and port)"**
   - Example: `192.168.1.100:9191`
   - If they don't know: `docker ps | grep dispatcharr` on the host
   
2. **"What is your Dispatcharr Docker container name?"**
   - Example: `dispatcharr` or `dispatcharr-IPTV2`
   - Find it: `docker ps --format '{{.Names}}' | grep -i dispatch`

3. **"Is Dispatcharr behind a VPN container like gluetun?"**
   - If yes: the port in question 1 is the one mapped through gluetun to the host
   - The bridge does NOT need to be behind the VPN
   - Note: never `docker restart` Dispatcharr if it's behind gluetun — use Portainer or docker-compose

4. **"Where will you run the bridge? (same host as Dispatcharr, or a different host?)"**
   - Same host is simplest — no shared storage setup needed
   - Different host requires a network share between bridge and Plex

5. **"Where is your Plex server running? (IP address and port, usually :32400)"**
   - Example: `192.168.1.50:32400`

6. **"Do you use Portainer to manage your Docker containers, or docker-compose from the CLI?"**
   - If Portainer: remind them never to `docker stop/rm/run` manually

### Credentials (gather these — user may need to look them up)
7. **"Do you have a Plex authentication token? If not, I can help you find it."**
   - Guide them: Open Plex Web → play any media → open browser Dev Tools → Network tab → filter for `X-Plex-Token` in any request URL
   - Or: browse to `http://PLEX_IP:32400/library/sections?X-Plex-Token=YOUR_TOKEN` and look in the URL they used to access it
   - Alternative: `curl -s "http://PLEX_IP:32400/identity"` works without a token and confirms connectivity

8. **"Do you have a TMDB API key? It's optional but gives you movie posters, genres, and descriptions."**
   - Free at https://www.themoviedb.org/settings/api
   - They need either an API key OR a v4 Read Access Token, not both

### Storage
9. **"Can both the bridge and Plex access the same directory on disk?"**
   - Same host: easy — both mount the same local path
   - Different hosts: they need NFS, CIFS/SMB, or another network share
   - If they don't have shared storage set up, help them create an NFS share or use a CIFS mount

### M3U Accounts
10. **"How many M3U accounts do you have in Dispatcharr with VOD content? Do you know their names?"**
    - The bridge shows all active accounts as "providers" — user selects which ones to import from
    - If multiple accounts share the same content (e.g., same IPTV provider, multiple connections), mention that the bridge handles this via provider groups

---

## Phase 2: Validate Network Connectivity

Before building anything, verify the three components can talk to each other.

```bash
# From the bridge host → Dispatcharr API
curl -s "http://DISPATCHARR_IP:PORT/api/vod/movies/?page=1&page_size=1" | head -c 200
# Should return JSON with movie data

# From the bridge host → Plex (no token needed for identity)
curl -s "http://PLEX_IP:32400/identity"
# Should return XML with machineIdentifier

# If bridge and Dispatcharr are on different hosts, also test:
ping -c 2 DISPATCHARR_IP
ping -c 2 PLEX_IP
```

**If any of these fail, fix networking/firewall before proceeding.** Common issues:
- Firewall blocking the port
- Dispatcharr behind gluetun — make sure the port is mapped through to the host
- Wrong IP (host vs container IP)

---

## Phase 3: Clone and Configure

```bash
# On the bridge host
git clone https://github.com/knmplace/vod-plex-bridge.git
cd vod-plex-bridge

# Copy the example files
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
```

### Fill in .env

Help the user fill in each value using what you gathered in Phase 1:

```bash
# REQUIRED — fill these in with the user's actual values
DISPATCHARR_URL=http://___:___
PLEX_URL=http://___:32400
PLEX_TOKEN=___
PLEX_LIBRARY_ID=___          # They may not know this yet — set up in Phase 6
BRIDGE_HOST=___              # LAN IP of THIS machine (not 0.0.0.0)
BRIDGE_PORT=8585

# RECOMMENDED
TZ=___                       # e.g., America/New_York, America/Los_Angeles, Europe/London
TMDB_API_KEY=___             # or TMDB_READ_TOKEN

# PATHS — defaults are usually fine
DATA_DIR=./data
PLEX_VOD_DIR=./plex-vod
```

### Critical: Explain BRIDGE_HOST

Tell the user: "BRIDGE_HOST must be the LAN IP of this machine — the one Plex will use to connect to the bridge. When you activate a movie, the bridge creates a `.strm` file containing `http://BRIDGE_HOST:8585/stream/movie_123.mkv`. If Plex can't reach that address, playback fails."

How to find it:
```bash
hostname -I | awk '{print $1}'   # Linux
ipconfig | grep "IPv4"            # Windows
```

### Critical: Explain PLEX_VOD_DIR

Tell the user: "This is where the bridge writes `.strm` and `.nfo` files. Plex must be able to read this same directory. If bridge and Plex are on the same host, just use a local path. If they're on different hosts, both need to mount the same network share."

---

## Phase 4: Create Directories and Build

```bash
# Create data directories
mkdir -p data
mkdir -p plex-vod/Movies

# Build and start
docker compose up -d --build

# Verify it's running
docker compose logs -f
# Look for: "Uvicorn running on http://0.0.0.0:8585"
# Press Ctrl+C to exit logs
```

**Test the bridge is accessible:**
```bash
curl -s http://BRIDGE_HOST:8585/version
# Should return: {"version":"0.29.1"}
```

If it doesn't work:
- Check `docker ps` — is the container running?
- Check `docker compose logs` — any startup errors?
- Is the port mapped correctly? (`-p 8585:8585`)

---

## Phase 5: Run the Mapping Dump Script

The bridge needs mapping files that tell it which movies belong to which provider accounts. These are extracted from Dispatcharr's database using `docker exec` — the script runs Django ORM commands inside the Dispatcharr container to query the database directly. This means:
- You must run this script on the same machine that hosts the Dispatcharr Docker container
- The user running the script must have permission to run `docker exec` (usually root or docker group)

**Run this on the host where Dispatcharr's Docker container is running:**

```bash
# If bridge repo is on the same host as Dispatcharr:
DISPATCHARR_CONTAINER=your-container-name \
BRIDGE_DATA_DIR=/path/to/bridge/data \
bash setup/dump_mappings.sh
```

**If Dispatcharr is on a different host than the bridge:**
1. Copy `setup/dump_mappings.sh` to the Dispatcharr host
2. Run it there with `BRIDGE_DATA_DIR` set to a local temp directory (e.g., `/tmp/bridge-data`)
3. Copy the three output JSON files back to the bridge's `data/` directory:
   - `stream_mapping.json` — maps movie IDs to stream IDs and provider accounts
   - `category_mapping.json` — maps categories to movie IDs
   - `account_credentials.json` — maps account IDs to names (for UI labels)

**Verify the dump worked:**
```bash
# Should show counts for movies, accounts, and categories
python3 -c "import json; d=json.load(open('data/stream_mapping.json')); print(f'{len(d)} movies mapped')"
python3 -c "import json; d=json.load(open('data/category_mapping.json')); print(f'{len(d)} categories')"
```

### If user can't run docker exec

If the user doesn't have shell access to the Dispatcharr host or can't run `docker exec`, there is currently no API-only alternative. The mapping data MUST be extracted from Dispatcharr's Django database. Options:
- Ask someone with admin access to run the dump script for them
- Set up SSH access to the Dispatcharr host
- If Dispatcharr adds a VOD export API in the future, the bridge could use that instead

### Set up automatic updates (recommended)

The mapping files should be refreshed periodically as providers add/remove VOD content:
```bash
crontab -e
# Add this line (adjust paths and container name):
0 */6 * * * DISPATCHARR_CONTAINER=your-container BRIDGE_DATA_DIR=/path/to/data /path/to/dump_mappings.sh
```

---

## Phase 6: Configure Plex Library

1. Open Plex Web → Settings → Libraries → Add Library
2. Type: **Movies**
3. Add Folder: browse to `PLEX_VOD_DIR/Movies`
   - If bridge and Plex are on the same host, this is the same path
   - If on different hosts, use the path where the network share is mounted on the Plex host
4. Advanced settings:
   - Scanner: **Plex Movie**
   - Agent: **Plex Movie**

**IMPORTANT — Disable these to prevent Plex from downloading entire movies:**
- Generate video preview thumbnails: **OFF**
- Generate intro video markers: **OFF**  
- Generate credits video markers: **OFF**

These features download the full movie file for analysis, which opens provider connections and can burn movies.

5. Click Add Library
6. **Find the library section ID:**
   - Go to the new library in Plex Web
   - Look at the URL: `...library/sections/7/...` → the ID is `7`
   - Or: `curl -s "http://PLEX_IP:32400/library/sections?X-Plex-Token=TOKEN"` → find the `key` attribute

7. Update `.env` with the library ID:
   ```
   PLEX_LIBRARY_ID=7
   ```

8. Restart the bridge:
   ```bash
   docker compose restart
   ```

---

## Phase 7: First Sync and Test

1. Open the bridge UI: `http://BRIDGE_HOST:8585`
2. Go to the **Catalog** tab
3. Under **Providers**, select which M3U accounts to import from
4. Under **Categories**, select movie categories
5. Click **Sync Selected**
6. Watch the progress — TMDB enrichment and language detection run in the background
7. Switch to **Browse** to see imported movies
8. Click the ⚡ (lightning) button on a movie card to activate it
9. In Plex, scan the VOD library — the movie should appear
10. Try playing it in Plex

### What to check after activation:
- The bridge's **Proxy Log** tab should show:
  - `Served from cache (header)` — Plex probing, served from 8MB head cache
  - `Served from cache (tail)` — Plex probing end of file
  - No upstream connections during scan = cache is working
- When you play in Plex:
  - `Pipe started for movie...` — one persistent connection opened
  - `Served from pipe` — streaming data to Plex
  - `Plex idle 32s... buffer retained` — clean disconnect when you stop

---

## Phase 8: Post-Setup Recommendations

### Scheduled Refresh
In the bridge UI → Settings tab:
- Set refresh interval (4h / 6h / 8h / 12h)
- This auto-syncs the catalog, re-checks stream mappings, and runs dead scans

### Activate in Small Batches
Start with 5-10 movies. Watch the proxy logs. Confirm clean behavior before activating more. The probe throttle protects against Plex scan burns, but it's still good practice.

### Monitor the Health Dashboard
The bridge UI has a Health tab showing:
- Bridge status and response time
- Dispatcharr connectivity
- Plex API connectivity
- VPN IP (if configured)

---

## Common Deployment Patterns

Help the user identify which pattern matches their setup — this determines how to configure storage and networking.

### Pattern A: Everything on One Host
Bridge, Dispatcharr, and Plex all run on the same machine. Simplest setup.
- Shared storage is just a local path (e.g., `/opt/vod-plex-bridge/plex-vod`)
- `BRIDGE_HOST` = this machine's LAN IP
- Dump script runs locally with `BRIDGE_DATA_DIR=./data`
- Plex library path = same local path

### Pattern B: Bridge + Dispatcharr on One Host, Plex on Another
- Bridge and Dispatcharr share a host, Plex is elsewhere
- Shared storage needed between bridge host and Plex host (NFS, CIFS/SMB)
- Bridge writes `.strm` files to the local path, Plex reads them via the network mount
- The mount must be read-write for the bridge, at least read for Plex
- `BRIDGE_HOST` = LAN IP of the bridge/Dispatcharr host

### Pattern C: All on Different Hosts
- Each component on a separate machine
- Dump script runs on Dispatcharr host, JSON files copied to bridge host
- Shared storage between bridge host and Plex host
- `BRIDGE_HOST` = LAN IP of the bridge host
- Bridge needs network access to both Dispatcharr (API) and Plex (API)

### VPN Considerations
- If Dispatcharr runs behind a VPN container (gluetun, wireguard, etc.):
  - The bridge does NOT need to be behind the VPN
  - Use the port that gluetun exposes to the host network
  - All provider traffic is routed through Dispatcharr → VPN automatically
- The bridge's streaming port (8585) must be accessible from the Plex host on the LAN

---

## How the Probe Throttle Works

When Plex scans its library, it probes each `.strm` file to read metadata. For most movies, the 8 MB head cache and 256 KB tail cache satisfy these probes without opening a provider connection.

But some movies (especially MP4 with moov atom at a non-standard location) cause Plex to seek to uncached byte ranges. Without protection, this opens rapid provider connections that can burn the movie.

**The probe throttle prevents this:**
1. First uncached range request → allowed (Plex needs initial metadata)
2. Second request within 60 seconds → 3-minute cooldown starts
   - Cached ranges (head/tail) still served during cooldown
   - Uncached ranges get HTTP 416 (range not available)
3. After cooldown, if Plex probes again → movie marked dead and removed from Plex
4. User can manually resurrect from Dead Movies tab

If someone actually hits **Play** (not a scan), the throttle resets — real playback is never blocked.

---

## Troubleshooting Reference

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| Bridge can't reach Dispatcharr | Wrong IP or port, firewall, gluetun port mapping | Test with `curl` from inside the container |
| Plex can't play movies | `BRIDGE_HOST` wrong (set to 0.0.0.0 or 127.0.0.1) | Set to the machine's LAN IP |
| 0 categories in UI | Dump script hasn't run or produced empty files | Run `setup/dump_mappings.sh`, check JSON files |
| Movies in bridge but not Plex | Library path mismatch, Plex hasn't scanned | Verify paths match, trigger Plex library scan |
| Stream stops after ~10 min | Old version bug or missing nginx config | Update to latest, check Dispatcharr nginx for `uwsgi_buffering off` |
| Movie "burned" after activation | Plex scan opened too many connections | Update to v0.29.1+ (has probe throttle) |
| "Database is locked" | Should not occur in v0.27.1+ | Restart bridge container |

---

## Architecture Details (For Troubleshooting)

### Data Flow
```
Dispatcharr DB → dump_mappings.sh → JSON files → Bridge reads at startup
                                                     ↓
Dispatcharr API → Bridge scraper → SQLite DB → UI shows catalog
                                                     ↓
User activates → Bridge fetches head/tail cache → .strm/.nfo written
                                                     ↓
Plex scans → Bridge serves from cache (no provider connection)
                                                     ↓
User plays → Bridge opens 1 pipe through Dispatcharr → throttled to bitrate
                                                     ↓
User stops → 30s idle timeout → clean disconnect → buffer retained on disk
```

### Key Files Inside the Container
| Path | Purpose |
|------|---------|
| `/data/vod_bridge.db` | SQLite database (movies, settings, cache) |
| `/data/stream_mapping.json` | Movie → provider stream ID mapping |
| `/data/category_mapping.json` | Category → movie ID mapping |
| `/data/account_credentials.json` | Account names for UI labels |
| `/data/buffers/` | Disk buffer files during playback |
| `/plex-vod/Movies/` | .strm + .nfo + poster output for Plex |
| `/plex-vod/Movies/.dead/` | Movies marked dead (moved here) |

### Environment Variables (Full Reference)
| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DISPATCHARR_URL` | Yes | `http://localhost:9191` | Dispatcharr API endpoint |
| `PLEX_URL` | Yes | — | Plex server URL |
| `PLEX_TOKEN` | Yes | — | Plex auth token |
| `PLEX_LIBRARY_ID` | Yes | `7` | Plex library section for VOD |
| `BRIDGE_HOST` | Yes | `0.0.0.0` | LAN IP for .strm URLs |
| `BRIDGE_PORT` | No | `8585` | Bridge HTTP port |
| `TZ` | No | `UTC` | Container timezone |
| `TMDB_API_KEY` | No | — | TMDB metadata enrichment |
| `TMDB_READ_TOKEN` | No | — | TMDB v4 token (alternative) |
| `DISPATCHARR_API_KEY` | No | — | For VPN IP display |
| `DB_PATH` | No | `/data/vod_bridge.db` | SQLite path |
| `CATEGORY_MAPPING_FILE` | No | `/data/category_mapping.json` | Category dump path |
| `STREAM_MAPPING_FILE` | No | `/data/stream_mapping.json` | Stream dump path |
| `STRM_OUTPUT_DIR` | No | `/plex-vod/Movies` | Where .strm files go |
