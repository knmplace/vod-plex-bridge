# VOD Plex Bridge

## What This Is
A FastAPI Docker service that bridges Dispatcharr VOD movies into Plex by:
1. Scraping the VOD catalog from Dispatcharr's API
2. Generating Plex-compatible .strm + .nfo files with configurable category/genre filtering
3. Proxying video streams so Plex sees real MKV/MP4 content with range-request support

## Shared Resources
- **SSH access**: See `b:\Claude_Apps\ssh.md` and `b:\Claude_Apps\.ssh.env`
- **Dispatcharr MCP**: Available on .94 testbed (port 9191 through gluetun)
- **CIFS NAS**: `//192.168.1.50/Media` mounted as `/MEDIA50` on multiple hosts
- **Standards**: See `b:\Claude_Apps\standards.md` and `b:\Claude_Apps\ui-standards.md`
- **Git/Deploy**: See `b:\Claude_Apps\git.md` and `b:\Claude_Apps\deployment.md`

## Infrastructure
- **Bridge runs on**: .94 (LXC 355 on Proxmox .244) — same host as Dispatcharr testbed
- **Dispatcharr**: .94:9191 (through gluetun VPN container `media_vpn`)
- **Plex**: .109 (LXC on Proxmox 3 / .244)
- **NAS**: .50 — CIFS shares for shared storage between bridge, Dispatcharr, and Plex

## Key Technical Details
- Dispatcharr VOD proxy (`/proxy/vod/movie/{uuid}?stream_id={id}`) returns raw video bytes
- Content-Type: video/x-matroska or video/mp4 with full range-request support
- Internal calls (localhost:9191) don't need JWT tokens
- External calls need short-lived JWT (~30 min expiry)
- VOD movies have: name, year, rating, genre, tmdb_id, poster (TMDB), description
- Genre field often empty in Dispatcharr — enrich from TMDB
- 42,288 total VOD items on .94 testbed

## Streaming Pipe Architecture (v0.23.0 — updated 2026-06-24)

### Session Management
- **ONE session per activation** (head+tail fetch reuse the same session URL via `_resolve_session`)
- **ONE session per playback** — pipe uses `follow_redirects=True` on httpx client, sends GET directly to base XC URL, httpx follows 301 redirect naturally. NO separate `_resolve_session` for pipe.
- CRITICAL: the old approach of calling `_resolve_session` separately "consumed" the session — Dispatcharr returned 500 on subsequent requests to that session_id.

### Activation
- Fetches 20MB head + 20MB tail cache per movie (stored in SQLite, one session)
- Also fetches provider info (bitrate, duration) from Dispatcharr `/api/vod/movies/{id}/provider-info/`
- Head/tail are raw movie file bytes — stable across sessions, never change for the same movie
- Serves Plex probing (HEAD) and initial playback requests instantly with zero provider connections

### Bitrate Throttling (Priority Order)
1. **Provider bitrate** (`stream_bitrate_kbps`): from Dispatcharr `provider-info` endpoint → `custom_properties.detailed_info.bitrate`. Exact per-stream value from the M3U provider. Converted: `(kbps * 1000) / 8` → bytes/sec.
2. **Calculated**: `file_size / duration_seconds` (TMDB runtime from scraper enrichment)
3. **Fallback**: 500 KB/s (500,000 bytes/sec) — safe default

### Playback — Single Persistent Streaming Connection (v0.23.0)
- **ONE streaming GET** with `aiter_bytes()` — connection stays open for entire movie duration.
- Pipe starts from byte 0 on first Plex GET request via `asyncio.create_task`.
- Wall-clock rate limiter: tracks `bytes_written / target_bps` vs elapsed time, sleeps when ahead.
- Target rate = provider bitrate × 1.2 (slight buffer build ahead of Plex).
- First 2MB at full speed for fast start, then paced.
- Progress logged every 30 seconds with actual vs target KB/s.
- `read_range()` returns partial data as soon as STREAM_CHUNK bytes available.
- Dispatcharr sees one continuous consumer — same pattern as its built-in browser player.

### Why Single Connection Matters
- v0.20-v0.22 made thousands of separate Range GET requests per movie.
- Each request completed instantly on LAN, Dispatcharr saw rapid connect/disconnect cycles.
- Dispatcharr closes upstream provider connection when no active consumer is pulling.
- Browser player uses ONE persistent connection — data flows continuously at playback speed.
- Bridge must mirror this: one GET, stream response, pace reads to match bitrate.

### Circuit Breaker (Hardened)
- Once tripped, NEVER auto-resets. Stays tripped until container restart.
- Every failure path (no pipe, pipe error, no data, exception) records failure.
- Entire pipe section wrapped in try/except that trips breaker on ANY error.

### Pause / Stop Behavior
- 5-min idle timeout → pipe closes, buffer cleared, head/tail cache kept
- Every replay after timeout = completely fresh — new session, new pipe

### Seek (Skip Forward/Back)
- Within buffer: served immediately
- Beyond buffer: pipe closes, restarts from new offset (one new session)

### Known Issues (as of 2026-06-24)
1. v0.23.0 built and deployed on .94, awaiting test with fresh movie.
2. ~23+ movies burned during testing. Test carefully with untouched movies only.
3. Git: all changes v0.17.1→v0.23.0 now committed.

## Rules
- NEVER auto-change ports, Docker configs, or network settings without asking
- All Gitea repos PRIVATE by default
- No IPs, keys, passwords, or infra details in commit messages
- Never git push without asking
- No Co-Authored-By in commits
