# Installation Guide

This guide walks you through setting up VOD Plex Bridge from scratch. It assumes you have basic familiarity with Docker, your home network, and the Linux command line.

## What You're Building

```
Your Network:

┌──────────────────────────────────────────────────────────┐
│                     LAN (192.168.x.x)                    │
│                                                          │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────┐  │
│  │ Dispatcharr  │◄──►│  VOD Plex    │◄──►│   Plex     │  │
│  │ :9191        │    │  Bridge      │    │   :32400   │  │
│  │              │    │  :8585       │    │            │  │
│  │ (manages M3U │    │              │    │ (plays     │  │
│  │  accounts +  │    │ (catalogs,   │    │  movies)   │  │
│  │  proxies VOD)│    │  caches,     │    │            │  │
│  └──────┬───────┘    │  streams)    │    └─────┬──────┘  │
│         │            └──────┬───────┘          │         │
│         │                   │                  │         │
│    ┌────▼───────────────────▼──────────────────▼────┐    │
│    │          Shared Storage (NAS / local dir)       │    │
│    │          /plex-vod/Movies/                       │    │
│    │          (bridge writes .strm, Plex reads them) │    │
│    └─────────────────────────────────────────────────┘    │
│         │                                                │
│    ┌────▼────┐                                           │
│    │   VPN   │ (optional — only Dispatcharr needs this)  │
│    └────┬────┘                                           │
│         │                                                │
└─────────┼────────────────────────────────────────────────┘
          │
    ┌─────▼──────┐
    │ IPTV       │
    │ Provider   │
    └────────────┘
```

**Three main components talk to each other over your LAN:**

1. **Dispatcharr** — Your existing M3U manager. It already handles your IPTV accounts and has a VOD proxy built in. The bridge reads movie data from it and streams video through it.

2. **VOD Plex Bridge** — The new piece. A Docker container that catalogs movies, caches stream headers, and proxies video to Plex. This is what you're installing.

3. **Plex Media Server** — Your existing Plex instance. You point a Movies library at a shared directory where the bridge writes `.strm` files.

**Shared storage** is a directory that both the bridge and Plex can access. If they're on the same host, it's just a local folder. If they're on different hosts, use NFS or CIFS (network share).

---

## Prerequisites Checklist

Before starting, confirm you have:

- [ ] **Dispatcharr** installed, running, and accessible on your network (e.g., `http://192.168.x.x:9191`)
- [ ] **At least one M3U account** added to Dispatcharr with VOD content
- [ ] **Plex Media Server** installed and running (e.g., `http://192.168.x.x:32400`)
- [ ] **Docker** and **Docker Compose** installed on the host where you'll run the bridge
- [ ] **Shell access** (SSH or terminal) to the host where Dispatcharr's Docker container runs
- [ ] **A shared path** that both the bridge and Plex can read/write (if they're on different hosts, set up NFS/CIFS first)
- [ ] *(Optional)* A **TMDB API key** — get one free at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)

### Gathering Your Info

You'll need these values during setup. Write them down:

| Info | Where to find it | Example |
|------|-------------------|---------|
| Dispatcharr URL | Your Dispatcharr browser URL | `http://192.168.1.100:9191` |
| Dispatcharr container name | `docker ps` on the Dispatcharr host | `dispatcharr` |
| Plex URL | Your Plex browser URL (without `/web`) | `http://192.168.1.50:32400` |
| Plex token | [Plex support article](https://support.plex.tv/articles/204059436/) — look in the XML or URL | `abc123xyz...` |
| Bridge host IP | The LAN IP of the machine where you'll run the bridge | `192.168.1.100` |
| Shared storage path | Where both bridge and Plex can access `.strm` files | `/mnt/media/plex-vod` |

---

## Step 1: Clone the Repository

On the host where you'll run the bridge:

```bash
git clone https://github.com/knmplace/vod-plex-bridge.git
cd vod-plex-bridge
```

## Step 2: Configure Environment

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in your values:

```bash
# These are REQUIRED — the bridge won't work without them:
DISPATCHARR_URL=http://192.168.x.x:9191     # Your Dispatcharr IP and port
PLEX_URL=http://192.168.x.x:32400           # Your Plex IP and port
PLEX_TOKEN=your-plex-token                   # Your Plex auth token
PLEX_LIBRARY_ID=7                            # Library section ID (see Step 6)
BRIDGE_HOST=192.168.x.x                     # LAN IP of THIS machine
BRIDGE_PORT=8585                             # Port for the bridge UI

# These are optional but recommended:
TZ=America/New_York                          # Your timezone
TMDB_API_KEY=your-tmdb-key                   # For movie posters and metadata

# Paths — adjust if needed:
DATA_DIR=./data                              # Bridge database + mapping files
PLEX_VOD_DIR=./plex-vod                      # Where .strm files go
```

### Important: Understanding BRIDGE_HOST

`BRIDGE_HOST` is the most common source of "Plex can't play movies" issues. Here's why:

When you activate a movie, the bridge creates a `.strm` file containing a URL like:
```
http://192.168.1.100:8585/stream/movie_12345.mkv
```

Plex reads this URL and tries to connect to it. If `BRIDGE_HOST` is set to `0.0.0.0` or `127.0.0.1`, Plex won't be able to reach the bridge. **It must be the actual LAN IP of the machine running the bridge.**

### Important: Understanding PLEX_VOD_DIR

This is the directory where the bridge writes `.strm`, `.nfo`, and poster files. Plex reads from this same directory. Both must see the same files:

**Same host (bridge and Plex on the same machine):**
```
PLEX_VOD_DIR=/opt/vod-plex-bridge/plex-vod
# Plex library points to: /opt/vod-plex-bridge/plex-vod/Movies
```

**Different hosts (bridge on host A, Plex on host B):**
```
# Use a network share that both can access:
PLEX_VOD_DIR=/mnt/nas/plex-vod
# Plex library also points to: /mnt/nas/plex-vod/Movies
# (The mount path may differ on each host, but the files must be the same)
```

## Step 3: Prepare Docker Compose

```bash
cp docker-compose.example.yml docker-compose.yml
```

The default `docker-compose.yml` should work for most setups. Review the volume mounts:

```yaml
volumes:
  - ${DATA_DIR:-./data}:/data              # Database + mapping files
  - ${PLEX_VOD_DIR:-./plex-vod}:/plex-vod  # .strm output for Plex
```

**If Dispatcharr is behind a VPN container** (e.g., gluetun): Make sure `DISPATCHARR_URL` points to the port that's mapped through the VPN to the host. The bridge does NOT need to be behind the VPN — only Dispatcharr does.

## Step 4: Create Data Directories

```bash
mkdir -p data
mkdir -p plex-vod/Movies
```

Or if you changed the paths in `.env`:
```bash
mkdir -p /your/data/path
mkdir -p /your/plex-vod/path/Movies
```

## Step 5: Run the Mapping Dump Script

The bridge needs to know which movies are available from which providers. This information lives inside Dispatcharr's database, so we extract it using a script.

**Run this on the host where Dispatcharr's Docker container is running** (which might be the same host as the bridge, or might be different):

```bash
# If the bridge repo is on the same host as Dispatcharr:
DISPATCHARR_CONTAINER=your-dispatcharr-container-name \
BRIDGE_DATA_DIR=/path/to/bridge/data \
bash setup/dump_mappings.sh
```

**Finding your Dispatcharr container name:**
```bash
docker ps --format "table {{.Names}}\t{{.Image}}" | grep -i dispatcharr
```

**If the bridge is on a different host than Dispatcharr:**
1. Copy `setup/dump_mappings.sh` to the Dispatcharr host
2. Run it there, pointing `BRIDGE_DATA_DIR` to a local temp directory
3. Copy the three JSON files (`stream_mapping.json`, `category_mapping.json`, `account_credentials.json`) to the bridge's `data/` directory

The script outputs three files:
- `stream_mapping.json` — which movies are on which provider accounts
- `category_mapping.json` — which categories contain which movies
- `account_credentials.json` — provider account names for UI labels

### Set Up Automatic Updates (Recommended)

Your provider's catalog changes over time. Add a cron job to keep the mappings current:

```bash
# Edit crontab on the Dispatcharr host:
crontab -e

# Add this line (runs every 6 hours):
0 */6 * * * DISPATCHARR_CONTAINER=your-container BRIDGE_DATA_DIR=/path/to/bridge/data /path/to/dump_mappings.sh
```

## Step 6: Build and Start the Bridge

```bash
docker compose up -d --build
```

Verify it's running:
```bash
docker compose logs -f
# You should see: "Uvicorn running on http://0.0.0.0:8585"
# Press Ctrl+C to exit the log viewer
```

The bridge UI is now at `http://your-bridge-ip:8585`.

## Step 7: Configure Plex

1. Open Plex Web → **Settings** → **Libraries**
2. Click **Add Library** → Choose **Movies**
3. **Add Folder** → Browse to the path where the bridge writes `.strm` files
   - This is `PLEX_VOD_DIR/Movies` from your `.env`
   - Inside the Docker container, it's `/plex-vod/Movies`
   - On the host, it's whatever you set `PLEX_VOD_DIR` to
4. Under **Advanced**:
   - Scanner: **Plex Movie**
   - Agent: **Plex Movie**
5. Click **Add Library**

**Find your library section ID:**
- Go to the library in Plex Web
- Look at the URL: `.../library/sections/7/...` → the ID is `7`
- Set `PLEX_LIBRARY_ID=7` in your `.env` file
- Restart the bridge: `docker compose restart`

## Step 8: First Sync

1. Open the bridge UI at `http://your-bridge-ip:8585`
2. Go to the **Catalog** tab
3. Under **Providers**, select which M3U accounts to include
4. Under **Categories**, select the movie categories you want
5. Click **Sync Selected**
6. Wait for the sync to complete (progress shows in the status bar)
7. TMDB enrichment and language detection will run in the background
8. Switch to the **Browse** tab to see your movies
9. Click the ⚡ button on any movie card to activate it (adds it to Plex)
10. In Plex, scan your VOD library — the activated movies will appear

<!-- TODO: Add screenshots of the sync flow here -->

---

## Common Deployment Patterns

### Pattern A: Everything on One Host

The simplest setup. Bridge, Dispatcharr, and Plex all run on the same machine.

```
Host 192.168.1.100:
  ├── Dispatcharr (:9191)
  ├── VOD Plex Bridge (:8585)
  ├── Plex (:32400)
  └── /opt/vod-plex-bridge/plex-vod/Movies/  ← shared local directory
```

`.env`:
```
DISPATCHARR_URL=http://192.168.1.100:9191
PLEX_URL=http://192.168.1.100:32400
BRIDGE_HOST=192.168.1.100
PLEX_VOD_DIR=/opt/vod-plex-bridge/plex-vod
```

### Pattern B: Bridge + Dispatcharr on One Host, Plex on Another

Common when Plex runs on a dedicated media server.

```
Host A (192.168.1.100):              Host B (192.168.1.50):
  ├── Dispatcharr (:9191)              ├── Plex (:32400)
  ├── VOD Plex Bridge (:8585)          └── /mnt/nas/plex-vod/  ← NFS mount
  └── /mnt/nas/plex-vod/  ← NFS mount
```

Both hosts mount the same NAS share. The bridge writes `.strm` files, Plex reads them.

`.env`:
```
DISPATCHARR_URL=http://192.168.1.100:9191
PLEX_URL=http://192.168.1.50:32400
BRIDGE_HOST=192.168.1.100
PLEX_VOD_DIR=/mnt/nas/plex-vod
```

### Pattern C: All on Different Hosts

```
Host A (Dispatcharr):  192.168.1.100:9191
Host B (Bridge):       192.168.1.200:8585
Host C (Plex):         192.168.1.50:32400
NAS:                   192.168.1.10 → /Media/plex-vod/
```

All three hosts mount the NAS share. The dump script runs on Host A, copies JSON files to Host B's data directory.

---

## Dispatcharr Behind a VPN (gluetun)

If your Dispatcharr container routes through a VPN container (e.g., [gluetun](https://github.com/qdm12/gluetun)):

```
gluetun (:7000 control API)
  └── Dispatcharr (mapped port :9191 on host)
```

- Set `DISPATCHARR_URL` to the port mapped through gluetun to the host (e.g., `http://192.168.1.100:9191`)
- The bridge does **NOT** need to be behind the VPN
- All provider traffic already flows through Dispatcharr → gluetun → provider
- Optionally set `GLUETUN_API_URL=http://192.168.1.100:7000` to show the VPN IP in the bridge header

---

## Troubleshooting

### Bridge can't reach Dispatcharr
```bash
# Test from inside the bridge container:
docker exec vod-plex-bridge curl -s http://your-dispatcharr-ip:9191/api/vod/movies/?page=1&page_size=1
```
If this fails, check:
- Is Dispatcharr running? (`docker ps`)
- Is the URL correct? (use the LAN IP, not `localhost`, unless bridge uses `network_mode: host`)
- Is a firewall blocking the port?

### Plex can't play activated movies
1. Check what's inside the `.strm` file:
   ```bash
   cat plex-vod/Movies/SomeMovie*/SomeMovie*.strm
   ```
2. The URL inside should look like `http://192.168.x.x:8585/stream/...`
3. Try opening that URL in a browser — it should start downloading video
4. If it doesn't work, check `BRIDGE_HOST` in your `.env`

### Categories show 0 movies
- Did you run `setup/dump_mappings.sh`? Check that `data/category_mapping.json` exists and isn't empty
- Did you select at least one provider in the Catalog tab?

### Movies activate but don't appear in Plex
- Check that `.strm` files were created: `ls plex-vod/Movies/`
- Scan the library in Plex: Settings → Libraries → your VOD library → Scan
- Verify Plex's library folder matches `PLEX_VOD_DIR/Movies`

### Stream stops or movie gets "burned"
- Update to the latest version (streaming fixes were in v0.25.0+)
- Check that Dispatcharr's nginx config has these settings on the `/proxy/` location:
  ```nginx
  uwsgi_buffering off;
  uwsgi_read_timeout 300s;
  ```

### "Database is locked" errors
- This was fixed in v0.27.1 (singleton SQLite connection)
- If it still occurs, restart: `docker compose restart`
