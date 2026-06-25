# VOD Plex Bridge — Pickup Summary (2026-06-24)

## Current State: v0.23.0 built on .94, awaiting first test

Image built and ready on .94. User will redeploy via Portainer and pick a FRESH movie to test.

## What Changed (v0.17.1 → v0.23.0)

### The Core Problem
Dispatcharr is a pass-through proxy. Its built-in browser player opens ONE persistent HTTP connection and reads data at playback speed. This keeps Dispatcharr's upstream provider connection alive for the full movie duration.

The bridge (v0.20-v0.22) made thousands of separate 64KB Range GET requests. Each completed instantly on the local network. Dispatcharr saw rapid connect/disconnect cycles and closed the upstream provider connection after each request completed. The bridge downloaded the entire 1.75GB file in ~30 seconds, Dispatcharr's active connection card appeared for 10-20 seconds then vanished.

### The Fix (v0.23.0)
Single persistent streaming connection using httpx `aiter_bytes()`:
- ONE GET request with `Range: bytes=0-`
- Response stays open — `async for chunk in resp.aiter_bytes(65536)`
- Wall-clock rate limiter: `bytes_written / (bitrate * 1.2)` vs elapsed time
- First 2MB at full speed, then paced to 1.2× provider bitrate
- Progress logged every 30 seconds
- Connection stays open for entire movie duration — same as browser player

### Key Files Changed
- `app/proxy.py` — StreamPipe._download_loop() rewritten from separate Range requests to single streaming connection
- `app/proxy.py` — Pipe starts from byte 0 on first Plex GET (not header_size offset)
- `app/proxy.py` — Live pipe data served before header cache fallback
- `app/api.py` — Provider info (bitrate/duration) fetched during activation, backfill on startup
- `app/database.py` — `stream_bitrate_kbps` column added
- `app/main.py` — Version 0.23.0
- `app/scraper.py` — Minor fixes
- `Dockerfile` — Removed stale `COPY templates/` line
- `CLAUDE.md` — Updated architecture docs
- `BUILD_SOP.md` — Version history updated

### Version History (what was tried and why it failed)
| Version | Approach | Result |
|---------|----------|--------|
| v0.20.0 | 256KB chunks with `sleep_per_chunk = chunk/bitrate` (5.4s) | Sleep too long, Dispatcharr dropped connection |
| v0.21.0 | Removed sleep entirely | Downloaded 1.86GB in 28s, provider killed it |
| v0.21.1 | Pipe from byte 0, no sleep | Still 30s download, same problem |
| v0.22.0 | Separate 64KB Range requests with 0.2s sleep | Thousands of connections, Dispatcharr dropped upstream each time |
| v0.23.0 | Single streaming connection, wall-clock rate limiter | **UNTESTED** — correct approach based on browser player analysis |

## What To Test Next
1. Redeploy v0.23.0 via Portainer
2. Pick a FRESH movie never touched during testing (~23+ movies burned)
3. Activate in bridge, Sync Selected, play in Plex
4. Watch Dispatcharr active connections — bridge connection should stay active for minutes, not seconds
5. Check bridge proxy logs — should show gradual progress, not instant 128MB chunks
6. Check container logs: `docker logs vod-plex-bridge` for pipe progress (every 30s)

## What Success Looks Like
- Dispatcharr shows bridge connection active for 10+ minutes continuously
- Bridge logs show: `Pipe movie X: 5.2% (91MB / 1751MB) @ 337 KB/s (target 337 KB/s) elapsed 270s`
- Plex plays movie smoothly from buffer
- Provider connection stays alive for full movie duration

## Infrastructure
- Bridge: .94:8585 (LXC 355 on .244)
- Dispatcharr: .94:9191 (through gluetun VPN)
- Plex: .109
- Local dev: `b:\Claude_Apps\vod-plex-bridge\`
- Server repo: `/etc/docker/plexbridge/repo/`
- Deploy: pscp files → `docker build -t vod-plex-bridge:latest .` on .94 → Portainer redeploy

## Bitrate Example
"Don't Look Now" (1065536): 2249 kbps = 281,125 bytes/sec
- Target rate: 281,125 × 1.2 = 337,350 bytes/sec (~330 KB/s)
- File size: 1,775.4 MB
- Expected download time: ~87 minutes (close to 1:50 runtime)

## Git State
- All v0.17.1→v0.23.0 changes committed to local repo
- Gitea remote: Kyle/vod-plex-bridge on .242:3005
- Push status: check with user before pushing (CI auto-builds on push)

## Rules (Always Follow)
- Never docker run/stop/rm — only build image, user redeploys via Portainer
- Never git push without asking
- No IPs, keys, passwords in commit messages
- No Co-Authored-By in commits
- Never auto-change ports, Docker configs, network settings
