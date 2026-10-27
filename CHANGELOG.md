# Changelog

All notable changes to VOD Plex Bridge. Newest first.

Versions follow `MAJOR.MINOR.PATCH`. A minor bump (0.36 → 0.37) marks a major feature. A patch bump (0.38.0 → 0.38.1) marks an incremental change or fix. Every push to `main` publishes a `-beta.N` pre-release and the `:beta` image. A published release builds the `X.Y.Z`, `X.Y` and `latest` images.

---

## Why the jump from 0.35 to 0.38

The last stable release was **v0.35.6**, and the last pre-release was **v0.35.9-beta.1**. Versions 0.36.0 through 0.38.1 were developed and run locally, and none of them was pushed until then. **v0.38.1 was the first published release since 0.35.9.** It contains everything from 0.36.0 to 0.38.1 in one update.

The three minor bumps each mark a major feature:

| Version | Major feature |
|---|---|
| **0.36** | TV series support, redirect streaming mode, sign-in, local TMDB library, Maintenance tab |
| **0.37** | Settings tab in the app with encrypted secrets, Daily Updates tab, account menu |
| **0.38** | Multiple Dispatcharr instances, with provider accounts as a separate (future) list |

### Upgrade notes (from 0.35.x)

- **Sign-in is now required.** On first start you create the admin account on the sign-in page.
- **Settings move into the app.** On first start, the Dispatcharr URL, API key and XC values from your environment (or from earlier in-app settings) are migrated into **Settings → Dispatcharr** as the primary instance. Secrets are stored encrypted, with a key file in `DATA_DIR`, or `SETTINGS_KEY` if you set it. After migration the matching environment variables are no longer needed, but they do no harm if left in place.
- **Changing the primary Dispatcharr is risky after movies are activated.** Movie IDs differ between Dispatcharr instances. The next catalog refresh removes movies the new primary does not have, including activated ones. Switch only to the same Dispatcharr at a new address, or before you activate anything.
- **Providers (XC) are read-only for now.** See 0.38.1.
- **rclone is still required** for Plex or Jellyfin to see the bridge's movie and series folders. You install and run it separately; see the [User Guide](docs/USER_GUIDE.md). A future version is planned to have rclone built in.
- **Series need their own Plex TV library**, either per category or through the `PLEX_SERIES_LIBRARY_ID` fallback, plus an rclone mount of `/vod-series/`.

---

## [0.43.0] — 2026-10-27

### Added
- **Scheduled catalog syncs.** **Settings → Sync schedule** sets how often the movie catalog and the series catalog sync from Dispatcharr: Off, or every 1, 2, 3, 4, 6, 8, 12 or 24 hours, each on its own. New, removed and renamed titles then show up during the day instead of waiting for the 6:00 AM Daily Run. A scheduled sync does the same work as the **Sync** button, including metadata enrichment. It is skipped while the same sync or the Daily Run is running, and starts on the next minute's check once that finishes. Saving a new interval restarts its clock, so the first sync runs one interval after you save. Both are Off by default.

## [0.42.0] — 2026-10-27

### Added
- **Titles and posters come from TMDB, for movies and series.** Dispatcharr keeps the name a title had when it was first imported, so a title imported under another language's name (for example "30 Monedas (es)") kept that name in Plex. The bridge now uses TMDB's title for anything with a TMDB ID, so names are consistent whatever languages your categories allow. Lookups use the local TMDB library first and call TMDB only on a miss. Locked titles, and titles without a TMDB ID, keep Dispatcharr's data. Show folder names never change. A renamed movie gets a new file name under the same `{tmdb-<id>}` hint, so Plex matches it to the same item on its next scan.
- **Series TMDB enrichment.** Series now get TMDB's title, poster, plot, genre and year, the same as movies. It runs after a series catalog sync and in the daily run. Every series with a TMDB ID is checked once, and again only when Dispatcharr's data for it changes. Re-syncing no longer reverts a TMDB title. An existing `tvshow.nfo` is rewritten in place when its metadata changes.

### Fixed
- **Provider posters that no longer load were kept.** A TMDB title-search match now replaces the provider's poster instead of only filling a missing one. Series on dead provider image links get TMDB's poster from series enrichment.
- **Status stuck on "Enriching..." after a catalog sync.** The sync now finishes as idle with a summary, for example `Synced … · Metadata: 120 filled from Dispatcharr, 85 updated from TMDB, 4,200 queued for the next run`.
- **Most movies never got TMDB metadata.** TMDB enrichment only checked movies whose Dispatcharr data had changed, so movies imported before that change tracking existed were never enriched. Every movie with a TMDB ID that hasn't been checked is now queued. Each run works through up to 5,000, answered from the local TMDB library first. A TMDB ID that returns 404 is checked once and not asked again. A temporary TMDB error leaves the movie queued.
- **Each sync re-asked the same 1,000 movies for provider metadata.** The provider-info pass picked movies by missing fields, and most stayed "missing" because the provider doesn't supply some of them (backdrops, for example). Each call can reach the provider through Dispatcharr, so the same movies were re-fetched on every sync. Each movie is now asked once (activated movies first, then newest), and asked again only when Dispatcharr's data for it changes. Activating a movie still fetches its stream details on demand.
- **Language detection retried the same titles every run.** Titles that TMDB doesn't have, or that have no language, are now checked once. The language is also taken from the TMDB metadata pass, so most movies don't need a separate lookup.
- TMDB title search now works through activated and newest movies first instead of in arbitrary order.

## [0.41.2] — 2026-10-26

### Added
- **Activity indicator.** A spinning pill next to **Scan Plex Library** shows what's running (activating, moving or deactivating movies or episodes) and ends with a ✓ or ✕ result. Cards and counts update without reloading the page.
- **Card Activate asks where the movie goes.** The card's **Activate** button opens a window with **Auto category**, **Default library**, every movie category, and **+ New category…**, which creates the category and its Plex library and activates the movie into it in one step.
- **Click a movie category to see its movies.** Clicking a row in **Movie Categories** (Default included) shows only the movies active in that library, like series categories. Click it again or **Back to catalog** to return.

### Changed
- **Plex matches bridge titles by TMDB ID.** Movie files and series show folders, as the bridge lists them to Plex, now carry Plex's exact-match hint, for example `The Lost Ones (2014) {tmdb-433534} [3331028].mp4` and `Verna {tmdb-480014}/`. Plex no longer guesses from title and year, which picked the wrong film when the provider's year differed from TMDB's. Titles without a TMDB ID keep the old name. Only the names Plex sees change: the bridge's STRM folders, your other Plex libraries and the `[id]` that links a file back to the bridge stay the same.
- **New logo.** The VPB logo now sits next to the app name in the top-left corner and on the sign-in page, and is used for the browser tab icon (favicon), the home-screen icons and the README.

### Fixed
- **A newly activated movie sometimes didn't appear in Plex.** The rclone mount on the Plex host caches each folder listing for a minute. A scan fired right after activation could read the old listing and find nothing new, which was more likely when the folder had just been listed. The bridge now scans again about 70 seconds later, once that cache has expired. Several activations in a row share one follow-up scan per library.
- **Slow Plex scans after activation in redirect mode.** Redirect mode sent Plex's scan reads of a movie's start and end to Dispatcharr instead of answering them from the copy saved at activation. Those reads are now served by the bridge in both modes; only playback is redirected.
- **Now Playing listed bridge movies under "Other media".** Sessions are now also matched by Plex library, so a bridge movie whose file path doesn't match still shows as a bridge title.
- The generated Command B rclone mounts now use `--timeout 30s`, so a stalled read fails and retries instead of hanging a Plex scan for the 5-minute default.

### Upgrade notes
- This release also includes 0.41.1, which was never published on its own.
- **Already-matched titles keep their Plex match.** The new names appear on the next scan, but Plex only uses the TMDB hint when it matches an item for the first time. If a title is already matched to the wrong film, use **Fix Match** in Plex and search `{tmdb-<id>}` (for example `{tmdb-433534}`), or remove it from the library and scan again. Empty the library's trash afterwards if an old entry lingers.
- To pick up `--timeout 30s` on an existing Plex/Jellyfin host, run **Command B** again from the Setup tab. Nothing else is needed.

---

## [0.41.1] — 2026-10-26

### Changed
- **Movie Categories panel is now on the Movies tab.** It sits at the top of the Movies tab, collapsible, the same way **Series Categories** sits at the top of the Series tab. Add a category there and it appears in the selection bar's picker next to **Auto category** and **Default library**. It used to be on the Catalog tab, so the Movies tab only offered Auto and Default.

---

## [0.41.0] — 2026-10-26

### Added
- **Movie categories.** Movies can now be split into categories the way series are. Each category has its own folder under `/vod-movies/<folder>/` and its own Plex Movies library, which the bridge can create for you. Set them up in the new **Movie Categories** panel (moved to the top of the Movies tab in 0.41.1). A category can list provider categories, so **Auto category** activation sends matching movies to it. Everything else stays in the **Default** library at `/vod/`, as before. A movie is only ever in one place, so it never shows up twice in Plex.
- **Category picker and Move in the Movies selection bar.** Activate into Auto, Default or a named category. **Move** sends already-active movies to another category. Cards show which category a movie is in.
- **Settings → Plex movie category mount root** (default `/mnt/vod-bridge-movies`), plus a matching folder field on each machine card in the Setup tab.
- **Jellyfin and Emby in Now Playing and Health.** Add their URL and API key under **Settings → Media servers**. Their sessions appear in **Streams → Now Playing**, tagged by server, and each server gets a Health card.
- **Every section and panel can be collapsed.** Panels start closed, and the browser remembers which ones you opened.

### Changed
- The series category table shows **Active Shows** and **Active Episodes** instead of the total series count.

### Upgrade notes
- **Run Command B again** on each Plex/Jellyfin machine (Setup tab). It adds the third rclone mount, `/mnt/vod-bridge-movies`, that category libraries point at. Existing mounts are left as they are. If Plex or Jellyfin runs in Docker, also add the `/mnt/vod-bridge-movies:/mnt/vod-bridge-movies:ro,rslave` volume line and recreate the container. See "Upgrading from 0.40 or earlier" in INSTALL.md.
- Movies that were active before the upgrade stay in the Default library. Nothing moves until you use **Move**.
- Jellyfin libraries for movie categories are still added by hand. Point a Movies library at the category's sub-folder of `/mnt/vod-bridge-movies`.

---

## [0.40.1] — 2026-10-26

### Added
- **Display size (Settings → Display).** Auto, the default, scales the whole app up on windows wider than 1920 pixels, up to 200%. Phones and normal screens stay at 100%. A fixed size from 80% to 200% can be chosen instead. The choice is saved per browser.

### Changed
- **Clear Catalog** and **Clear STRMs** now appear only on the Catalog tab.
- On phones the header wraps onto several lines, so the page no longer scrolls sideways.

---

## [0.40.0] — 2026-10-25

### Added
- **Setup tab.** A five-step guide that opens on first sign-in: bridge address, Dispatcharr host, media server machines, libraries, done. Each step has a light that turns green when the bridge confirms it worked.
- **Command A for the Dispatcharr host.** One pasted command installs a small agent with `/etc/cron.d/vod-plex-bridge`. The agent makes the mapping files and uploads them to the bridge.
  - It checks every minute whether the Daily Run or **Run Now** wants fresh files, and sends them every 12 hours anyway.
  - It no longer matters whether the bridge's data folder is on the Dispatcharr host. The old `.dump-trigger` file and hand-made cron lines still work.
- **Command B for each Plex/Jellyfin machine.** One pasted command per machine card does the rclone setup that used to be done by hand. It:
  - installs rclone and FUSE,
  - adds the `vodbridge-movies` and `vodbridge-series` remotes,
  - starts `rclone-vod-bridge-movies.service` and `rclone-vod-bridge-series.service`,
  - checks the mounts as the Plex/Jellyfin user and reports back.
  - Notes:
    - Working older mounts are left alone.
    - In a Proxmox LXC without FUSE, it says how to turn FUSE on.
    - Safe to run again.
- **Optional Rclone Manager** per machine, installed by Command B when ticked on the card first. A new **Maintenance → Rclone** sub-tab and the Health tab link to it.
- **Create Plex movie library** in Setup step 4. It makes the Movies library at the movie folder and saves its ID in Settings.
- **Filled-in manual steps** for Unraid, Windows, macOS and NAS media machines, and for Plex/Jellyfin in Docker. They are marked untested.
- **Private setup link.** The commands contain a link that can be replaced from the Setup tab; old commands then stop working.
- Screenshots and diagrams in the docs.

### Changed
- **INSTALL.md rewritten** around the Setup tab, with step-by-step Plex and Jellyfin library instructions and troubleshooting. The README quick start and the User Guide follow it.
- **Docs now say plainly that the bridge is for a few titles at a time** (2–3 movies, or one season of 10–15 episodes), and that every activation and every Plex/Jellyfin scan of a new title uses a Dispatcharr connection slot. Redirect mode is marked "use with caution", with a VPN on the Plex/Jellyfin machine recommended.
- **The compose example uses the published image** (`ghcr.io/knmplace/vod-plex-bridge`) instead of building from a clone.
- Mount roots in Settings can be Windows drive paths (for example `V:\`).
- Deleting a Plex library waits up to 60 seconds, since Plex can be slow to drop one.

### Removed
- The "rclone must be set up by hand" requirement. A manual setup is still described in INSTALL.md for anyone who'd rather not paste a command.

---

## [0.39.0] — 2026-10-25

### Added
- **Redirect targets in the proxy log.** In redirect mode, the log line now names the account and stream the playback went to, for example "via Account A, stream 1234".
  - It notes when a fallback account was used because the primary was full, and when all accounts were full.

### Changed
- **One redirect log line per playback.** Plex re-requests a title every minute or two while it buffers, and each repeat used to add another line. Now a redirect is logged once, then again only after 15 minutes of quiet, or as "Redirect switched to …" when the stream changes.

### Removed
- The **Detect Language** button in the Movies selection bar, and the API endpoint behind it. The Daily Run detects languages for new movies, and per-movie detection is still available from a movie's details.

---

## [0.38.1] — 2026-10-25

### Changed
- **Settings → Providers (XC)** is now read-only and labeled with a **Future — pending** badge.
  - Every stream runs through Dispatcharr, which holds the provider logins itself. The bridge never contacts a provider, so logins entered here would never be used.
  - The list is grayed out and has no row actions. The Add button is disabled.
  - The API refuses provider writes with a 403 error. Migrated rows are still listed.

## [0.38.0] — 2026-10-25

### Added
- **Multiple Dispatcharr instances** under Settings → Dispatcharr, each with Test, Make primary, Edit and Delete.
  - The primary instance drives catalog sync and playback.
  - The first instance you add becomes primary. Deleting the primary promotes the next one.
  - A secret left blank on edit keeps its saved value, and **Clear saved value** removes it.
- **Providers (XC)** as a separate list (name, server, login, max streams, priority, active), stored encrypted.
- A one-time migration of the earlier Dispatcharr and XC settings into these lists.

### Changed
- Settings layout is left-aligned and wider.
- The connection-mode switch is removed. Dispatcharr is always the source.
- References to compose files are removed from the UI.

## [0.37.0] — 2026-10-25

### Added
- **Settings tab** with a sidebar per service: Dispatcharr, Plex, TMDB, streaming and paths.
  - Values apply right away, without a restart, except the redirect mode.
  - Keys, tokens and passwords are encrypted at rest, are never shown again, and can be rotated.
  - Plex Test shows the series category → library mapping.
- **Daily Updates tab** under Maintenance. The Daily Run settings and history moved here.
- **Account menu** with a profile and password change.
- The Daily Run report shows TMDB enrichment by source (local library or API call) and which fields changed.
- **TMDB title search** runs inside the Daily Run (step 5a), local library first, and tries each title once.

### Changed
- UI text is brighter and larger.
- The manual "Detect All Languages" and "TMDB Search" buttons are removed, because the Daily Run covers both.
- A saved Plex token is used on the next call, with no restart needed.

## [0.36.0] — 2026-10-24

### Added
- **TV series support**: series scraping, episodes, series STRM/NFO folders and the `/vod-series/` listing for rclone.
  - Series categories, with pruning outside the selected ones.
  - Per-category Plex TV libraries.
  - Season and show cleanup.
- **Redirect streaming mode** (`REDIRECT_MODE`) issues a 302 to Dispatcharr's VOD proxy, with a per-title session cap (`MAX_CONCURRENT_PER_TITLE`). The default mode is still the proxy pipe.
- **Sign-in page**, with the account created on first run.
- **Movies tab** with redesigned cards, trailers, and genre, year and activation filters.
  - An **IN PLEX** badge from Plex presence.
  - Multi-select delete.
- **Maintenance tab**. Proxy log archives and run history can be deleted, one at a time or several at once.
- **TMDB Local** tab and a local TMDB library. Daily-run TMDB enrichment uses the local library first.
- A provider-only panel with lock/unlock. Locked titles skip language detection and TMDB search.
- Adult categories are hidden.

### Changed
- **Dispatcharr is the source of truth at import.** TMDB re-checks run only when a movie's Dispatcharr data changes, tracked by a hash.
- Metadata comes from the host dump, through one shared extraction path for catalog refresh and full sync. This removes thousands of false TMDB re-checks per run.
- The host dump script is hardened.

### Fixed
- Bulk Restore All and Delete All on Dead Movies.
- Plex removal on deactivation.
- Episode activation.

---

## [0.35.9] — 2026-07-04
### Fixed
- Reverted the upstream stall watchdog. A short gap in provider data during playback no longer marks a movie dead. Only a real 5xx from upstream does.

## [0.35.8] — 2026-07-04
### Added
- A loud warning in the Daily Run report when dump regeneration fails or times out, so a stale catalog snapshot is visible.
### Fixed
- The stream-mapping "updated" count now counts only real changes.

## [0.35.7] — 2026-07-04
### Fixed
- Orphaned STRM folders after deactivation. Movies now store a stable `strm_folder`.
- Catalog refresh now fully removes movies that are gone from Dispatcharr (Plex, STRM and database row). Dead movies that are still present get a "try restoring" note.

## [0.35.6] — 2026-07-03
### Fixed
- Several clients can stream the same movie at once.

## [0.35.5] — 2026-07-03
### Fixed
- Browse provider filter.

## [0.35.4] — 2026-07-02
### Added
- **Daily Run** at 6am: dump regen, categories, catalog, stream mapping, provider info, stream-id refresh and language detection, plus a manual Run Now.

## [0.35.3]
### Fixed
- Restoring a dead movie.

## [0.35.2]
### Changed
- Adaptive throttle peak raised to 2.3x.

## [0.35.1]
### Added
- Error screens for Plex (a short MP4 card for dead, busy or removed movies) instead of HTTP errors.
- Category removal.

## [0.34.0] — 2026-06-28
### Added
- Provider fallback: several streams per movie, with automatic failover on a 500.
- Title parsing, a sticky header, and the CI workflow (beta pre-releases and multi-arch images).

## [0.32.0]
### Added
- Safe refresh with no provider calls.
- Provider-info enrichment.
- 302 redirect mode (later reverted, then reintroduced in 0.36.0 as an option).

## [0.29.0]
### Added
- Rewritten docs, BUILD_SOP, rclone setup docs and Plex Now Playing.
- STRM files are created on activation.
- Probe throttle.

## [0.28.0 – 0.28.5] — 2026-06-27
### Added
- Proxy log archives.
- Catalog sync from the dump file.
- Category counts by provider.
- Configurable `TZ`.
- TMDB language detection, rate-limited and run in the background.

## [0.27.2]
### Fixed
- Raised the idle timeout to stop session cycling, which was getting movies blocked.

## [0.27.1] — 2026-06-26
### Added
- Singleton database connection and multi-select filters.

## [0.23.0] — 2026-06-24
### Added
- One persistent streaming connection per movie, with a wall-clock rate limiter.

## [0.17.1] — 2026-06-23
### Added
- Session reuse, endpoint routing, health checks and dead-stream management.

## [0.8.2] — 2026-06-22
### Added
- Stream caching, provider filtering and Plex auto-cleanup.
