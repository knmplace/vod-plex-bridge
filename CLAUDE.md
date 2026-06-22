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
- **Bridge runs on**: .94 (LXC 405 on Proxmox .244) — same host as Dispatcharr testbed
- **Dispatcharr**: .94:9191 (through gluetun VPN container `media_vpn`)
- **Plex**: .109 (LXC on Proxmox 3 / .244)
- **NAS**: .50 — CIFS shares for shared storage between bridge, Dispatcharr, and Plex
- **Bridge port**: TBD (needs to be accessible from Plex on .109)

## Key Technical Details
- Dispatcharr VOD proxy (`/proxy/vod/movie/{uuid}?stream_id={id}`) returns raw video bytes
- Content-Type: video/x-matroska or video/mp4 with full range-request support
- Internal calls (localhost:9191) don't need JWT tokens
- External calls need short-lived JWT (~30 min expiry)
- VOD movies have: name, year, rating, genre, tmdb_id, poster (TMDB), description
- Genre field often empty in Dispatcharr — enrich from TMDB
- 42,288 total VOD items on .94 testbed

## Rules
- NEVER auto-change ports, Docker configs, or network settings without asking
- All Gitea repos PRIVATE by default
- No IPs, keys, passwords, or infra details in commit messages
- Never git push without asking
- No Co-Authored-By in commits
