<p align="center"><img src="docs/images/logo.png" alt="VOD Plex Bridge logo" width="120"></p>

# VOD Plex Bridge

A self-hosted Docker application that bridges Video On Demand (VOD) content from [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) into [Plex Media Server](https://www.plex.tv/). Browse, activate, and stream VOD movies and TV series directly through the Plex interface — no manual file management required.

![Movies tab](docs/images/movies.png)

## How It Works

The bridge sits between Dispatcharr and Plex. It reads your VOD catalog from Dispatcharr, lets you pick which movies you want in Plex, and handles all the streaming plumbing automatically.

```mermaid
graph LR
    A[IPTV Provider] <-->|"M3U streams"| B[Dispatcharr]
    B <-->|"VOD proxy API"| C[VOD Plex Bridge]
    C -->|"HTTP /vod/ endpoint"| D["rclone FUSE mount<br/>(on Plex host)"]
    D -->|"virtual .mp4 files"| E[Plex Media Server]
    E -->|"stream request"| C

    style A fill:#e74c3c,color:#fff
    style B fill:#3498db,color:#fff
    style C fill:#2ecc71,color:#fff
    style D fill:#f39c12,color:#fff
    style E fill:#9b59b6,color:#fff
```

**Key points:**
- The bridge **never** connects directly to your IPTV provider — all traffic flows through Dispatcharr
- However Dispatcharr routes its traffic (VPN, direct, etc.), the bridge inherits that routing
- The bridge serves activated movies as virtual `.mp4` files via an HTTP endpoint (`/vod/`), movies in a movie category under `/vod-movies/<category>/`, and activated series episodes under `/vod-series/`
- **rclone** on the Plex host mounts this endpoint as a FUSE filesystem — Plex sees a normal directory of movie files
- No shared storage (NFS/CIFS) is required between the bridge and Plex — rclone handles it over HTTP

## Features

- **VOD Catalog Browser** — Browse and search your provider's movie catalog (Movies tab) with multi-select filters for language, category, genre, year, provider and activation state
- **TV Series** — Pick series categories, activate whole shows, seasons or single episodes, and send each category to its own Plex TV library
- **Movie Categories** — Split movies into categories (kids, 4K, ...), each with its own Plex Movies library the bridge creates for you. Map provider categories to them for automatic placement, and move active movies between them
- **One-Click Activation** — Activate movies to instantly add them to your Plex library with poster art, genres, and metadata
- **Pipe Streaming** (default) — Single persistent connection to Dispatcharr with adaptive bitrate throttling and local disk buffering
- **Redirect Mode** (optional) — Sends Plex straight to Dispatcharr's VOD proxy with a 302, with a per-title session cap
- **Head/Tail Caching** — Caches the first 8 MB and last 256 KB of each activated movie so Plex can probe metadata without opening a provider connection
- **TMDB Enrichment** — Automatically fills in titles, genres, descriptions, posters, runtime, director, country, and cast for movies and series from [The Movie Database](https://www.themoviedb.org/), checking a local TMDB library first (TMDB Local tab) before calling the API. TMDB's title wins over the provider's, so names stay the same whatever languages or countries the provider uses
- **Multi-Provider Support** — Works with multiple M3U accounts, shows which providers carry each movie
- **Language Detection** — Background detection of audio language via TMDB, with filters in the browse UI
- **Now Playing Dashboard** — Real-time view of all Plex sessions (bridge content and local/live TV) with codec, resolution, bandwidth, and transcode details, plus Jellyfin and Emby sessions when they are set up
- **Health Dashboard** — Real-time status of Bridge, Dispatcharr, Plex, and Jellyfin/Emby with response times
- **Tidy UI** — Every section collapses (closed by default, remembered per browser), and a Display size setting scales the app for large screens
- **Daily Run** — Every day at 6am (plus a manual Run Now) the bridge refreshes mappings, categories and the catalog, enriches metadata and detects languages; each run's report is kept under Maintenance → Daily Updates
- **Settings in the App** — Dispatcharr instances, Plex, TMDB and streaming options are set in the Settings tab, with a Test button per service; secrets are stored encrypted
- **Sign-in** — The web UI requires an account, created on first start
- **Maintenance** — Dead movies, proxy log archives and run history in one tab
- **Dead Movie Tracking** — Automatically detects and removes movies no longer available from your provider
- **Persistent Buffers** — In pipe mode, downloaded data stays on disk between play sessions for resume
- **Error Screens** — When a movie can't play (dead, busy, removed), Plex shows a user-friendly video message instead of buffering
- **Activation Gating** — Only activated movies are visible to Plex. No mass scanning of your provider's catalog.

## How Streaming Works

```mermaid
sequenceDiagram
    participant P as Plex
    participant B as Bridge
    participant D as Dispatcharr
    participant S as Provider

    P->>B: HEAD /vod/Movie (2020) {tmdb-456} [123].mp4
    B-->>P: 200 OK (file size, content type)
    P->>B: GET (Range: bytes=0-8MB)
    B-->>P: 206 Partial (from head cache — instant)
    P->>B: GET (Range: bytes=tail)
    B-->>P: 206 Partial (from tail cache — instant)
    P->>B: GET (Range: playback start)
    B->>D: Single persistent GET (through VPN)
    D->>S: Stream request
    S-->>D: Video bytes
    D-->>B: Streamed response
    B-->>P: Adaptive throttled delivery
    Note over B: Pipe mode: one persistent connection<br/>Adaptive throttle + disk buffer
```

The bridge maintains a single persistent connection to Dispatcharr per movie, with adaptive bitrate throttling and local disk buffering. Head/tail caches (8MB + 256KB) serve Plex's initial probing requests instantly with zero provider connections. Playback data is buffered to disk so Plex can read at its own pace.

## Architecture Overview

```mermaid
graph TB
    subgraph "Bridge Host"
        subgraph "Docker"
            B["VOD Plex Bridge<br/>:8585"]
            D["Dispatcharr<br/>:9191"]
            V["VPN Container<br/>(optional, e.g. gluetun)"]
        end
        DB[("SQLite DB<br/>/data/vod_bridge.db")]
        MAP["Mapping Files<br/>/data/*.json"]
    end

    subgraph "Plex Host"
        RC["rclone FUSE mount<br/>/mnt/vod-bridge/"]
        PLEX["Plex Media Server<br/>:32400"]
    end

    PROVIDER["IPTV Provider"]

    B --> DB
    B --> MAP
    B <--> D
    D <--> V
    V <--> PROVIDER
    B -->|"HTTP /vod/"| RC
    RC --> PLEX
    PLEX <-->|"stream requests"| B

    style B fill:#2ecc71,color:#fff
    style D fill:#3498db,color:#fff
    style V fill:#e67e22,color:#fff
    style PLEX fill:#9b59b6,color:#fff
    style PROVIDER fill:#e74c3c,color:#fff
```

> **Note:** Dispatcharr and the bridge can run on the same host or different hosts. Plex can be anywhere on your network. The only requirements are: the bridge can reach Dispatcharr and Plex over the LAN, and the Plex host can reach the bridge's HTTP port (default 8585) for the rclone mount.

## Catalog Sync Flow

```mermaid
flowchart TD
    A[Bridge agent on Dispatcharr host<br/>installed by Command A] --> B[Creates stream_mapping.json<br/>category_mapping.json<br/>series_category_mapping.json<br/>movie_metadata.json<br/>account_credentials.json]
    B --> C[Agent uploads the files to the bridge's /data/]
    C --> D[Select Providers in Bridge UI]
    D --> E[Categories populate filtered to selected providers]
    E --> F[Select categories → Sync Selected]
    F --> G[Bridge fetches movie details from Dispatcharr API]
    G --> H[TMDB enrichment runs in background]
    H --> I[Language detection runs in background]
    I --> J[Movies appear in Movies tab]
    J --> K{Activate movies}
    K --> L[Fetch 8MB head + 256KB tail cache]
    L --> M[Movie appears in /vod/ endpoint]
    M --> N["rclone refreshes → Plex sees new .mp4"]

    style A fill:#e67e22,color:#fff
    style F fill:#3498db,color:#fff
    style K fill:#2ecc71,color:#fff
    style N fill:#9b59b6,color:#fff
```

## Requirements

Before you start, you'll need:

| Requirement | Why | Notes |
|-------------|-----|-------|
| **[Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** | Manages your M3U accounts and proxies VOD streams | Must be running and accessible from the bridge |
| **[Plex Media Server](https://www.plex.tv/)** and/or **[Jellyfin](https://jellyfin.org/)** | Plays the movies and series | Must be able to reach the bridge over your LAN |
| **Docker + Docker Compose** | Runs the bridge | v20+ recommended |
| **Root shell on the Dispatcharr host** | Paste Command A once (installs the bridge agent) | SSH or direct terminal |
| **Root shell on each Plex / Jellyfin host** | Paste Command B once (installs rclone, FUSE and the mounts for you) | Linux / Proxmox LXC with systemd is tested; other platforms have manual steps in [INSTALL.md](INSTALL.md) |
| **FUSE in LXC containers** | rclone needs `/dev/fuse` | On Proxmox: `pct set <id> --features fuse=1`, then restart the LXC. Command B checks this for you |
| **Dispatcharr API key** | The bridge reads the catalog through Dispatcharr's API | Create one in Dispatcharr; enter it in **Settings → Dispatcharr** |
| **TMDB API key** *(optional)* | Enriches metadata (posters, genres, descriptions) | Free at [themoviedb.org](https://www.themoviedb.org/settings/api) |

## Quick Start

See **[INSTALL.md](INSTALL.md)** for the full step-by-step guide with explanations. Once it's running, the **[User Guide](docs/USER_GUIDE.md)** covers every tab and setting. Release history is in **[CHANGELOG.md](CHANGELOG.md)**.

![First run and everyday use](docs/images/first-run-vs-everyday.svg)

1. **Start the bridge.** Save [docker-compose.example.yml](docker-compose.example.yml) as `docker-compose.yml` next to a `.env` that sets `BRIDGE_HOST` (the bridge's LAN IP) and `SETTINGS_KEY` (any long random string), then run `docker compose up -d`. This uses the published `ghcr.io/knmplace/vod-plex-bridge` image, so you don't need to clone or build anything.
2. **Sign in.** Open `http://BRIDGE_IP:8585`. The first visit creates your admin account and opens the **Setup** tab. Fill in **Settings** for Dispatcharr, Plex and TMDB.
3. **Paste Command A on the Dispatcharr host.** The Setup tab shows it with a copy button. It installs a small agent that sends the movie and series lists to the bridge and refreshes them every day.
4. **Paste Command B on each Plex or Jellyfin host.** It installs rclone and FUSE, mounts `/mnt/vod-bridge`, `/mnt/vod-bridge-movies` (movie categories) and `/mnt/vod-bridge-series`, and checks that Plex or Jellyfin can read them. If you want the Rclone Manager web UI, tick it on that machine's card *before* you run the command.
5. **Create the libraries.** For Plex, one click on the Setup tab. For Jellyfin, add two libraries by hand (plus one per movie category); [INSTALL.md](INSTALL.md) has the exact steps.
6. **Optional: movie categories.** At the top of the Movies tab, add a category in **Movie Categories** and the bridge creates its own Plex library. Activate with **Auto category** and movies from its provider categories land there. Upgrading from 0.40? Run Command B again for the new mount ([INSTALL.md](INSTALL.md#upgrading-from-040-or-earlier)).
7. **Pick categories** in the Catalog tab, then activate titles on the Movies and Series tabs.

Each step on the Setup tab turns green once the bridge confirms it worked. Platforms without systemd (Unraid, Windows, macOS, NAS) have manual steps in [INSTALL.md](INSTALL.md).

> [!IMPORTANT]
> **Activate a few titles at a time. This app is not built for hoarding a large library.**
> It works best for picking what you want to watch next: about **2–3 movies**, or **one
> season of a series (10–15 episodes)**, at a time. Let Plex or Jellyfin finish scanning them
> before you activate more.
>
> **Every activation, and every Plex or Jellyfin library scan of a new title, uses an open
> active connection slot in Dispatcharr — the same slot a viewer uses to watch.**
>
> - **Activating a movie** reads its start and end once through Dispatcharr to check that it
>   plays.
> - **Plex reads the head and tail of every new file** when it scans it. For movies the bridge
>   answers those reads from what it saved at activation, so most never reach Dispatcharr.
>   Episodes have no saved copy, so **every new episode Plex scans opens a Dispatcharr
>   connection**.
> - **Jellyfin does the same.** Its library scan probes each new file (ffprobe), which reads
>   the start of the file and often the end.
> - **Redirect mode: use with caution.** It sends Plex and Jellyfin straight to Dispatcharr's
>   proxy, so every scan read and every playback opens its own Dispatcharr connection; the
>   bridge's saved start/end and its shared pipe don't apply. **Running a VPN on the
>   Plex/Jellyfin machine is recommended** in this mode.
>
> Activate 50 episodes at once and the scan can fill every slot your provider allows, block
> live viewing, and look like rapid reconnects to the provider, which can get titles
> temporarily blocked or marked dead.

## AI-Assisted Setup

If you use an AI coding assistant (Claude, ChatGPT, etc.), the repo includes **[BUILD_SOP.md](BUILD_SOP.md)** — a setup guide written to be dropped into an AI chat session. It walks through the same deployment steps in an interactive, question-and-answer format: the AI asks about your network layout, Dispatcharr location, Plex configuration, etc., and tailors the commands to your environment.

This guide was built from the steps the development team used to get the application running. It's provided as-is to help you get started — no guarantees that it covers every edge case or environment, but it can save time and help you avoid common pitfalls. If you run into issues, the [Troubleshooting](#troubleshooting) section and [INSTALL.md](INSTALL.md) are your best references.

## File Structure

```
vod-plex-bridge/
├── app/                      # Application source
│   ├── main.py               # FastAPI app, version, lifespan
│   ├── api.py                # REST API (catalog, activation, filters, Daily Run, series)
│   ├── auth.py               # Sign-in and sessions
│   ├── proxy.py              # Stream proxy (pipes, range requests, /vod/ and /vod-series/ listings)
│   ├── redirect.py           # Optional 302 redirect streaming mode
│   ├── cache.py              # Stream buffer cache
│   ├── scraper.py            # Movie catalog sync from Dispatcharr API
│   ├── series_scraper.py     # Series catalog sync
│   ├── generator.py          # Movie .strm / .nfo file generation
│   ├── series_generator.py   # Series / season / episode folders and NFOs
│   ├── database.py           # SQLite (WAL mode, singleton connection)
│   ├── stream_mapper.py      # Movie → provider stream ID mapping
│   ├── health.py             # Health check system
│   ├── error_screens.py      # Error video generation (Pillow + FFmpeg)
│   ├── config.py             # Environment defaults + values saved in Settings
│   ├── sources.py            # Dispatcharr instances and provider records (Settings)
│   ├── secret_box.py         # Encryption for saved secrets
│   ├── connection.py         # Plex connection test
│   ├── plex_libraries.py     # Plex library lookup / creation for series
│   ├── plex_presence.py      # "In Plex" detection
│   ├── setup_scripts.py      # Setup tab: per-host commands, agent uploads, reports
│   ├── setup_templates/      # Command A / Command B shell script templates
│   ├── tmdb_local.py, tmdb_store.py, tmdb_routes.py   # Local TMDB library
│   ├── assets/
│   │   └── bg.jpg            # Background image for error screens
│   └── templates/
│       └── index.html        # Single-page UI
├── setup/
│   └── dump_mappings.sh      # Dispatcharr data extraction script (installed by Command A)
├── docs/
│   ├── USER_GUIDE.md         # How to use every tab and setting
│   └── images/               # Diagrams and screenshots used by the docs
├── Dockerfile
├── docker-compose.example.yml
├── .env.example
├── entrypoint.sh             # TZ configuration at startup
├── requirements.txt
├── INSTALL.md                # Detailed installation guide
├── BUILD_SOP.md              # AI-assisted setup guide
├── CHANGELOG.md              # Release history
└── README.md
```

## Configuration Reference

Most settings live in the app under **Settings** (one section per service, with a Test button
for each). They apply right away without a restart, except the redirect mode.

- **Plex, TMDB and streaming:** a value saved in Settings overrides `.env`; clearing it falls
  back to `.env`.
- **Dispatcharr:** on first start the `DISPATCHARR_*` values from `.env` become the primary
  instance in **Settings → Dispatcharr**. After that, the instance list is used and these
  variables are ignored.
- **Providers (XC):** read-only, a future feature. Provider logins belong in Dispatcharr; the
  bridge never contacts a provider.

API keys, tokens and passwords saved in Settings are encrypted in the database with
`SETTINGS_KEY` (or, when that is not set, a key file generated in the data folder).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISPATCHARR_URL` | Yes* | `http://localhost:9191` | Dispatcharr URL reachable from the bridge container |
| `DISPATCHARR_API_KEY` | Yes* | — | Dispatcharr API key, sent with every catalog and stream call |
| `PLEX_URL` | Yes* | — | Plex server URL (e.g., `http://192.168.x.x:32400`) |
| `PLEX_TOKEN` | Yes* | — | Plex authentication token |
| `PLEX_LIBRARY_ID` | Yes* | `7` | Plex library section ID for VOD movies |
| `PLEX_SERIES_LIBRARY_ID` | No | — | Fallback Plex TV library for series categories without their own library |
| `PLEX_MOVIE_MOUNT_ROOT` | No | `/mnt/vod-bridge` | Where the movie rclone mount appears on the Plex host |
| `PLEX_MOVIE_CATEGORY_MOUNT_ROOT` | No | `/mnt/vod-bridge-movies` | Where the movie-category rclone mount appears on the Plex host |
| `PLEX_SERIES_MOUNT_ROOT` | No | `/mnt/vod-bridge-series` | Where the series rclone mount appears on the Plex host |
| `BRIDGE_HOST` | Yes | `0.0.0.0` | LAN IP of the bridge host (written into .strm URLs) |
| `BRIDGE_PORT` | No | `8585` | Port the bridge listens on |
| `TZ` | No | `UTC` | Container timezone (e.g., `America/New_York`) |
| `TMDB_API_KEY` | No | — | TMDB API key for metadata enrichment |
| `TMDB_READ_TOKEN` | No | — | TMDB v4 read access token (alternative to API key) |
| `SETTINGS_KEY` | Recommended | — | Any long random string; encrypts secrets saved in Settings. Keep it safe: if lost, saved secrets must be re-entered |
| `DATA_DIR` | No | `./data` | Host path for database and mapping files |
| `PLEX_VOD_DIR` | No | `./plex-vod` | Host path for internal .strm/.nfo storage (Plex reads via rclone, not this path directly) |
| `STRM_SERIES_OUTPUT_DIR` | No | `/plex-vod/Series` | Path inside the container for series folders |
| `REDIRECT_MODE` | No | `false` | `true` sends Plex to Dispatcharr with a 302 instead of the pipe. Restart required |
| `MAX_CONCURRENT_PER_TITLE` | No | `2` | Session cap per title in redirect mode |

\* Required, but can be left out of `.env` and entered in **Settings** after the first start.

## Updating

```bash
docker compose pull
docker compose up -d
```

Your database and settings persist in the `DATA_DIR` volume. The agent on the Dispatcharr host and the mounts on the Plex/Jellyfin hosts keep running on their own; re-running Command A or B updates them in place.

## Troubleshooting

| Problem | Check |
|---------|-------|
| Bridge can't reach Dispatcharr | Verify `DISPATCHARR_URL` — use the LAN IP, not `localhost` (unless using `network_mode: host`) |
| Plex can't play movies | `BRIDGE_HOST` must be the LAN IP (not `0.0.0.0`). Verify rclone mount is active: `ls /mnt/vod-bridge/` on the Plex host |
| Movies in bridge but not Plex | Check rclone mount shows `.mp4` files (`ls /mnt/vod-bridge/`). Scan the library in Plex. Verify Plex library points to the rclone mount path |
| rclone mount is empty | No activated movies in the bridge, or bridge is unreachable. Test: `curl http://BRIDGE_IP:8585/vod/` |
| rclone mount hangs | Bridge is down. Check `docker ps` and `curl http://BRIDGE_IP:8585/version` from the Plex host |
| 0 categories showing | Command A hasn't reported yet: check the Dispatcharr light on the Setup tab and re-run Command A. Select at least one provider. |
| Daily Run report says the dump is stale | The agent on the Dispatcharr host isn't checking in. Re-run Command A, and check `/etc/cron.d/vod-plex-bridge` exists there |
| Mount missing / folder picker empty | `systemctl status rclone-vod-bridge-movies` on the media host, then re-run Command B. In an LXC, enable FUSE first (`pct set <id> --features fuse=1`) |
| Every page sends you to sign-in | Expected: everything except the `/vod/`, `/vod-movies/`, `/vod-series/` and `/stream/` paths requires sign-in |
| Stream stops after ~10 min | Check Dispatcharr nginx has `uwsgi_buffering off` and `uwsgi_read_timeout 300s` on the `/proxy/` location |
| "Database is locked" | Should not occur in current versions. Restart the container if it does. |

## License

MIT
