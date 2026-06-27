# VOD Plex Bridge

A self-hosted Docker application that bridges Video On Demand (VOD) content from [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) into [Plex Media Server](https://www.plex.tv/). Browse, activate, and stream VOD movies directly through the Plex interface — no manual file management required.

<!-- TODO: Add screenshot of the bridge UI here -->
<!-- ![Bridge UI](docs/screenshots/bridge-ui.png) -->

## How It Works

The bridge sits between Dispatcharr and Plex. It reads your VOD catalog from Dispatcharr, lets you pick which movies you want in Plex, and handles all the streaming plumbing automatically.

```mermaid
graph LR
    A[IPTV Provider] <-->|"M3U streams"| B[Dispatcharr]
    B <-->|"VOD proxy API"| C[VOD Plex Bridge]
    C -->|".strm files"| D[Shared Storage]
    D -->|"reads .strm"| E[Plex Media Server]
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
- Plex reads `.strm` files that point back to the bridge, which proxies the actual video stream

## Features

- **VOD Catalog Browser** — Browse and search your provider's movie catalog with multi-select filters for language, category, and provider
- **One-Click Activation** — Activate movies to instantly add them to your Plex library with poster art, genres, and metadata
- **Smart Streaming** — Single persistent connection per movie, bitrate-throttled to match the stream's actual bitrate
- **Head/Tail Caching** — Caches the first 8 MB and last 256 KB of each activated movie so Plex can probe metadata without opening a provider connection
- **TMDB Enrichment** — Automatically fills in genres, descriptions, posters, and runtime from [The Movie Database](https://www.themoviedb.org/)
- **Multi-Provider Support** — Works with multiple M3U accounts, shows which providers carry each movie
- **Language Detection** — Background detection of audio language via TMDB, with filters in the browse UI
- **Health Dashboard** — Real-time status of Bridge, Dispatcharr, and Plex with response times
- **Scheduled Refresh** — Configurable auto-refresh cycle (4h / 6h / 8h / 12h) keeps your catalog current
- **Dead Movie Tracking** — Automatically detects and removes movies no longer available from your provider
- **Mark Dead** — Manual skull button on movie cards to remove specific movies you don't want
- **Persistent Buffers** — Downloaded data stays on disk between play sessions. Resume where you left off.

## Architecture Overview

```mermaid
graph TB
    subgraph "Host Machine"
        subgraph "Docker"
            B["VOD Plex Bridge<br/>:8585"]
            D["Dispatcharr<br/>:9191"]
            V["VPN Container<br/>(optional, e.g. gluetun)"]
        end
        DB[("SQLite DB<br/>/data/vod_bridge.db")]
        MAP["Mapping Files<br/>/data/*.json"]
        STRM["STRM Output<br/>/plex-vod/Movies/"]
    end

    subgraph "Network"
        PLEX["Plex Media Server<br/>:32400"]
        PROVIDER["IPTV Provider"]
    end

    B --> DB
    B --> MAP
    B --> STRM
    B <--> D
    D <--> V
    V <--> PROVIDER
    PLEX --> STRM
    PLEX <--> B

    style B fill:#2ecc71,color:#fff
    style D fill:#3498db,color:#fff
    style V fill:#e67e22,color:#fff
    style PLEX fill:#9b59b6,color:#fff
    style PROVIDER fill:#e74c3c,color:#fff
```

> **Note:** Dispatcharr and the bridge can run on the same host or different hosts. Plex can be anywhere on your network. The only requirement is that all three can reach each other over the LAN.

## Playback Flow

When Plex plays a movie, here's what happens under the hood:

```mermaid
sequenceDiagram
    participant P as Plex
    participant B as Bridge
    participant D as Dispatcharr
    participant S as Provider

    P->>B: GET /stream/movie_123.mkv
    Note over B: Check buffer cache first
    alt Buffer exists on disk
        B-->>P: Serve from buffer
    else No buffer
        B->>B: Check head/tail cache (SQLite)
        B-->>P: Serve 8MB head cache (fast start)
        B->>D: GET /proxy/vod/movie/{uuid}?stream_id={id}
        D->>S: Stream request (through VPN)
        S-->>D: Video bytes
        D-->>B: Proxied video stream
        Note over B: Single persistent connection<br/>Throttled to stream bitrate × 1.2
        B->>B: Write to disk buffer
        B-->>P: Serve from buffer as it fills
    end
    Note over P,B: Plex reads in bursts (5-7s pauses)<br/>Bridge keeps connection open for 30s idle
    P->>B: Plex stops/pauses
    Note over B: 30s idle timeout → clean disconnect<br/>Buffer stays on disk for resume
```

## Catalog Sync Flow

```mermaid
flowchart TD
    A[Run dump_mappings.sh on Dispatcharr host] --> B[Creates stream_mapping.json<br/>category_mapping.json<br/>account_credentials.json]
    B --> C[Bridge reads mapping files from /data/]
    C --> D[Select Providers in Bridge UI]
    D --> E[Categories populate filtered to selected providers]
    E --> F[Select categories → Sync Selected]
    F --> G[Bridge fetches movie details from Dispatcharr API]
    G --> H[TMDB enrichment runs in background]
    H --> I[Language detection runs in background]
    I --> J[Movies appear in Browse tab]
    J --> K{Activate movies}
    K --> L[Fetch 8MB head + 256KB tail cache]
    L --> M[Generate .strm + .nfo + poster]
    M --> N[Plex library scan picks them up]

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
| **[Plex Media Server](https://www.plex.tv/)** | Plays the movies | Must be able to reach the bridge over your LAN |
| **Docker + Docker Compose** | Runs the bridge | v20+ recommended |
| **Shell access to Dispatcharr host** | Run the dump script to extract mapping data | SSH or direct terminal |
| **Shared storage path** | Both the bridge and Plex need to read `.strm` files | Same host, NFS, CIFS, etc. |
| **TMDB API key** *(optional)* | Enriches metadata (posters, genres, descriptions) | Free at [themoviedb.org](https://www.themoviedb.org/settings/api) |

## Quick Start

See **[INSTALL.md](INSTALL.md)** for the full step-by-step guide with explanations.

```bash
# 1. Clone and configure
git clone https://github.com/knmplace/vod-plex-bridge.git
cd vod-plex-bridge
cp .env.example .env          # Edit with your IPs, tokens, paths
cp docker-compose.example.yml docker-compose.yml

# 2. Build and start
docker compose up -d --build

# 3. Run the dump script on your Dispatcharr host
DISPATCHARR_CONTAINER=your-container-name \
BRIDGE_DATA_DIR=/path/to/bridge/data \
bash setup/dump_mappings.sh

# 4. Open http://your-bridge-ip:8585 and start browsing!
```

## AI-Assisted Setup

If you use an AI coding assistant (Claude, ChatGPT, etc.), the repo includes **[BUILD_SOP.md](BUILD_SOP.md)** — a setup guide written to be dropped into an AI chat session. It walks through the same deployment steps in an interactive, question-and-answer format: the AI asks about your network layout, Dispatcharr location, Plex configuration, etc., and tailors the commands to your environment.

This guide was built from the steps the development team used to get the application running. It's provided as-is to help you get started — no guarantees that it covers every edge case or environment, but it can save time and help you avoid common pitfalls. If you run into issues, the [Troubleshooting](#troubleshooting) section and [INSTALL.md](INSTALL.md) are your best references.

## File Structure

```
vod-plex-bridge/
├── app/                      # Application source
│   ├── main.py               # FastAPI app, version, lifespan
│   ├── api.py                # REST API (catalog, activation, filters, settings)
│   ├── proxy.py              # Stream proxy (pipes, range requests, circuit breaker)
│   ├── scraper.py            # Catalog sync from Dispatcharr API
│   ├── generator.py          # .strm / .nfo file generation
│   ├── database.py           # SQLite (WAL mode, singleton connection)
│   ├── stream_mapper.py      # Movie → provider stream ID mapping
│   ├── health.py             # Health check system
│   ├── config.py             # Environment variable config
│   └── templates/
│       └── index.html        # Single-page UI
├── setup/
│   └── dump_mappings.sh      # Dispatcharr data extraction script
├── Dockerfile
├── docker-compose.example.yml
├── .env.example
├── entrypoint.sh             # TZ configuration at startup
├── requirements.txt
├── INSTALL.md                # Detailed installation guide
└── README.md
```

## Configuration Reference

All configuration is done through environment variables in `.env`:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISPATCHARR_URL` | Yes | `http://localhost:9191` | Dispatcharr URL reachable from the bridge container |
| `PLEX_URL` | Yes | — | Plex server URL (e.g., `http://192.168.x.x:32400`) |
| `PLEX_TOKEN` | Yes | — | Plex authentication token |
| `PLEX_LIBRARY_ID` | Yes | `7` | Plex library section ID for VOD movies |
| `BRIDGE_HOST` | Yes | `0.0.0.0` | LAN IP of the bridge host (Plex uses this to reach the bridge) |
| `BRIDGE_PORT` | No | `8585` | Port the bridge listens on |
| `TZ` | No | `UTC` | Container timezone (e.g., `America/New_York`) |
| `TMDB_API_KEY` | No | — | TMDB API key for metadata enrichment |
| `TMDB_READ_TOKEN` | No | — | TMDB v4 read access token (alternative to API key) |
| `DISPATCHARR_API_KEY` | No | — | Dispatcharr API key (used for VPN IP display) |
| `DATA_DIR` | No | `./data` | Host path for database and mapping files |
| `PLEX_VOD_DIR` | No | `./plex-vod` | Host path for .strm output (Plex reads this) |

## Updating

```bash
cd vod-plex-bridge
git pull
docker compose up -d --build
```

Your database and settings persist in the `DATA_DIR` volume. Re-run `setup/dump_mappings.sh` on the Dispatcharr host if your M3U accounts have changed.

## Troubleshooting

| Problem | Check |
|---------|-------|
| Bridge can't reach Dispatcharr | Verify `DISPATCHARR_URL` — use the LAN IP, not `localhost` (unless using `network_mode: host`) |
| Plex can't play movies | `BRIDGE_HOST` must be the LAN IP (not `0.0.0.0`). Check `.strm` file contents: URL inside must be reachable from Plex |
| Movies in bridge but not Plex | Ensure Plex library path matches `PLEX_VOD_DIR`. Scan the library in Plex. Check `.strm` files exist |
| 0 categories showing | Run `setup/dump_mappings.sh` on the Dispatcharr host first. Select at least one provider. |
| Stream stops after ~10 min | Update to latest version (fixed in v0.25.0+). Check Dispatcharr nginx has `uwsgi_buffering off` on `/proxy/` |
| "Database is locked" | Should not occur in v0.27.1+. Restart the container if it does. |

## License

MIT
