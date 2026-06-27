# Installation Guide

## Prerequisites

Before starting, ensure you have:

1. **Dispatcharr** running and accessible — note the URL and port (e.g., `http://192.168.x.x:9191`)
2. **Plex Media Server** running — note the URL, token, and the library section ID you want to use
3. **Docker** and **Docker Compose** installed on the host where the bridge will run
4. **Shared storage** — a directory that both the bridge container and Plex can access (for .strm files)
5. **TMDB API key** (optional) — get one free at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)

## Step 1: Clone the Repository

```bash
git clone https://github.com/knmplace/vod-plex-bridge.git
cd vod-plex-bridge
```

## Step 2: Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your values. The key settings:

| Variable | Description | Example |
|----------|-------------|---------|
| `DISPATCHARR_URL` | Dispatcharr URL accessible from the bridge container | `http://192.168.x.x:9191` |
| `DISPATCHARR_API_KEY` | API key from Dispatcharr (if required) | |
| `PLEX_URL` | Plex server URL | `http://192.168.x.x:32400` |
| `PLEX_TOKEN` | Plex authentication token ([how to find](https://support.plex.tv/articles/204059436/)) | |
| `PLEX_LIBRARY_ID` | Plex library section ID for VOD movies | `7` |
| `BRIDGE_HOST` | LAN IP of the machine running the bridge | `192.168.x.x` |
| `BRIDGE_PORT` | Port the bridge listens on | `8585` |
| `DATA_DIR` | Host path for bridge database and config files | `/opt/vod-plex-bridge/data` |
| `PLEX_VOD_DIR` | Host path where .strm files are written — Plex reads from here | `/mnt/media/plex-vod` |
| `TMDB_API_KEY` | TMDB API key for metadata enrichment | |

### Important Notes on Paths

- **`BRIDGE_HOST`** must be the LAN IP that Plex can reach. The .strm files contain URLs like `http://BRIDGE_HOST:BRIDGE_PORT/stream/12345.mp4` — if Plex can't reach this address, playback will fail.

- **`PLEX_VOD_DIR`** must be accessible to both the bridge container (as a volume mount) and Plex. Common setups:
  - **Same host**: Both containers mount the same host directory
  - **NAS/NFS**: Both mount a shared network path
  - **Different hosts**: Use NFS, CIFS, or similar to make the path available to both

- **`DISPATCHARR_URL`** should be the URL as reachable from inside the bridge container. If the bridge and Dispatcharr are on the same host, `http://localhost:<port>` works if you use `network_mode: host`, otherwise use the host's LAN IP.

## Step 3: Prepare Docker Compose

```bash
cp docker-compose.example.yml docker-compose.yml
```

Review the volume mounts and adjust if needed:

```yaml
volumes:
  - ${DATA_DIR:-./data}:/data          # Bridge database + mappings
  - ${PLEX_VOD_DIR:-./plex-vod}:/plex-vod  # .strm output → Plex reads this
```

If Dispatcharr and the bridge are on the same Docker network, you can add:

```yaml
networks:
  default:
    external: true
    name: your-dispatcharr-network
```

## Step 4: Create Data Directories

```bash
mkdir -p ${DATA_DIR:-./data}
mkdir -p ${PLEX_VOD_DIR:-./plex-vod}/Movies
```

## Step 5: Build and Start

```bash
docker compose up -d --build
```

Verify the container is running:

```bash
docker compose logs -f
```

The bridge UI will be available at `http://your-host-ip:8585`.

## Step 6: Generate Stream Mappings

The bridge needs a mapping file that links each VOD movie to its provider stream ID. Run the included dump script on the host where Dispatcharr's Docker container runs:

```bash
# Edit the script's variables if your container name differs
DISPATCHARR_CONTAINER=your-dispatcharr-container-name \
BRIDGE_DATA_DIR=/path/to/bridge/data \
bash dump_stream_mapping.sh
```

For automatic updates, add a cron job:

```bash
# Every 6 hours
0 */6 * * * DISPATCHARR_CONTAINER=dispatcharr BRIDGE_DATA_DIR=/opt/vod-plex-bridge/data /opt/vod-plex-bridge/dump_stream_mapping.sh
```

## Step 7: Configure Plex

1. In Plex, create a new **Movies** library (or use an existing one)
2. Point it at the path where the bridge writes .strm files (`PLEX_VOD_DIR/Movies`)
3. Under Advanced settings:
   - Set the scanner to **Plex Movie**
   - Set the agent to **Plex Movie**
4. Note the library section ID from the URL (e.g., `/library/sections/7` → ID is `7`)
5. Set `PLEX_LIBRARY_ID` in your `.env` to this value

## Step 8: First Sync

1. Open the bridge UI at `http://your-host-ip:8585`
2. Click **Reload Categories** to load available VOD categories from Dispatcharr
3. Click **Sync Selected** to pull movie metadata from Dispatcharr
4. Optionally click **Detect All Languages** to identify audio languages
5. Browse movies, activate the ones you want in Plex
6. Scan or refresh your Plex library — activated movies will appear

## Network Topology

The bridge connects to Dispatcharr's API to sync catalog data and proxy video streams. It does **not** connect directly to your IPTV provider. All provider traffic flows through Dispatcharr:

```
[Provider] ←→ [Dispatcharr] ←→ [Bridge] ←→ [Plex]
```

However you have Dispatcharr configured (direct connection, behind a VPN, etc.), the bridge uses that same path. No additional VPN or routing configuration is needed for the bridge itself.

If `DISPATCHARR_API_KEY` is set, the bridge will display Dispatcharr's public IP address in the header (fetched from Dispatcharr's environment API).

## Updating

```bash
cd vod-plex-bridge
git pull
docker compose up -d --build
```

Your database and settings persist in the `DATA_DIR` volume.

## Troubleshooting

### Bridge can't reach Dispatcharr
- Verify `DISPATCHARR_URL` is correct and reachable from inside the container
- If using `localhost`, ensure `network_mode: host` or use the host's LAN IP instead
- Check Dispatcharr is running: `curl http://your-dispatcharr-url/api/vod/movies/?page=1&page_size=1`

### Plex can't play movies
- Verify `BRIDGE_HOST` is set to the LAN IP (not `0.0.0.0` or `127.0.0.1`)
- Check the .strm file contents: `cat /path/to/plex-vod/Movies/SomeMovie/SomeMovie.strm`
- The URL inside should be reachable from Plex: `http://BRIDGE_HOST:BRIDGE_PORT/stream/...`

### Movies show in bridge but not Plex
- Ensure Plex library points to the correct path
- Scan the library in Plex (Settings → Libraries → Scan Library Files)
- Check that activated movies have .strm files: `ls /path/to/plex-vod/Movies/`

### "Database is locked" errors
- The bridge uses a singleton SQLite connection — this should not occur in v0.27.1+
- If it does, restart the container: `docker compose restart`

### Stream stops after ~10 minutes
- This was fixed in v0.25.0+ — ensure you're running the latest version
- Check Dispatcharr's nginx config has `uwsgi_buffering off` and `uwsgi_read_timeout 300s` on the `/proxy/` location
