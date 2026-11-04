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

## [0.45.2] — 2026-11-04

### Added
- **Continuing / Ended badge on series cards.** Each series card shows whether the show is still airing, using the show status from TMDB, the same information Sonarr shows. **Continuing** (green) covers TMDB's *Returning Series*, *In Production*, *Planned* and *Pilot*. **Ended** (red) covers *Ended* and *Canceled*. Shows without a TMDB match have no badge. The badge sits at the bottom right of the card, next to the language tag. Existing series get their status from the local TMDB library on the next series TMDB enrichment (after a series catalog sync or in the Daily Run), and only titles missing from it are fetched from TMDB. A show TMDB later marks as changed, for example when it is cancelled, is checked again on the next pass.

### Changed
- Episode lines in the proxy activity log now name the show, season and episode, for example "The Walking Dead - S02E05 - What Will Survive", in the same form as Now Playing. Before, they showed only the episode name.

### Fixed
- **A scheduled catalog sync no longer runs straight after a manual one.** Clicking **Sync Series Catalog** (or the movie catalog sync) didn't restart the sync schedule's clock. If a scheduled sync was due, it started as soon as yours finished, while your sync was still applying TMDB data. Any full sync now restarts the clock, and only one series sync runs at a time. A second request is ignored while one is still running.

## [0.45.1] — 2026-11-03

**0.44.0 and 0.45.0 were never released on their own.** They were developed and tested locally, and 0.45.1 is the first published release that contains them. If you're upgrading from 0.43.0, you get everything listed here, including the items marked *(0.44.0)* and *(0.45.0)*.

### Upgrade notes (from 0.43.0)
- Update the bridge image, then **run Command B again on each Plex/Jellyfin machine** (Settings → Setup). That adds the cache limit to the rclone mounts, installs the disk report timer for the disk gauge, and repairs a hand-made bridge mount whose remote went missing. Command A doesn't need to run again.
- After changing a machine's **Cache limit (GB)** later, press **Save** and run its Command B again.
- No database steps are needed. New tables and columns are added on first start, and episodes that are already active get their head/tail cache from the background queue, one short provider connection each.

### Added
- *(0.44.0)* **Shift+click** selects a range of movie cards.
- *(0.44.0)* **Activation popup.** Activating movies opens one popup with a library picker for all of them, a warning for two or more, and the free provider slots per account.
- *(0.44.0)* **Activation queue.** Movies are activated one at a time, 5 seconds apart. If no provider account has a free slot, the movie waits and is checked again every 10 minutes instead of failing. A queue pill shows progress; click it to see or remove queued movies.
- *(0.45.0)* **Activate several series at once.** Tick series cards (shift+click for a range), then **Activate** opens a wizard with one step per show and a Review step. Each step picks the library and either all seasons or the seasons and episodes you choose. Nothing is queued until **Add N episodes**. Episodes are added by a background queue, with one Plex scan per library when it finishes.
- *(0.45.0)* Series cards show provider and language tags.
- **The bridge warns when Plex can't see its folders.** Every 30 minutes, and in the Daily Run, the bridge asks Plex about each folder it serves (movies, movie categories, series) that has active titles. It checks that a Plex library uses the folder and that Plex can still read the newest file there. If the rclone mount on the Plex machine has stopped, a red banner appears at the top of every page. It names the folder and the Plex machine, and its **Show the setup command** button opens Settings → Setup at that machine's command, which restarts or repairs the mount. The Health tab shows the same live result, and the Daily Run report shows `Plex mounts: FAILED`. The check uses the Plex API and the bridge's database only; it never contacts a provider. The last result is at `GET /api/plex/mounts`, and **Check again** in the banner runs it at once.
- **Single-show view uses the wizard layout.** Clicking a series card opens the show in the Activate wizard's layout instead of the old episode table. Active episodes can be deactivated or reactivated there, and **Refresh episode list** re-reads the show from Dispatcharr.
- **Select all and shift+click ranges** for seasons and episodes in the wizard.
- **Series per page** selector (60, 100, 200 or 300), remembered by the browser.
- **Back to top** button on every page.
- **Episodes get a head/tail cache, like movies.** After episodes are added, the bridge reads the start (6 MB) and end (256 KB) of each one and stores them with its real file size. Plex's scans and analysis then read those parts from the bridge instead of the provider. The reads go one at a time through the activation queue, 5 seconds apart, before the series Plex scan (which waits at most 15 minutes for them), and only while a provider account has a free connection. Episodes played in the last 3 hours are left alone. A failed read is retried after 1 hour, 6 hours, then daily, and never marks anything dead. Episodes already active when you update are cached the same way.
- The wizard's Review step says that adding episodes opens one short provider connection per episode, with an extra warning for batches over 20 episodes.
- **Cache limit for the rclone mounts.** Each media machine's Setup card has a **Cache limit (GB)** setting (default 20 GB, shared by that machine's mounts). Command B writes it into the mounts as `--vfs-cache-max-size`. With rclone 1.64 or newer it also adds `--vfs-cache-min-free-space 5G`. The full rclone cache stays on because it lets Plex seek without opening new provider connections. Copies are still removed 10 minutes after playback stops, the oldest go first once the limit is reached, and a title that is still playing is never removed. **After changing the limit, press Save and run Command B again on that machine.** The limit only takes effect when you do, and until then the Setup and Health tabs say the machine still uses the old limit. A working hand-made rclone unit that Command B leaves alone doesn't get the limit; Command B warns about it, and `--replace` swaps it for the bridge's own.
- **Disk gauge for media machines.** Command B installs `vod-bridge-disk-report.timer`, which sends the machine's disk size, free space and rclone cache size to the bridge every 5 minutes. The cache size includes a hand-made bridge mount that Command B left in place. A gauge on the Health tab and on the machine's Setup card shows them, and turns yellow below 20% free and red below 10%. Nothing else is sent. Run Command B again on existing machines to get it.

### Changed
- **A movie's head/tail cache is refilled after a failover.** Switching to another stream, or a new UUID from Dispatcharr, clears the cache, and until now it stayed empty, so Plex's later scans reached the provider. The activation queue now refills it the same way as episodes, skipping movies that are playing or were played in the last 3 hours.
- An episode's cache is cleared when Dispatcharr gives it a new UUID, when it is deactivated, and when playback switches to a different provider account than the one the cache came from.
- Now Playing shows the show name for Plex episodes, in the same form as Jellyfin: "Show - S01E02 - Episode".

### Fixed
- **Command B (media setup) could break a hand-made rclone mount.** When it rewrote `rclone.conf`, it removed every remote named `vodbridge-movies`, `vodbridge-movies-cat` or `vodbridge-series`, including one that a separate, hand-made rclone unit used. The running mount kept working until its next start, often a reboot, and then failed on every restart while Plex showed an empty library. Command B now leaves any remote that another rclone unit uses, and its own mount takes a spare `-setup` name if needed. If a hand-made unit's bridge remote is already missing, Command B puts it back and restarts the unit, so re-running Command B repairs an affected host. Its final check also reports any rclone unit whose remote is missing from its config. This only affected hosts with a hand-made rclone unit that used one of those exact remote names. Command B is served by the bridge, so updating the bridge updates it.
- When Command B asks to replace a hand-made rclone unit that isn't working, it now shows that unit's actual `ExecStart` line (it showed `ExecStartPre` before).

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
