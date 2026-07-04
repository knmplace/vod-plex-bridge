# VOD Plex Bridge — Architecture Archive

> Full version-by-version streaming pipe architecture history, moved out of `CLAUDE.md` on
> 2026-07-03 to keep that file under 100 lines. `CLAUDE.md` keeps only current load-bearing
> facts; this file is the detailed "why it works this way" reference.

## Streaming Pipe Architecture (v0.27.1 — updated 2026-06-26)

### Endpoint Routing — FIXED in v0.26.0 (root cause of burned movies #2)
- **All streaming now uses `/proxy/vod/movie/{uuid}?stream_id={id}`** — routes through Dispatcharr's nginx `/proxy/` location which has `uwsgi_buffering off` and `uwsgi_read_timeout 300s`.
- **BUG (pre-v0.26.0)**: XC-style `/movie/` path went through nginx's default `/` location with no streaming optimizations. After ~10 min, nginx tore down the uwsgi connection (GeneratorExit), killing the provider session. Bridge continued reading from nginx buffer until it ran out, then reconnected to find provider returned 509 (burned).
- **PROOF**: Browser playing through `/proxy/vod/` streamed 51+ minutes without any disconnection. XC path failed at ~10 minutes every time.

### Session Management
- **ONE session per activation** (head+tail fetch reuse the same session URL via `_resolve_session`)
- **ONE session per playback** — `_get_or_create_pipe` resolves the session URL via `_resolve_session` (one 301 follow), verifies the resolved host matches Dispatcharr's domain, then passes it to `StreamPipe`. The pipe's httpx client uses `follow_redirects=False` to ensure it never escapes Dispatcharr.

### VPN / Routing — FIXED in v0.25.0
- **Bridge is NOT behind the VPN.** Only Dispatcharr runs inside gluetun.
- `_resolve_session` pre-resolves the 301 redirect, verifies the resolved URL stays on Dispatcharr's domain, and creates the pipe with that session URL. The pipe's httpx client uses `follow_redirects=False`, so it can never escape to an external provider URL.

### Activation-as-Validation (v0.27.0)
- Activation IS the validation — `_fetch_headers_validated()` fetches head+tail and only marks `activated=1` on success
- On failure (HTTP error, empty response, session resolve fail): marks `stream_dead=1, activated=0` immediately (1-strike)
- Failed movies removed from Browse, appear in Dead Movies tab
- No resurrection scheduler — once dead, stays dead (manual restore only)
- No catalog validation scheduler — activation is the only code path that opens provider connections (besides playback)
- Fetches 8MB head + 256KB tail cache per movie (stored in SQLite, one session)
- Also fetches provider info (bitrate, duration) from Dispatcharr `/api/vod/movies/{id}/provider-info/`
- Head/tail are raw movie file bytes — stable across sessions, never change for the same movie
- Serves Plex probing (HEAD) and initial playback requests instantly with zero provider connections

### Data Serve Priority (in vod_file endpoint)
1. **Pipe buffer** — checked first. If pipe (active or finished) has data for the requested range, serve from disk buffer.
2. **Header cache** — 8MB from SQLite. Covers Plex fast-start probing. Used while pipe is starting up.
3. **Tail cache** — last 256KB from SQLite. Used for Plex probing (moov atom / seek tables).
4. **New pipe / circuit breaker** — if nothing above can serve, try to create a new pipe or return 503 if CB tripped.

### Bitrate Throttling (Priority Order)
1. **Provider bitrate** (`stream_bitrate_kbps`): from Dispatcharr `provider-info` endpoint → `custom_properties.detailed_info.bitrate`. Exact per-stream value from the M3U provider. Converted: `(kbps * 1000) / 8` → bytes/sec.
2. **Calculated**: `file_size / duration_seconds` (TMDB runtime from scraper enrichment)
3. **Fallback**: 500 KB/s (500,000 bytes/sec) — safe default

### Playback — Single Persistent Streaming Connection
- **ONE streaming GET** with `aiter_bytes()` — connection stays open for entire movie duration.
- Pipe starts from byte 0 on first Plex GET request via `asyncio.create_task`.
- Wall-clock rate limiter: tracks `bytes_written / target_bps` vs elapsed time, sleeps when ahead.
- Adaptive throttle: 2.3x when <5s ahead, 1.8x at 5-10s, 1.3x at 10-30s, 1.2x at 30-60s, 1.0x at 60-120s, 0.8x when >120s ahead.
- First 2MB at full speed for fast start, then paced.
- Progress logged every 30 seconds with actual vs target KB/s.
- `read_range()` returns partial data as soon as STREAM_CHUNK bytes available.
- Finished pipes still serve data from buffer — no reconnect needed for end-of-movie.

### Why Single Connection Matters
- v0.20-v0.22 made thousands of separate Range GET requests per movie.
- Each request completed instantly on LAN, Dispatcharr saw rapid connect/disconnect cycles.
- Dispatcharr closes upstream provider connection when no active consumer is pulling.
- Browser player uses ONE persistent connection — data flows continuously at playback speed.
- Bridge must mirror this: one GET, stream response, pace reads to match bitrate.

### Dual Idle Timer System (v0.24.1, updated 2026-06-27)
- **Plex idle** (`last_plex_read`): **30 sec**. Only updated by `read_range()`. Detects user stop/pause → clean disconnect from provider, buffer retained on disk.
- **Full idle** (`last_read_at`): 15 min. Updated by both Plex reads AND download writes. Safety net for finished/stalled pipes.
- Download loop updates `last_read_at` to prevent premature pipe kills during active playback when Plex is reading from header cache.
- **CRITICAL**: Never set PLEX_IDLE_TIMEOUT below 15s. Plex reads in bursts (5-7s pauses between). Timeouts shorter than Plex's pause window cause rapid session cycling that burns movies (proven 2026-06-27: 5s timeout → 6 Dispatcharr sessions in 60s → provider block).

### Circuit Breaker (Hardened)
- Once tripped, NEVER auto-resets. Stays tripped until container restart.
- Every failure path (no pipe, pipe error, no data, exception) records failure.
- Entire pipe section wrapped in try/except that trips breaker on ANY error.

### Seek (Skip Forward/Back)
- Within buffer: served immediately
- Beyond buffer: pipe closes, restarts from new offset (one new session)

### Health Check
- Validates entire chain: Bridge DB → Dispatcharr API → Plex VOD library section (API call to /library/sections/7)
- Polls every 2 hours. UI shows all three components with response times.

### Pipe Creation Lock (v0.26.1 — fixed duplicate pipe race condition)
- Per-movie `asyncio.Lock` in `_pipe_creating` dict prevents concurrent pipe creation.
- `vod_file()` uses `await _get_or_create_pipe()` instead of `asyncio.create_task()` (fire-and-forget).
- Root cause: Plex sends multiple requests milliseconds apart. Without lock, both saw `_movie_pipes.get(movie_id)` as None and created separate pipes → two upstream connections → provider killed both (509 burn).

### UUID Resilience (v0.26.2)
- Both `_upsert_movie` (scraper.py) and `_upsert_movie_from_api` (api.py) detect UUID changes on catalog sync.
- If UUID changed: clears `header_data`, `tail_data`, `file_size` for that movie and logs it.
- Prevents stale caches after Dispatcharr M3U refresh regenerates UUIDs.

### Proxy Log Movie Names (v0.26.2)
- `_log_event()` accepts optional `movie_name` parameter.
- `vod_file()` passes movie name from DB to all log events.
- Log UI shows movie title instead of raw movie ID.

### Activate Button on Browse Cards (v0.26.2)
- Each movie card has a lightning bolt button (hover to reveal).
- Green = click to activate, red = click to deactivate.
- Uses same `/api/movies/activate` and `/api/movies/deactivate` endpoints as bulk selection.

### Collapsible Panels (v0.26.2)
- Providers and Categories panels collapse/expand on header click.
- State persisted in localStorage across page reloads.

### Daily Run (v0.35.4, renamed/simplified from "Catalog Refresh Schedule")
- Settings tab: simple Off / On toggle. When On, runs automatically once daily at 6:00 AM local time.
- Scheduler wakes at wall-clock 6:00 AM (not "24h since last run") — survives restarts/backups without drifting, rechecks the setting hourly in case it's toggled off mid-wait.
- Manual "Run Now" button triggers the identical cycle on demand, any time.
- Concurrency guard: `_refresh_running` flag + 409 response + button disable prevents double-launch; scheduled run skips if a manual run is already in progress.
- Stored in `sync_state.refresh_interval_hours` column (0=off, 24=daily) — persists across restarts. Driven by `start_dead_scan_scheduler()` in api.py (name kept for compat; it no longer runs dead scan).
- **Cycle** (`_run_scheduled_refresh()`, all DB/Dispatcharr-API-only, zero provider connections):
  1. Dump regen (trigger file → host cron regenerates category/stream mapping dumps; reports "TIMED OUT" clearly if the 3-min wait lapses, since downstream steps may then use stale dump data)
  2. Categories (reload category/provider mappings from dump files)
  3. Catalog refresh (Phase 1: re-fetch every existing movie from Dispatcharr, update metadata, detect UUID changes; Phase 2: discover new movies in selected categories)
  4. Stream mapping (apply fresh stream_ids to all movies)
  5. Provider-info enrichment (fill genre/description/director/cast/country from Dispatcharr, Dispatcharr API only)
  6. Activated stream_id refresh — DB-only update of `stream_id`/`content_type`/`account_id` for movies with `activated=1`; existing head/tail cache is deliberately retained, not cleared, so playback keeps working immediately
  7. Language detection — background task, but only ever touches movies with no language tag yet (new movies), so it's a no-op for the rest of the catalog
- **Explicitly NOT in the daily cycle**: TMDB title search (manual "TMDB Search" button only) and dead scan (manual "Scan Dead" button only — was mistakenly documented/implemented as part of the cycle in older versions; confirmed removed).
- Stream_id comparison is type-safe: `str(old) != str(new)` (SQLite=int, JSON=string).
- Report archive: JSON array in `sync_state.last_refresh_report`, up to 10 entries with date/elapsed/steps. Settings tab shows a centered "Last Run" card plus a compact "Run Archive" list (date + duration only) — click an entry to open a popup with full step details.
- Dead-scan behavior (still separate, manual-only, unchanged): movies removed from Dispatcharr are marked `dead=1`, deactivated; if previously activated, also removed from Plex and STRM moved to `.dead/`.

### Dispatcharr IP Display (v0.27.1)
- Header shows VPN IP from gluetun control API (`GLUETUN_API_URL`, default `http://192.168.1.94:7000`)
- Gluetun port 8000 mapped to host port 7000 on .94
- Endpoint: `GET /api/vpn-ip` → `{ip, country, city}`
- Refreshes every 60 seconds. Hover shows city/country. Red text if unavailable.

### SQLite Configuration (v0.27.1)
- **Singleton connection pattern**: Single persistent `_db` global in database.py — all code shares one connection
- WAL journal mode for concurrent read/write
- 30-second busy_timeout
- Root cause of "database is locked": multiple `get_db()` calls created separate connections that deadlocked on write. Fixed by singleton + removing all `db.close()` calls across entire codebase.

### Multi-Select Browse Filters (v0.27.1)
- Language, Category, and Provider dropdowns support selecting multiple values simultaneously
- Custom CSS/JS checkbox dropdown widgets replacing native `<select>` elements
- Backend `list_movies()` accepts multiple `language`, `category_id`, and `account_id` query params via `getlist()`
- `msState` object (Sets) tracks selections; `buildMsDropdown()` renders checkbox panels
- Catalog summary "browse" links use `browseByCat()` which sets `msState.msCat`

### Provider Behavior (discovered 2026-06-26, updated 2026-06-27)
- Movies are NOT permanently burned — provider uses temporary blocks with cooldown
- HTTP 500 from proxy = provider blocking, not content removal
- Cross-instance access (same movie from .94 and .251 VPN IPs) triggers account-level ban
- **ROOT CAUSE of burns**: Rapid Dispatcharr session cycling. Single clean disconnect = movie survives. 6 sessions in 60 seconds = movie burned. Proven via manual probe testing on Quicksand (965133).
- **Fix**: PLEX_IDLE_TIMEOUT raised from 5s to 30s (Plex reads in bursts with 5-7s pauses between)
- Implication: 1-strike dead may need cooldown+retry mechanism later

### Confirmed Stable (2026-06-26)
- North of Nightfall (962642) played 13+ minutes with zero drops — first movie past the 10-minute wall
- Single persistent pipe, 440 KB/s download vs 368 KB/s target, ~43-71s buffer ahead of Plex
- No "database is locked" errors after singleton fix
- Full pipeline: Bridge → Dispatcharr → Provider, all green

### Persistent Buffer Cache (v0.27.2, 2026-06-27)
- On Plex stop/pause: upstream disconnects after PLEX_IDLE_TIMEOUT (30s), **buffer file stays on disk**
- Buffer files in `/data/buffers/` with `.meta` files tracking start offset
- On resume: if buffer covers requested range, pipe resumes downloading from buffer end offset
- If Plex requests past buffer end, old buffer replaced with new pipe from requested offset
- Hourly cleanup: buffer files with mtime > 24 hours are deleted (skips files with active pipes)
- `close_movie_pipe(keep_buffer=True)` for idle disconnects, `keep_buffer=False` for errors

### Dead Detection Fix (v0.27.2, 2026-06-27)
- `_handle_upstream_error()` was dead code — never called during playback. Only the in-memory circuit breaker recorded failures.
- Fixed: three new call sites — pipe error path, no-data path, and pipe manager error cleanup
- HTTP 5xx from provider now properly marks `stream_dead=1` and deactivates via `_deactivate_dead_movie()`

### Category Hide (v0.27.2, 2026-06-27)
- `vod_categories.hidden` column — categories can be hidden from Browse/Summary
- Hidden categories excluded from browse queries and catalog summary counts

### TMDB Title Search (v0.27.2, 2026-06-27)
- `search_tmdb_for_missing()` in scraper.py — finds TMDB metadata for movies missing tmdb_id
- Cleans title: strips year suffixes, brackets, quality/codec tags
- TMDB search API → word-overlap similarity scoring (threshold 0.5, +0.3 year bonus)
- `tmdb_searched` DB flag (INTEGER DEFAULT 0) prevents retrying failed lookups
- Runs automatically during catalog sync and scheduled refresh
- Manual trigger: "TMDB Search" button in UI, or `POST /api/movies/tmdb-search`
- Reset flags: `POST /api/movies/tmdb-search/reset`
- Results: found 7/9 missing movies on first run. 2 remain (likely non-English titles with no TMDB match)

### Browse Filter Dropdowns (v0.27.2, 2026-06-27)
- Provider and Category browse dropdowns now use `?browse=true` query param
- `GET /api/providers?browse=true` — INNER JOIN with movies table, only providers with catalog movies
- `GET /api/categories?browse=true` — INNER JOIN with movie_categories+movies, excludes hidden, real movie_count
- Languages dropdown was already catalog-only (queries movies table directly)
- Settings tab still shows ALL providers/categories for selection (uses endpoints without `?browse=true`)

### Catalog Summary Categories (v0.27.2, 2026-06-27)
- `/api/catalog/summary` categories changed from LEFT JOIN to INNER JOIN
- Only categories with movies in catalog appear in summary
- Hidden categories excluded from summary
- "Categories currently in catalog" centered header label added
- Browse/hide buttons removed from summary (remain in Settings category grid)
- Future: make summary categories clickable to jump to Browse filtered view

### Catalog Import Flow (v0.28.2)
- Two-step selection: providers first, then categories. No providers selected = no categories shown.
- Category grid shows "Select a provider to see categories." when no providers are selected.
- Clearing all providers auto-deselects all categories in the DB.
- Deselecting the last provider also auto-clears category selections.
- Categories shown are the union of all selected providers' categories.
- `category_mapping.json` (from cron dump) is the source of truth for category→movie ID mapping, not the `movie_categories` DB table.
- When all providers are selected, the provider intersection filter is skipped entirely (redundant — all movies qualify).
- Provider filter only applies when a strict subset of providers is selected, using `stream_mapping.json` to identify which movies belong to which account.
- UI validates at least one category is selected before allowing Sync Selected.
- Sync endpoint returns 400 with error message if no categories are selected.

### UI Auto-Refresh (v0.28.3)
- `loadCatalogSummary()` called after all data-changing actions: sync, clear catalog, activate/deactivate, language detection, scan dead, TMDB search, delete/restore dead movies
- Language detection progress: `_langWasActive` flag tracks transition from detecting→complete, triggers `loadMovies()` + `loadCatalogSummary()` + `refreshLanguageFilter()` when detection finishes
- Clear catalog now resets `lang_status` in sync_state (no stale "350 detected, 360 skipped" after clear)

### TMDB Language Detection (v0.28.4)
- **Background-only** — no overlay, no blocking. Fires after catalog sync, scheduled refresh, and manual "Detect All Languages" button
- **Concurrency guard** — `_lang_detect_running` flag prevents duplicate runs
- **Safe pace**: 1 request per 0.5s (~2/sec), well under TMDB 40 req/10s limit. Zero 429s expected.
- 5 retries on HTTP 429 with `Retry-After` + 1s padding
- **ETA in status bar**: "Detecting languages in background: 500/2300 (480 detected, ~15 min remaining)..."
- Status bar auto-polls while detecting (amber pulsing dot + "Detecting languages...")
- When detection finishes, UI auto-refreshes: movie cards, language filter, catalog summary (`_langWasActive` transition)
- Final status shows elapsed time: "Language detection complete: 2200 detected, 80 not found, 20 no TMDB ID (19.2 min)"
- **Catalog sync no longer blocks on lang detection** — sync overlay closes when scraping is done, lang continues in background
- Scheduled refresh triggers lang detection as step 8 (background)
- **Unknown filter (2026-07-03)**: movies with no detected language are now a real selectable "Unknown" filter item in Browse (`GET /api/languages` appends `{"language": "unknown", "cnt": N}`; `list_movies()` treats `"unknown"` as `language = '' OR language IS NULL`), not just a display-only count. Catalog summary panel label changed "Undetected" → "Unknown".

### Plex Now Playing Dashboard (v0.30.0, 2026-06-27)
- New panel on Streams tab below Active Streams: "Plex Now Playing"
- `GET /api/plex/sessions` queries Plex `/status/sessions?X-Plex-Token=TOKEN` API
- Categorizes sessions: **Bridge Content** (file path contains `vod-bridge` from rclone mount) vs **Local / Live TV** (everything else)
- Each session card shows: title, year, play state (playing/paused/buffering with animated dot), position/duration progress bar, player name+device, local vs remote, video codec+resolution+transcode decision, audio codec+channels+transcode decision, bandwidth
- Bridge sessions get green left border, local sessions get blue
- Auto-refresh every 3s (independent toggle from Active Streams)
- Streams tab badge shows `max(pipe_count, plex_session_count)` — badge persists even after bridge pipe disconnects
- **Informational only** — does NOT keep upstream pipe open or delay idle timeout. The 30s disconnect is correct behavior; Plex buffers locally.
- Proven use case: remote user in Florida streaming through bridge from Georgia — Active Streams showed "No active streams" (pipe disconnected after 30s idle) while Plex Now Playing showed movie still playing at 41:43/1:54:38

### Error Screens for Plex (v0.35.0, 2026-06-28)
- When a movie can't play, Plex receives a 10-second MP4 video with a user-friendly error message instead of an HTTP error code
- Three screen types: **dead** (red, "Movie Temporarily Removed"), **busy** (amber, "Streaming Service Busy"), **removed** (gray, "Content Not Available")
- Generated at container startup using Pillow + FFmpeg, cached in `/data/error_screens/`
- Background image: `app/assets/bg.jpg` (cinematic bokeh), card overlay with accent-colored border
- `_error_screen_response()` in proxy.py handles Range requests and HEAD so Plex treats it as a normal video file
- Error path mapping: dead/burned movies → "dead" screen, circuit breaker/no stream/pipe failures → "busy" screen, HTTP 5xx from provider → "dead" screen
- Falls back to plain HTTP error responses if FFmpeg fails or screens aren't generated
- Dockerfile adds `ffmpeg` and `fonts-dejavu-core`, requirements.txt adds `Pillow`

### Category Removal (v0.35.1, 2026-06-28)
- Settings category panel: red "Remove" button toggles remove mode
- Remove mode: checkboxes on category chips, multi-select, action bar with "Remove Selected" / "Cancel"
- `POST /api/categories/remove` accepts `{category_ids: [...]}`
- **Exclusive movies** (only in selected categories): deleted from DB, deactivated, STRMs removed, removed from Plex
- **Shared movies** (also in other categories): unlinked from removed categories but kept in catalog
- Category rows deleted from `vod_categories`, `movie_categories`, `selected_categories`
- Confirmation dialog shows category names and estimated movie count
- After removal: reloads categories, catalog summary, browse filters

### Multi-Client Streaming — Client Labels + Concurrent Pipe Cap (v0.35.5, 2026-07-02)
- **Client label**: `_client_label(request)` in proxy.py derives "Plex"/"Jellyfin"/"Unknown" from the request's `User-Agent` header. Stored on `StreamPipe.client`, included in `status_dict()`, surfaced as a colored badge next to the movie title in the Active Streams panel (index.html).
- **Same movie, multiple clients/requests**: still just ONE shared pipe/buffer per movie (unchanged design) — any number of requests for the same movie (Plex, Jellyfin, both, repeated requests) reuse the existing pipe with zero new provider connections, as long as the requested byte offset is close to what's already buffered. Reusing the pipe refreshes `client` to whichever caller asked most recently.
- **Global concurrent-pipe cap**: `MAX_CONCURRENT_PIPES = 4` in proxy.py — hard limit on distinct movies streaming at once (not per-client, per-movie). A request for a 5th distinct movie while 4 others already have open pipes raises `PipeCapReached`, which returns a clean 503 "Maximum concurrent streams in use" response — does NOT record a circuit-breaker failure strike or mark the movie dead, since the cap has nothing to do with that specific movie's health.
- Far-seek behavior on an already-open movie is unchanged: still closes and restarts that same single pipe from the new offset (never counted as a second pipe for that movie).
- **Reopen grace period**: `REOPEN_GRACE_SECS = 2` — after closing an existing pipe to make room for a new one (far-seek or new client on the same movie), the bridge waits 2s before opening the replacement session. Root cause fixed: a fast client handoff (e.g. Jellyfin finishes/seeks, Plex immediately requests the same movie) could open a second Dispatcharr/provider session before the first one was fully released, and the provider — not Dispatcharr — rejected it as a second concurrent connection on the same stream. Movie wasn't actually dead/burned (played fine directly via Dispatcharr) — this was a timing race, not a content problem.
- **Finished-pipe cleanup bug fixed**: previously, once a pipe finished downloading (100%), it was NEVER removed by the idle-cleanup loop (`_pipe_manager_loop` only cleaned up idle pipes with `not pipe.finished`) — it lingered in `_movie_pipes` showing as an active "COMPLETE" card in Active Streams for up to 15 minutes after the client stopped reading, and kept occupying a `MAX_CONCURRENT_PIPES` slot the whole time. Fixed: finished pipes now get swept after the normal 30s Plex-idle window too (`finished_idle` cleanup reason), same as active pipes — buffer stays on disk, removed from the active pipe dict. Resuming later re-serves from the on-disk buffer file with no new provider connection (existing disk-buffer fallback path in `vod_file()`).

### Browse Provider Filter Fix (v0.35.6, 2026-07-03)
- **Bug**: `loadMovies()` in index.html silently fell back to filtering Browse results by the Settings-tab "selected for catalog import" providers whenever no explicit Browse provider filter was set. Result: any already-activated movie whose provider was later deselected/removed in Settings became permanently invisible in Browse (search included too), with the activated/STRM header counters (which don't use this filter) showing a higher count than the grid actually rendered — no way to deactivate the "missing" movie from the UI.
- **Fix**: removed the Settings-provider fallback entirely. "All Providers" in Browse now always means all providers, regardless of what's selected in Settings for import.
- **Real case**: "Girl in the Closet" (movie id 498059) was invisible in Browse due to this bug; manually deactivated via direct API call as a one-off fix (STRM + Plex entry removed).
