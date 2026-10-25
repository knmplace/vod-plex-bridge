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

2. **VOD Plex Bridge** — The new piece. A Docker container that catalogs movies and series, caches stream headers for fast metadata probing, and streams playback from Dispatcharr's VOD proxy (one buffered connection per movie by default, or an optional redirect mode). This is what you're installing. It exposes a `/vod/` HTTP endpoint that lists activated movies as virtual `.mp4` files, and a `/vod-series/` endpoint with the activated series as show/season folders.

3. **Plex Media Server** — Your existing Plex instance. **rclone** on the Plex host mounts the bridge's `/vod/` (and `/vod-series/`) endpoints as local FUSE directories. Plex sees them as normal folders of movie and episode files.

> **rclone is required, and you install it yourself** on the Plex host (Step 7). A future version will have rclone built in.

**No shared storage (NFS/CIFS) is required** between the bridge and Plex. rclone handles the connection over HTTP. The Plex host just needs to reach the bridge's port (default 8585) over the LAN.

---

## Prerequisites Checklist

Before starting, confirm you have:

- [ ] **Dispatcharr** installed, running, and accessible on your network (e.g., `http://192.168.x.x:9191`)
- [ ] **At least one M3U account** added to Dispatcharr with VOD content
- [ ] A **Dispatcharr API key** (created in Dispatcharr) — the bridge reads the catalog through Dispatcharr's API
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
| Dispatcharr API key | Created in Dispatcharr | `dsp_...` |
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
# REQUIRED in .env:
BRIDGE_HOST=192.168.x.x                     # LAN IP of THIS machine
BRIDGE_PORT=8585                             # Port for the bridge UI

# Recommended:
SETTINGS_KEY=a-long-random-string            # Encrypts secrets saved in Settings (keep it safe)

# Optional here — you can enter these in the app's Settings tab instead (Step 9):
DISPATCHARR_URL=http://192.168.x.x:9191     # Your Dispatcharr IP and port
DISPATCHARR_API_KEY=your-dispatcharr-key     # Dispatcharr API key
PLEX_URL=http://192.168.x.x:32400           # Your Plex IP and port
PLEX_TOKEN=your-plex-token                   # Your Plex auth token
PLEX_LIBRARY_ID=7                            # Movie library section ID (see Step 8)
TMDB_API_KEY=your-tmdb-key                   # For posters and metadata

# Optional:
TZ=America/New_York                          # Your timezone
DATA_DIR=./data                              # Bridge database + mapping files
PLEX_VOD_DIR=./plex-vod                      # Where .strm files go
```

**How `.env` and Settings work together:** Plex, TMDB and streaming values saved in the app's
**Settings** tab override `.env`; clearing a field falls back to `.env`. The `DISPATCHARR_*`
values are copied once, on first start, into **Settings → Dispatcharr** as the primary instance;
after that the instance list is used and the variables are ignored. If `SETTINGS_KEY` is not set,
the bridge generates a key file in the data folder instead.

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

**If Dispatcharr is behind a VPN container** (e.g., gluetun): Make sure the Dispatcharr URL points to the port that's mapped through the VPN to the host. The bridge does NOT need to be behind the VPN — only Dispatcharr does.

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
BRIDGE_DATA_DIR=/path/to/bridge/data bash setup/dump_mappings.sh
```

The script finds the Dispatcharr container by itself (the first running container with
"dispatcharr" in its name). If you run more than one, name it explicitly:

```bash
docker ps --format "table {{.Names}}\t{{.Image}}" | grep -i dispatcharr
DISPATCHARR_CONTAINER=your-dispatcharr-container-name \
BRIDGE_DATA_DIR=/path/to/bridge/data \
bash setup/dump_mappings.sh
```

The script writes five files to the bridge's data folder:
- `stream_mapping.json` — which movies are on which provider accounts
- `category_mapping.json` — which movie categories contain which movies
- `series_category_mapping.json` — which series categories contain which series
- `movie_metadata.json` — Dispatcharr's own movie details, used at import
- `account_credentials.json` — provider account names for UI labels

**If the bridge is on a different host than Dispatcharr:**
1. Copy `setup/dump_mappings.sh` to the Dispatcharr host
2. Run it there, pointing `BRIDGE_DATA_DIR` to a local temp directory
3. Copy all five JSON files to the bridge's `data/` directory

### Set Up Automatic Updates (Recommended)

Your provider's catalog changes over time, and the bridge's **Daily Run** (6am) starts by
asking for a fresh dump. Two cron jobs on the Dispatcharr host keep the files current.

**1. A regular dump** (every 12 hours):

```bash
crontab -e
# Add:
0 */12 * * * BRIDGE_DATA_DIR=/path/to/bridge/data bash /path/to/dump_mappings.sh >> /var/log/vod-bridge-dump.log 2>&1
```

**2. A trigger check**, so the Daily Run and the manual Run Now get a fresh dump on request.
The bridge asks for a dump by creating a `.dump-trigger` file in its data folder, then waits up
to 3 minutes for `category_mapping.json` to change. This small script, run every minute,
answers that request:

```bash
cat > /usr/local/bin/vod-bridge-trigger-check.sh << 'EOF'
#!/bin/bash
TRIGGER="/path/to/bridge/data/.dump-trigger"
if [ -f "$TRIGGER" ]; then
    rm -f "$TRIGGER"
    BRIDGE_DATA_DIR=/path/to/bridge/data bash /path/to/dump_mappings.sh >> /var/log/vod-bridge-dump.log 2>&1
fi
EOF
chmod +x /usr/local/bin/vod-bridge-trigger-check.sh

crontab -e
# Add:
* * * * * /usr/local/bin/vod-bridge-trigger-check.sh
```

> The trigger check only works when the bridge's data folder is on the same host as
> Dispatcharr. If they are on different hosts, rely on the regular dump plus a copy job.
> When no fresh dump arrives, the Daily Run report shows a red **STALE dump** warning and
> continues with the previous files.

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

The bridge UI is now at `http://your-bridge-ip:8585`. On your first visit the sign-in page
asks you to create the admin account; after that, every page except the `/vod/`, `/vod-series/`
and `/stream/` paths (which rclone and Plex use) requires sign-in.

## Step 7: Set Up rclone on the Plex Host

The bridge exposes a `/vod/` HTTP endpoint that lists activated movies as virtual `.mp4` files, and a `/vod-series/` endpoint with activated series as show/season folders. rclone mounts these endpoints as FUSE filesystems on the Plex server, so Plex sees normal directories of movie and episode files.

> rclone is not bundled with the bridge today; you install and run it on the Plex host. A future version will have rclone built in.

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

[vodbridgeseries]
type = http
url = http://BRIDGE_IP:8585/vod-series/
EOF
```

The `vodbridgeseries` remote is only needed if you use TV series. Replace `BRIDGE_IP` with your bridge host's LAN IP (the `BRIDGE_HOST` value from Step 2).

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

**For series**, create a second service the same way, with these differences:

```bash
cat > /etc/systemd/system/rclone-vodbridge-series.service << 'EOF'
[Unit]
Description=rclone VOD Bridge series FUSE mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p /mnt/vod-bridge-series
ExecStart=/bin/rclone mount vodbridgeseries: /mnt/vod-bridge-series --allow-other --dir-cache-time 1m --vfs-cache-mode full --vfs-cache-max-age 10m
ExecStop=/bin/fusermount -u /mnt/vod-bridge-series
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rclone-vodbridge-series
```

If you mount at different paths, set **Settings → Plex → Movie mount root / Series mount root**
in the bridge to match, so it can tell Plex where each folder is.

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
- Enter it in the bridge under **Settings → Plex → Movie library ID** (applies right away, no restart)

**For TV series** (optional): series are sent to Plex TV Shows libraries. In the bridge's
**Series** tab, each series category can be pointed at an existing Plex TV library or at
**+ Create new Plex library**, which creates one under `/mnt/vod-bridge-series`. Categories
without their own library use the fallback **Settings → Plex → Series library ID**. Turn off the
same video-analysis options listed above for these libraries.

## Step 9: First Sync

1. Open the bridge UI at `http://your-bridge-ip:8585` and create your account
2. Open **Settings** and fill in each section, using its **Test** button:
   - **Dispatcharr** — add your instance (URL and API key). The first one becomes primary.
   - **Plex** — URL, token and the movie library ID from Step 8
   - **TMDB** — API key or read token (optional)
3. Go to the **Catalog** tab and click **Reload Categories** if the list is empty
4. Under **Providers**, select which M3U accounts to include
5. Under **Categories**, select the movie categories you want
6. Click **Sync Selected** and wait for it to finish
7. TMDB enrichment and language detection run in the background (and in every Daily Run)
8. Switch to the **Movies** tab to see your movies
9. Click **Activate** on a movie card (or select several and use the selection bar) to add it to Plex
10. In Plex, scan your VOD library — the activated movies will appear

For series, use the **Series** tab the same way: pick categories, then activate shows, seasons or
episodes. The [User Guide](docs/USER_GUIDE.md) covers every tab in detail.

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

The dump script runs on Host A, and its JSON files are copied to Host B's data directory (the trigger check from Step 5 can't be used in this layout). rclone on Host C mounts the bridge's `/vod/` endpoint from Host B.

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

---

## Troubleshooting

### Bridge can't reach Dispatcharr
```bash
# Test from inside the bridge container:
docker exec vod-plex-bridge curl -s -H "X-API-Key: your-dispatcharr-key" "http://your-dispatcharr-ip:9191/api/vod/movies/?page=1&page_size=1"
```
Or use the **Test** button in **Settings → Dispatcharr**.
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

### Daily Run report says "STALE dump"
- The bridge asked for a fresh dump and none arrived within 3 minutes
- Check the trigger-check cron from Step 5 is installed, and look at `/var/log/vod-bridge-dump.log` on the Dispatcharr host
- The trigger only works when the bridge's data folder is on the Dispatcharr host

### Movies activate but don't appear in Plex
- Check that the rclone mount shows the movie: `ls /mnt/vod-bridge/` on the Plex host
- Scan the library in Plex: Settings → Libraries → your VOD library → Scan
- Verify Plex's library folder points to the rclone mount path (e.g., `/mnt/vod-bridge`)

### Stream stops or movie gets "burned"
- The bridge maintains a single persistent connection to Dispatcharr with adaptive throttling. Update to the latest version.
- Check that Dispatcharr's nginx config has `uwsgi_buffering off` and `uwsgi_read_timeout 300s` on the `/proxy/` location.

### Plex plays briefly then stops
- Check the bridge logs for "Plex idle" messages — the bridge disconnects from the provider after 30 seconds of no Plex reads. This is normal; Plex buffers locally.
- If the movie stops after ~10 minutes, check that Dispatcharr's nginx `/proxy/` location has `uwsgi_buffering off` and `uwsgi_read_timeout 300s`.

### "Database is locked" errors
- This was fixed in v0.27.1 (singleton SQLite connection)
- If it still occurs, restart the container
