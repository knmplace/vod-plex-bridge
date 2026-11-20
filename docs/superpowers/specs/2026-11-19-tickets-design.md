# Tickets: send a support report from the bridge (design)

Status: APPROVED 2026-11-19. Target version 0.47.0 (new feature, so a minor bump).

Setup done 2026-11-19: private Gitea repo `plex_bridge_tickets` (Kyle), bot user `vpb-ticket-bot` (write on that repo only,
token scope write:issue), RSA-4096 keypair. Issue #1 was a setup test and is closed, so first real ticket is VPB-2.
Keys/tokens live outside the repo in `b:\Claude_Apps\plex-bridge-tickets-secrets\`. Decisions: token is built into the
app (public-repo exposure accepted, with size cap + rate limit); Plex section uses the saved Plex token, "unavailable" if unset.

## Intent and why
Users hit problems we cannot see. Example (2026-11-19): a user's proxy log looks like playback but is Plex
scanning, reading a whole episode in growing ranges, each read redirected to Dispatcharr and opening a provider
connection. To diagnose that we needed Plex's active tasks and settings, not just the bridge log. A Tickets button
lets a user send us everything we would look at if we were logged in to their system, safely, and keep a ticket
number to refer back to. Same model as KNM Media's "Send a report".

## Goals
- Maintenance sub-tab **Tickets**, placed right after Rclone, always visible.
- Split page like Proxy Logs: left = make and send a ticket, right = My Tickets (VPB-n, date, version, note title, status), kept locally so the user can refer back.
- "Collect report" builds the bundle, scrubs it, and shows exactly what will be sent in a read-only scrollable box. A Note box (free text, like the media app) sits beside it. Submit encrypts and sends, then shows the ticket number **VPB-nn**.
- Reports are encrypted on the box; only we can read them.
- Gather enough to troubleshoot without asking the user follow-up questions (list below).

## Non-goals / anti-goals
- No auto-send, no background telemetry. Nothing leaves the box until the user clicks Submit.
- No replies to users in v1 (KNM has them; later).
- Failure anti-goal: a secret, provider host, or credential reaching the ticket. The preview must be the same bytes that get encrypted.

## Constraints
- Bridge is a FastAPI app, single `index.html` (Jinja: no literal `{{ }}` in JS). New panels are collapsible.
- Public GitHub repo: no keys, IPs, passwords, or provider names in code, commits, or docs. The private RSA key never enters the repo.
- No provider calls. The bundle only reads the bridge DB, bridge logs, Plex API, Dispatcharr API (cached/status endpoints).
- Never push without asking; every push gets a full release with user-facing notes.

## Approaches considered
1. **Encrypted report to a private Gitea issues repo (chosen).** Hybrid RSA-OAEP + AES-GCM blob (KNM's "v1" format), public key built into the app, create-issue-only token. Issue number = ticket number. Decrypt with our private key. Proven in KNM Media, no new server.
2. GitHub issues on the public repo: rejected, encrypted blobs in public issues and a token in public source is the weakest option.
3. Download a file and send by hand: kept only as the fallback when sending fails.

## Design

**Repo and keys (we set up).** New private Gitea repo `plex_bridge_tickets` under the existing account. A dedicated bot user with write access to that repo only; its token (issue-create scope) is built into the app, so a leaked token can only create issues there. New RSA-4096 keypair; public key in the app, private key kept on our machine. Tool `tools/decrypt_ticket.py` (adapted from KNM's `decrypt_report.py`) reads and decrypts VPB-n.

**Ticket numbers.** Gitea issue number n shows as VPB-n. Title: `VPB-n <date> <app version> - <first words of the note>`. The box saves each sent ticket (number, date, version, note title) in `app_settings`; the right pane lists them.

**Bundle contents (bridge side).**
- App: version, uptime, OS/container, Python, all settings (names and values; secrets shown only as set/unset), schedule times, last Daily Run report.
- Database counts: movies (total/activated/dead/queued), series, episodes, activation queue (movies and series), learned_ranges summary, series watch state and recent events, last sync results.
- Streaming: proxy log (recent entries plus latest archive), active pipes, pipe caps, circuit breaker state, per-account slot use, recent errors.
- Container log tail (app logger, last ~2000 lines).
- Plex mounts check, saved machines and their reports (rclone flags, mount status, disk gauge, rclone/OS versions).

**Bundle contents (Plex and Dispatcharr).**
- Plex (using the user's own token): version, `/status/sessions`, `/activities`, library list with paths, the scan/analysis preferences (scheduled library scan, partial scans, video preview thumbnails, intro/credits detection, loudness/chapter analysis, butler task windows), recent scanner activity if available.
- Dispatcharr: version, VOD proxy stats, M3U accounts with name, status and max connections only (no URLs or credentials).

**Scrubbing and review.** Applied on the server before display. Replace with stable numbered placeholders so correlation survives: `<IP-1>`, `<HOST-1>`, `<PROVIDER-1>`, `<ACCOUNT-1>`, `<TOKEN>`, `<API-KEY>`, `<PASSWORD>`, `<EMAIL>`. Two layers: (a) exact values of known secrets from config/env/DB (Dispatcharr key, Plex token, TMDB keys, provider hosts and usernames, M3U URLs, admin password hash); (b) regex for IPv4/IPv6, JWTs, long tokens, `token=`/`api_key=`/`X-Plex-Token` query values, URL credentials, emails, provider-looking hostnames. The preview is read-only, scrollable, and shows a short list "N placeholders substituted". The Submit button encrypts the exact previewed text.

**Send.** Gzip then encrypt (v1 blob), post as an issue; if too large for one body, the rest go as ordered comments (chunk size below Gitea's limit). Client side: one send per minute. If Gitea is unreachable: ticket stays "unsent" in the right pane and the user can download the encrypted file to send by hand.

**UI.** Left pane: [Collect report] -> read-only preview + counts, Note box, [Submit]. Right pane: ticket list, newest first, each with VPB-n, date, version, note title, status "sent/unsent". Both panes collapsible per repo UI rule.

## Decisions for the builder
- Exact placeholder rules and the full secret list: builder inventories every setting/env var that holds a secret, and adds a test that seeds known secrets and asserts none survive in the preview.
- Chunking size and gzip level.
- Plex endpoints that fail (old Plex, no token): skip with a one-line "unavailable" note instead of failing the bundle.
- Tests: scrub tests, blob round-trip with the private key, endpoint tests; no real Gitea in tests.

## Calls I made (not yet agreed)
- Dedicated bot user rather than Kyle's own token.
- Version 0.47.0.
- Download-file fallback when sending fails.
- Preview is read-only; users can only edit the Note.
- Tickets tab always visible (not hidden like Rclone).
