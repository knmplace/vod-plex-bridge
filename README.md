# VOD Plex Bridge

A self-hosted Docker application that bridges Video On Demand (VOD) content from [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) into Plex Media Server. Browse, activate, and stream VOD movies directly through the Plex interface.

## Features

- **VOD Catalog Browser** — Browse and search your provider's full movie catalog with filters for language, category, and provider
- **One-Click Activation** — Activate movies to add them to your Plex library instantly
- **Smart Streaming** — Single persistent connection per movie, bitrate-matched to prevent provider rate limiting
- **Head/Tail Caching** — Caches the first 8MB and last 256KB of each activated movie for instant Plex probing (no provider connection needed for metadata scans)
- **TMDB Enrichment** — Automatically fills in genres, descriptions, posters, and runtime from TMDB
- **Multi-Provider Support** — Works with multiple M3U accounts, shows which provider has each movie
- **Language Detection** — Detects audio language from stream headers
- **Health Monitoring** — Dashboard showing bridge, Dispatcharr, and Plex status with response times
- **Scheduled Refresh** — Configurable auto-refresh cycle keeps your catalog current
- **Dead Movie Tracking** — Automatically identifies and removes movies that are no longer available

## Requirements

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) — running and accessible from the bridge
- [Plex Media Server](https://www.plex.tv/) — with a library pointed at the bridge's output directory
- Docker and Docker Compose
- TMDB API key (optional, for metadata enrichment)

## Quick Start

See [INSTALL.md](INSTALL.md) for detailed setup instructions.

```bash
git clone https://github.com/knmplace/vod-plex-bridge.git
cd vod-plex-bridge
cp .env.example .env        # Edit with your settings
cp docker-compose.example.yml docker-compose.yml
docker compose up -d --build
```

The bridge UI will be available at `http://your-host-ip:8585`.

## How It Works

```
Provider  ←→  Dispatcharr  ←→  VOD Plex Bridge  ←→  Plex
              (manages M3U      (catalogs, caches,    (plays .strm
               accounts +        proxies streams)       files)
               VOD proxy)
```

1. The bridge syncs the movie catalog from Dispatcharr's API
2. You browse and activate movies in the bridge UI
3. On activation, the bridge fetches and caches the head/tail bytes for instant Plex compatibility
4. The bridge generates `.strm` + `.nfo` + poster files in a Plex-readable directory
5. When Plex plays a movie, the bridge opens a single streaming connection through Dispatcharr, throttled to match the movie's bitrate

**The bridge uses Dispatcharr's connection to your provider** — however your Dispatcharr routes its traffic (VPN, direct, etc.), the bridge inherits that routing automatically.

## Architecture

| Component | Purpose |
|-----------|---------|
| `main.py` | FastAPI app, lifespan management |
| `api.py` | REST API — catalog, activation, filters, settings |
| `proxy.py` | Stream proxy — pipe management, range requests, circuit breaker |
| `scraper.py` | Catalog sync from Dispatcharr API |
| `generator.py` | .strm/.nfo file generation |
| `database.py` | SQLite with WAL mode, singleton connection |
| `stream_mapper.py` | Maps movies to provider stream IDs |
| `health.py` | Health check system (bridge, Dispatcharr, Plex) |
| `config.py` | Environment variable configuration |

## License

MIT
