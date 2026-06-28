# Installation Guide

This guide walks you through setting up VOD Plex Bridge from scratch. It assumes you have basic familiarity with Docker, your home network, and the Linux command line.

## What You're Building

```
Your Network:

┌──────────────────────────────────────────────────────────────┐
│                     LAN (192.168.x.x)                        │
│                                                              │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │ Dispatcharr  │◄──►│  VOD Plex    │◄──►│   Plex :32400  │  │
│  │ :9191        │    │  Bridge      │    │                │  │
│  │              │    │  :8585       │    │  rclone FUSE   │  │
│  │ (manages M3U │    │              │    │  mount from    │  │
│  │  accounts +  │    │ (catalogs,   │    │  Bridge /vod/  │  │
│  │  proxies VOD)│    │  caches,     │    │  → /mnt/       │  │
│  └──────┬───────┘    │  streams)    │    │    vod-bridge/ │  │
│         │            └──────────────┘    └────────────────┘  │
│         │                   │ serves /vod/       ▲           │
│         │                   │ HTTP endpoint       │           │
│         │                   └────────────────────┘           │
│         │                    rclone reads over HTTP           │
│    ┌────▼────┐                                               │
│    │   VPN   │ (optional — only Dispatcharr needs this)      │
│    └────┬────┘                                               │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │
    ┌─────▼──────┐
    │ IPTV       │
    │ Provider   │
    └────────────┘
```

**Three main components talk to each other over your LAN:**

1. **Dispatcharr** — Your existing M3U manager. It already handles your IPTV accounts and has a VOD proxy built in. The bridge reads movie data from it and streams video through it.

2. **VOD Plex Bridge** — The new piece. A Docker container that catalogs movies, caches stream headers for fast metadata probing, and redirects playback to Dispatcharr's VOD proxy. This is what you're installing. It exposes a `/vod/` HTTP endpoint that lists activated movies as virtual `.mp4` files.

3. **Plex Media Server** — Your existing Plex instance. **rclone** on the Plex host mounts the bridge's `/vod/` endpoint as a local FUSE directory. Plex sees this as a normal folder of movie files.

**No shared storage (NFS/CIFS) is required** between the bridge and Plex. rclone handles the connection over HTTP. The Plex host just needs to reach the bridge's port (default 8585) over the LAN.

---

## Prerequisites Checklist

Before starting, confirm you have:

- [ ] **Dispatcharr** installed, running, and accessible on your network (e.g., `http://192.168.x.x:9191`)
- [ ] **At least one M3U account** added to Dispatcharr with VOD content
- [ ] **Plex Media Server** installed and running (e.g., `http://192.168.x.x:32400`)
- [ ] **Docker** and **Docker Compose** installed on the host where you'll run the bridge
- [ ] **Shell access** (SSH or terminal) to the host where Dispatcharr's Docker container runs
- [ ] **rclone** installed on the **Plex host** (not the bridge host) — [rclone.org/install](https://rclone.org/install/)
- [ ] **FUSE** installed on the Plex host — `apt install fuse3` (Debian/Ubuntu)
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
REDIRECT_MODE=false                          # false = pipe mode (recommended for rclone mounts)
                                             # true = 302 redirect (does NOT work with rclone)

# Paths — adjust if needed:
DATA_DIR=./data                              # Bridge database + mapping files
PLEX_VOD_DIR=./plex-vod                      # Where .strm files go
```

### Important: Understanding BRIDGE_HOST

`BRIDGE_HOST` is the most common source of "Plex can't play movies" issues. This is the LAN IP that rclone on the Plex host uses to reach the bridge. If `BRIDGE_HOST` is set to `0.0.0.0` or `127.0.0.1`, the rclone mount and Plex playback will fail. **It must be the actual LAN IP of the machine running the bridge.**

### Important: Understanding PLEX_VOD_DIR

This is a local directory inside the bridge container used for internal `.strm`/`.nfo` file storage. **Plex does NOT read from this directory directly.** Plex reads movies through the rclone FUSE mount, which connects to the bridge's `/vod/` HTTP endpoint. The default value (`./plex-vod`) is fine for most setups.

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

## Step 7: Set Up rclone on the Plex Host

The bridge exposes a `/vod/` HTTP endpoint that lists activated movies as virtual `.mp4` files. rclone mounts this endpoint as a FUSE filesystem on the Plex server, so Plex sees a normal directory of movie files.

**Run all of the following on the Plex host (NOT the bridge host).**

### Install rclone and FUSE

```bash
apt install rclone fuse3
```

Or install the latest rclone from [rclone.org/install](https://rclone.org/install/).

### Configure rclone

```bash
mkdir -p /root/.config/rclone
cat > /root/.config/rclone/rclone.conf << EOF
[vodbridge]
type = http
url = http://BRIDGE_IP:8585/vod/
EOF
```

Replace `BRIDGE_IP` with your bridge host's LAN IP (the `BRIDGE_HOST` value from Step 2).

**Test the connection:**
```bash
rclone ls vodbridge:
# Should list any activated movies (empty if none activated yet — that's OK)
```

### Create a systemd service for automatic startup

```bash
cat > /etc/systemd/system/rclone-vodbridge.service << 'EOF'
[Unit]
Description=rclone VOD Bridge FUSE mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p /mnt/vod-bridge
ExecStart=/bin/rclone mount vodbridge: /mnt/vod-bridge --allow-other --dir-cache-time 1m --vfs-cache-mode full --vfs-cache-max-age 10m
ExecStop=/bin/fusermount -u /mnt/vod-bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable rclone-vodbridge
systemctl start rclone-vodbridge
```

**Verify the mount:**
```bash
ls /mnt/vod-bridge/
# Shows .mp4 files for any activated movies
```

### Important: FUSE permissions

If Plex runs as a non-root user (e.g., `plex`), it needs permission to read the FUSE mount. The `--allow-other` flag handles this, but FUSE must be configured to allow it:

```bash
# Uncomment 'user_allow_other' in /etc/fuse.conf:
sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf
```

## Step 8: Configure Plex

1. Open Plex Web → **Settings** → **Libraries**
2. Click **Add Library** → Choose **Movies**
3. **Add Folder** → Browse to **`/mnt/vod-bridge`** (the rclone mount point from Step 7)
4. Under **Advanced**:
   - Scanner: **Plex Movie**
   - Agent: **Plex Movie**
5. Click **Add Library**

**IMPORTANT — Disable these to prevent Plex from downloading entire movies for analysis:**
- Generate video preview thumbnails: **OFF**
- Generate intro video markers: **OFF**
- Generate credits video markers: **OFF**

**Find your library section ID:**
- Go to the library in Plex Web
- Look at the URL: `.../library/sections/7/...` → the ID is `7`
- Set `PLEX_LIBRARY_ID=7` in your `.env` file
- Restart the bridge: `docker compose restart`

## Step 9: First Sync

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
  └── rclone mount: /mnt/vod-bridge/ → http://192.168.1.100:8585/vod/
```

`.env`:
```
DISPATCHARR_URL=http://192.168.1.100:9191
PLEX_URL=http://192.168.1.100:32400
BRIDGE_HOST=192.168.1.100
```

### Pattern B: Bridge + Dispatcharr on One Host, Plex on Another

Common when Plex runs on a dedicated media server.

```
Host A (192.168.1.100):              Host B (192.168.1.50):
  ├── Dispatcharr (:9191)              ├── Plex (:32400)
  └── VOD Plex Bridge (:8585)          └── rclone mount: /mnt/vod-bridge/
                                             → http://192.168.1.100:8585/vod/
```

No shared storage needed. rclone on Host B connects to the bridge on Host A over HTTP.

`.env`:
```
DISPATCHARR_URL=http://192.168.1.100:9191
PLEX_URL=http://192.168.1.50:32400
BRIDGE_HOST=192.168.1.100
```

### Pattern C: All on Different Hosts

```
Host A (Dispatcharr):  192.168.1.100:9191
Host B (Bridge):       192.168.1.200:8585
Host C (Plex):         192.168.1.50:32400
  └── rclone mount: /mnt/vod-bridge/ → http://192.168.1.200:8585/vod/
```

The dump script runs on Host A, copies JSON files to Host B's data directory. rclone on Host C mounts the bridge's `/vod/` endpoint from Host B.

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
1. Check the rclone mount is active: `ls /mnt/vod-bridge/` on the Plex host
2. Check the bridge is reachable: `curl http://BRIDGE_IP:8585/vod/` from the Plex host
3. If rclone mount is empty, check that you have activated movies in the bridge UI
4. If the bridge is unreachable, check `BRIDGE_HOST` in your `.env` and Docker port mapping

### rclone mount issues
- **Mount hangs or is empty:** Bridge is down or unreachable. Test with `curl http://BRIDGE_IP:8585/version`
- **Permission denied:** Check `--allow-other` flag and `user_allow_other` in `/etc/fuse.conf`
- **Movies don't appear after activation:** rclone caches the directory listing. Wait up to 1 minute (`--dir-cache-time 1m`) or restart the service: `systemctl restart rclone-vodbridge`

### Categories show 0 movies
- Did you run `setup/dump_mappings.sh`? Check that `data/category_mapping.json` exists and isn't empty
- Did you select at least one provider in the Catalog tab?

### Movies activate but don't appear in Plex
- Check that the rclone mount shows the movie: `ls /mnt/vod-bridge/` on the Plex host
- Scan the library in Plex: Settings → Libraries → your VOD library → Scan
- Verify Plex's library folder points to the rclone mount path (e.g., `/mnt/vod-bridge`)

### Stream stops or movie gets "burned"
- Ensure you're using pipe mode (default): `REDIRECT_MODE=false`. The bridge maintains a single persistent connection to Dispatcharr with adaptive throttling.
- Do NOT use redirect mode (`REDIRECT_MODE=true`) with rclone FUSE mounts — rclone doesn't follow 302 redirects and creates rapid session cycling that burns movies.
- Update to the latest version (streaming fixes in v0.25.0+) and check that Dispatcharr's nginx config has `uwsgi_buffering off` on the `/proxy/` location.

### Plex plays briefly then stops
- Make sure `REDIRECT_MODE=false` (pipe mode). Redirect mode does not work with rclone mounts.
- Check the bridge logs for "Plex idle" messages — the bridge disconnects from the provider after 30 seconds of no Plex reads. This is normal; Plex buffers locally.
- If the movie stops after ~10 minutes, check that Dispatcharr's nginx `/proxy/` location has `uwsgi_buffering off` and `uwsgi_read_timeout 300s`.

### "Database is locked" errors
- This was fixed in v0.27.1 (singleton SQLite connection)
- If it still occurs, restart the container via Portainer
