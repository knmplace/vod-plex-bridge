# VOD Plex Bridge — User Guide

This guide covers VOD Plex Bridge **v0.39.0**. It explains what the app does, how to set it up, and how to use each tab. For a first-time install (Docker, compose, the mapping dump script), see [INSTALL.md](../INSTALL.md). Come back here once the container is running.

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `http://<bridge-host>:8585` | The machine running the VOD Plex Bridge container |
| `http://dispatcharr:9191` | Your Dispatcharr instance (a container name works if both are on the same Docker network) |
| `http://<plex-host>:32400` | Your Plex Media Server |

---

## 1. What the app does

VOD Plex Bridge takes the movies and TV series that Dispatcharr already knows about from your provider and makes them appear in Plex as normal library items. You choose which titles to **activate**. Only activated titles show up in Plex.

### The data path

```
Provider  ->  Dispatcharr  ->  VOD Plex Bridge  ->  rclone (on the Plex host)  ->  Plex
```

1. **Dispatcharr** holds the VOD catalog and is the only thing that talks to your provider. All video traffic goes through Dispatcharr's VOD proxy. The bridge never connects to provider URLs directly.
2. **The bridge** reads the catalog from the Dispatcharr API and stores it in its own database (`/data/vod_bridge.db`). When you activate a title, it writes a STRM file, an NFO file, and a poster into its output folder (`/plex-vod/Movies` or `/plex-vod/Series` inside the container).
3. The bridge publishes activated titles as a plain HTTP directory listing:
   - Movies: `http://<bridge-host>:8585/vod/`
   - Series: `http://<bridge-host>:8585/vod-series/`
4. **rclone**, running on the Plex host, mounts those listings as folders (for example `/mnt/vod-bridge`). It uses rclone's read-only HTTP backend.
5. **Plex** scans those folders like any other movie or TV library. When someone presses play, Plex reads the file through rclone. The bridge then streams the video from Dispatcharr.

No network share (NAS, SMB/CIFS, NFS) is involved. The only link between the bridge and Plex is HTTP.

---

## 2. rclone is required

> **Important:** Plex cannot see anything from the bridge unless rclone is installed and mounted **on the Plex host**. This is not optional. Without it, your Plex library will stay empty no matter what you activate.
>
> **Coming in a future release:** built-in rclone support, so the bridge can handle this step for you. Until then, set it up by hand as shown below.

Run all of the following **on the Plex host**, not the bridge host.

### Install

```bash
apt install rclone fuse3
# or the latest build from https://rclone.org/install/
```

If Plex runs as a non-root user, allow FUSE mounts to be shared with other users:

```bash
sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf
```

### Configure the remotes

Edit `/root/.config/rclone/rclone.conf`:

```ini
[vodbridge]
type = http
url = http://<bridge-host>:8585/vod/

[vodbridge-series]
type = http
url = http://<bridge-host>:8585/vod-series/
```

Test it with `rclone ls vodbridge:`. An empty result is fine if nothing is activated yet. An error means the Plex host can't reach the bridge.

### Mount as a service (movies)

`/etc/systemd/system/rclone-vodbridge.service`:

```ini
[Unit]
Description=rclone VOD Bridge FUSE mount
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p /mnt/vod-bridge
ExecStart=/bin/rclone mount vodbridge: /mnt/vod-bridge --allow-other --dir-cache-time 1m --vfs-cache-mode full --vfs-cache-max-age 10m
ExecStop=/bin/fusermount -u /mnt/vod-bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now rclone-vodbridge
ls /mnt/vod-bridge/
```

For series, make a second unit the same way. Name it `rclone-vodbridge-series.service`, use `vodbridge-series:` as the remote, and `/mnt/vod-bridge-series` as the mount point.

Tips:
- Check the rclone path with `which rclone` and adjust `ExecStart` if it's somewhere else (often `/usr/bin/rclone`).
- On fuse3-only systems, use `fusermount3` in `ExecStop`.
- `--dir-cache-time 1m` means a newly activated title can take up to a minute to appear in the mount.

### Point Plex at the mounts

- Create a **Movies** library on `/mnt/vod-bridge`.
- Create a **TV Shows** library on `/mnt/vod-bridge-series`, or one per series category (see section 5).
- In each library's advanced settings, turn **off** video preview thumbnails, intro markers and credits markers. Otherwise Plex will try to read entire movies over the stream just to analyze them.

The mount paths you use here must match **Plex movie mount root** and **Plex series mount root** in Settings (defaults `/mnt/vod-bridge` and `/mnt/vod-bridge-series`).

---

## 3. First run: sign-in and your profile

### Create the account

The first time you open `http://<bridge-host>:8585`, you are asked to create the app's single account. Pick a username and a password of at least 8 characters. There is only one account; the app has no multiple users or roles.

### Signing in

- A sign-in lasts **30 days** per browser.
- Wrong passwords are slowed down on purpose (about a one-second delay each time).
- The web UI and its `/api` calls require sign-in. The file and stream paths that Plex and rclone use (`/vod/`, `/vod-series/`, `/stream/`) do **not**, so playback keeps working without a browser session.

> Because those paths have no login, don't expose port 8585 to the internet. Keep it on your LAN or behind a VPN.

### Profile page

Open the account menu (top right) and choose **Profile** to change your username, your password, or both. You must enter your current password to save changes. Changing the password **signs out every other browser**. Choose **Sign out** from the same menu to end your own session.

---

## 4. Settings

Open the **Settings** tab. Each section saves on its own. Most sections have a **Test** button that checks the connection using the values currently in the form.

> Saved values are used right away. Keys, tokens and passwords are stored encrypted and are never shown again. Leave a secret field blank to keep the saved value. Use **Clear saved value** to remove it.

### 4.1 Dispatcharr

You can store several Dispatcharr instances. **Only one is used at a time**: the one marked **primary** ("Used for catalog and playback"). The others are listed as **Standby**.

| Field | Notes |
|---|---|
| Name | Any label |
| URL | e.g. `http://dispatcharr:9191`. Must start with `http://` or `https://` |
| API key | Create one in Dispatcharr under **Settings > API Keys** |
| XC username / password | Optional. Only needed for older XC-style stream links |

Row actions: **Test**, **Make primary**, **Edit**, **Delete**.

- The first instance you add becomes primary automatically.
- If you delete the primary, the oldest remaining instance becomes primary.
- **Test** asks Dispatcharr for one movie. Possible results:
  - "Connected — N movies"
  - "Dispatcharr refused the API key"
  - "Dispatcharr answered HTTP N"
  - "Could not connect: ..."

> **Warning: switching the primary Dispatcharr.** Catalog sync and playback move to the new instance right away. Movie IDs are different between Dispatcharr instances. Only do this in one of these two cases:
> - it's the **same Dispatcharr at a new address**, or
> - you haven't activated any movies yet.
>
> Otherwise, the next catalog refresh will **remove every movie the new instance doesn't have, including activated ones**. They are taken out of Plex and their STRM folders are deleted.

If you use redirect streaming mode (see 4.5), the API key needs admin rights. The bridge uses it to close extra sessions on Dispatcharr.

### 4.2 Providers (XC): future feature

This section is shown greyed out with a **"Future — pending"** label, and the **Add** button is disabled. Right now every stream goes through Dispatcharr, so provider accounts are managed in Dispatcharr, not here.

If you had an older XC server setting, it may show up as a read-only row in this section. You don't need to do anything with it.

### 4.3 Plex

| Setting | Notes |
|---|---|
| Plex URL | e.g. `http://<plex-host>:32400` |
| Plex token | Secret. In Plex Web, open any item, choose **Get Info > View XML**, and copy the `X-Plex-Token` value from the URL |
| Movie library ID | The numeric section ID of your movie library (default `7`). It appears in the Plex Web URL: `.../library/sections/<id>/...` |
| Series library ID (fallback) | Used only for series categories that don't have their own library (see section 5) |
| Plex movie mount root | Where rclone mounts movies on the Plex host (default `/mnt/vod-bridge`) |
| Plex series mount root | Where rclone mounts series on the Plex host (default `/mnt/vod-bridge-series`) |

Library IDs must be numbers, and mount roots must start with `/`.

**Test** lists every Plex library with its ID and type. It also shows each series category with either its own library or "uses fallback", and warns about IDs that don't exist.

The bridge uses Plex to refresh libraries after you activate or deactivate titles, to remove deactivated items, to show Now Playing, and to mark titles that are already in Plex.

### 4.4 TMDB

Enter **either** of these:
- a **TMDB read access token** (recommended; long, starts with `eyJ`), or
- a **TMDB API key** (32 characters).

Get them from your TMDB account under **Settings > API**.

**Test** checks the credential against TMDB. Without TMDB, the app still works, but posters, genres and descriptions come only from the provider. The TMDB title search and enrichment steps of the Daily Run are also skipped.

### 4.5 Streaming

This section has no Test button.

| Setting | Notes |
|---|---|
| Stream mode | **Redirect** or **Proxy**. Takes effect only after a container restart. Until then, the page shows a "restart pending" note |
| Max connections per title | 1–20, default 2. Applies in redirect mode only. When a title goes over the limit, the oldest connections are closed |

The header badge shows which mode is currently running. See section 8 for how the two modes differ.

### 4.6 Secrets and environment variables

**Where values come from**

| Area | Behavior |
|---|---|
| Plex, TMDB, Streaming | A value saved in Settings wins. If a field is left blank, the matching value from your compose/`.env` file is used, and it keeps being used as the fallback. |
| Dispatcharr | On first start, `DISPATCHARR_URL`, `DISPATCHARR_API_KEY` and any XC values from the environment are copied **once** into an instance named "Dispatcharr". After that, edit it in Settings. Changing the environment variables later has no effect while an instance exists. |
| Paths, port, database location | Set in compose only. Not editable in the UI. |

**How secrets are protected**

- Saved keys, tokens and passwords are encrypted at rest.
- Set `SETTINGS_KEY` in your compose/`.env` file (recommended).
- If you don't, the bridge creates a `settings.key` file in its data folder and uses that. Anyone with a copy of the data folder can then read your secrets, so treat backups accordingly.
- When you add `SETTINGS_KEY` later, saved secrets are re-encrypted with it and the generated `settings.key` file is removed.
- **If you lose or change the key**, saved secrets can't be decrypted. Those fields show **re-enter**. Type the values in again and save.

---

## 5. Browsing and activating

The header bar shows catalog, activated and STRM counts. On the Series tab it shows series counts instead. It also has these buttons:

| Button | What it does |
|---|---|
| Scan Plex Library | Marks movies and shows that are already in a Plex library |
| Clear Catalog / Clear STRMs | Wipe the stored catalog or the generated files. Use with care |
| Scan Dead | Disabled for now ("pending redesign") |

### 5.1 Choose what to import (Catalog tab)

Start in **Catalog**.

1. Use the **Providers** panel to pick which provider accounts to include (**All** / **None**).
2. Use the **Categories** panel to pick categories. You can filter the list by language or search it. Right-click a category to hide it from browsing.
3. Click **Sync Selected** to import. Click **Reload Categories** if the category list looks out of date.

Summary tables show counts **by provider** (fallback providers included), **by language**, and **by category**. The category table has a **Remove** mode.

> **Warning:** your provider and category selection also controls the Daily Run. If you un-select a category or provider, the next catalog refresh **removes its movies, including activated ones**. They are removed from Plex and their STRM folders are deleted.

### 5.2 Movies tab

- **Search** by title, and **sort** by rating, year or name (ascending or descending).
- **Filters**: activated or not, decade, genre, language, category, provider (the last four are multi-select). Use **Clear filters** to reset them. Page size can be 60, 100, 200 or 300.
- **Activate or deactivate one movie** with the button on its card.
- **Bulk actions**: click cards to select them. The selection bar then offers **Activate**, **Deactivate** and **Clear**. Language detection runs automatically in the Daily Run, and you can run it for a single movie from its details.

What happens when you activate:

- The bridge opens the stream once, to confirm it really plays.
- **If it works**, the STRM, NFO and poster are written and Plex is asked to refresh. The movie appears in Plex after rclone's directory cache updates (up to about a minute) and Plex finishes scanning.
- **If it fails**, the movie is marked dead right away (one strike) and disappears from the grid. It's moved to **Maintenance > Dead Movies**, where you can restore it.

Activation is the only action, besides actually playing, that connects to the provider. Activate in reasonable batches rather than thousands at once.

### 5.3 Series tab

Series are activated **per episode** and grouped into **series categories**. Each category can have its own Plex library.

**Series categories panel**

1. Click **Add Category**. Enter a name and a STRM folder (a sub-folder under the series output).
2. Pick a Plex library for it:
   - **+ Create new Plex library** makes a TV library pointed at `<series mount root>/<folder>`, or
   - choose an existing library.

   If you set neither, the category uses the **Series library ID (fallback)** from Settings.
3. **Sync Plex Folders** makes sure each category's folder and library are lined up.

The category table shows each category's name, STRM folder, Plex library, number of series and number of active items.

Because each category is its own folder under the series mount, one rclone series mount covers all of them. In Plex, add one library per category, each pointed at its own sub-folder of `/mnt/vod-bridge-series`.

**Browse Series**

- Search, filter by category, and filter by activated.
- **Sync Series Catalog** pulls the latest series list from Dispatcharr.

**Series detail**

1. Click a series to open it.
2. Pick its category, then a season.
3. Click **Fetch Episodes** to load the episode list. A **Trailer** button appears when one is available.
4. Select episodes, then choose **Activate**, **Reactivate** or **Deactivate**.

---

## 6. Maintenance

The **Maintenance** tab has three sub-tabs: **Daily Updates**, **Dead Movies** and **Proxy Logs**.

### 6.1 Daily Updates

- **Schedule**: Off, or **On — 6:00 AM daily** (in the container's time zone, set with `TZ`).
- **Run Now** starts a run immediately.
- **Last Run** shows the latest report. **Run History** keeps the last 10 runs; you can select and delete entries. Click a run to see its full report.
- Failed or timed-out steps are shown **in red**.

What a run does, in order:

| Step | What happens |
|---|---|
| 1. Dump refresh | Asks the host-side dump script for fresh mapping files, then waits up to 3 minutes. If nothing new arrives, the report shows a red **STALE dump** warning and the remaining steps use the older files. |
| 2. Categories | Reloads the category lists. |
| 3. Catalog | Removes movies outside your selected categories and providers (activated ones too). Then it re-checks every stored movie against Dispatcharr. Movies Dispatcharr no longer has are removed completely: from Plex, from disk, and from the database. Dead movies that Dispatcharr still has get a "try restoring" note. New movies in your selected categories are added. |
| 4. Stream mapping | Updates which provider stream each movie uses, including backup streams. Only real changes are counted. |
| 5. Provider info | Fills in missing details from Dispatcharr (up to 500 per run). |
| 5a. TMDB title search | Looks up a TMDB ID for titles that don't have one (up to 2000 per run). Reports found, not found and skipped. Needs TMDB credentials. |
| 5b. TMDB enrichment | Refreshes posters, genres, descriptions and so on. NFOs are rewritten when something changed. The report says how much came from the local TMDB library versus live TMDB calls, and which fields changed. Needs TMDB credentials. |
| 6. Activated stream refresh | Keeps activated movies pointing at their current stream. |
| 7. Plex presence | Marks titles already in Plex (only if Plex is configured). |
| 8. Language detection | Runs in the background, for new movies only. |

A Daily Run **never connects to your provider**, and it doesn't scan for dead streams.

> **About the dump script:** the mapping files (`stream_mapping.json`, `category_mapping.json` and others) are produced by `setup/dump_mappings.sh`. It runs on the **Dispatcharr host** from cron (see INSTALL.md Step 5). The Daily Run asks for a fresh dump by creating a `.dump-trigger` file in the bridge's data folder; a small trigger-check script, run every minute by cron on the Dispatcharr host, sees that file and runs the dump. This only works when the bridge's data folder is on the Dispatcharr host. If you see STALE dump warnings day after day, the trigger check or the dump script isn't running or is failing. Check both cron entries and run the dump script by hand once to see any errors.

### 6.2 Dead Movies

A movie becomes **dead** in two cases:
- it failed activation, or
- its stream returned a real server error during playback (and no backup stream was available).

Dead movies are removed from Plex and stay out until **you** restore them. Nothing restores them automatically.

- Each card shows when the movie died.
- A green note, **"Provider data refreshed — this may have been a temporary issue. Try restoring."**, means Dispatcharr still lists the movie and has fresh data for it. It's worth trying.
- Search, select cards, then **Restore** or **Delete** them.
- **Restore All** and **Delete All** in the header act on the whole list. If one of these appears to do nothing, select the cards and use the per-selection buttons instead.

Provider blocks are usually temporary. Many dead movies play fine after a restore, especially if they died during a burst of testing.

### 6.3 Proxy Logs

- **Activity log**: live streaming and proxy events, with auto-refresh, a level filter and a **Refresh** button.
- **Redirect mode**: each playback logs one line naming the account and stream it went to, for example "Redirecting to Dispatcharr proxy via Account A, stream 1234". The line says so when a fallback account was used because the primary was full, or when all accounts were full. Plex re-requests a title every minute or two while it buffers, and those repeats are not logged. A new line appears only after 15 minutes of quiet, or as "Redirect switched to …" when the stream changes.
- **Archived logs**: older logs are archived automatically every 4 hours (change this with `LOG_ARCHIVE_INTERVAL`), and old archives are cleaned up. Click **Archive Now** to archive immediately, click an archive to view it, and select archives to **Delete** them.

### Other monitoring tabs

- **Streams**: active bridge streams and Plex **Now Playing**, each with auto-refresh.
- **Health**: status cards for the bridge, Dispatcharr and the Plex library (**Check Now** to refresh them). It also shows stream health counts (activated, healthy, striking, dead, never checked) and a health check log. The schedule itself lives in **Maintenance > Daily Updates**, not in Settings.

---

## 7. TMDB Local tab

The TMDB Local tab manages a local copy of TMDB details. It cuts down on live TMDB requests and speeds up the Daily Run. It requires a TMDB credential in Settings.

### Turning it on and off

| Setting | Effect |
|---|---|
| **On** | TMDB details are saved locally and reused |
| **Off** | Nothing is stored. A **Delete stored data** button appears so you can clear what was saved |

### Status and stats

The status card shows:
- whether it's active,
- requests used today against the daily budget,
- the percentage of lookups answered locally,
- the size of the local library,
- when the next fill burst runs.

The stats table counts entries and how many have a release date, poster, cast, overview and rating. It also shows how many changed on TMDB, how many TMDB IDs are known, and when data was last refreshed.

### Actions

| Button | What it does |
|---|---|
| Run fill burst now | Fetches a batch of missing details right away, within the daily budget |
| Check TMDB changes | Asks TMDB which titles changed so they get refreshed |
| Refresh TMDB ID list | Updates the list of known TMDB IDs |
| Match titles without a TMDB ID | Searches TMDB for titles that don't have an ID yet |
| Preview retention cleanup | Shows what cleanup would remove. Click **Apply** to do it |

The retention note explains the limits: data is kept for at most N days, refreshed after M days, and cleaned up daily.

### Lookup

Search by title or TMDB ID, choose Movie or TV, and optionally give a year. The local library is checked first. On a miss, TMDB is searched and the results are saved.

### Catalog refresh settings

These are numeric limits used by the Daily Run's TMDB steps, including the **daily budget**. Click **Save** after changing them.

### Provider-only titles (no TMDB ID)

Some titles will never match TMDB, such as local-language releases or oddly named uploads. You can filter this list:
- **Kind**: Movies or Shows
- **State**: unlocked, locked or all
- **Category**, plus a search box

Select titles (on this page, or **all matching**) and click **Lock to provider data**. Locked titles keep the provider's poster and details. They're skipped by TMDB search, TMDB updates and language lookup. They can still be browsed and activated as usual. Use **Unlock** to reverse it.

---

## 8. Streaming behavior

### Modes

| Mode | How it works | Good for |
|---|---|---|
| **Redirect** | Plex is sent to Dispatcharr's VOD proxy for the actual bytes. The bridge enforces **Max connections per title** by closing the oldest extra sessions, which needs an admin-scoped Dispatcharr API key. | Low bridge load |
| **Proxy** | The bridge keeps one steady connection per movie to Dispatcharr and serves Plex from it, with seeking support. One connection is shared by everyone watching that movie, with a small global limit on simultaneous movies. Download speed adapts to how far ahead of the viewer it is. It's slowed down, never paused. After about 30 seconds with no Plex reads, the connection is closed cleanly and what was already downloaded is kept. | Smooth seeking, fewer provider sessions |

Changing the mode requires a container restart.

### What you might notice

- **Short error video instead of a broken file.** If a movie can't be played (dead, busy, or removed), Plex shows a 10-second video card explaining why. This replaces a generic playback error.
- **Don't rapidly start and stop.** Opening and closing a movie many times in a short window (roughly 6 sessions in a minute) can get it temporarily blocked by the provider. Let a movie start, then seek, instead of restarting it repeatedly.
- **Dead after one real failure.** A real server error from the provider marks the movie dead unless a backup stream is mapped. In that case the bridge switches to the backup automatically. A brief stall in the data alone does **not** mark anything dead.
- **Circuit breaker.** If the provider side fails repeatedly, the bridge stops trying new connections. This only resets when the container restarts.
- **First play can take a few seconds** while the connection opens and Plex reads the file's header.

---

## 9. Troubleshooting FAQ

**My Plex library is empty.**
1. On the Plex host, run `ls /mnt/vod-bridge`. If it's empty or errors, rclone isn't mounted. Check `systemctl status rclone-vodbridge`.
2. Open `http://<bridge-host>:8585/vod/` in a browser. It should list your activated movies. If it does but the mount is empty, check the URL in `rclone.conf`.
3. Make sure the Plex library points at the mount path, and that path matches **Plex movie mount root** in Settings.
4. Wait about a minute (rclone's directory cache), then scan the library in Plex.

**Activated movies vanished from the Movies grid.**
They failed activation and were marked dead. Look in **Maintenance > Dead Movies**.

**Activated movies disappeared from Plex after the morning run.**
Check that day's Daily Run report. The usual causes:
- the movie's category or provider was un-selected in Catalog,
- Dispatcharr no longer lists the movie, or
- you recently switched the primary Dispatcharr.

**The Daily Run shows a red STALE dump warning.**
The host dump script didn't produce new mapping files within 3 minutes. Check that the trigger-check cron job (INSTALL.md Step 5) is installed on the Dispatcharr host, that the bridge's data folder is on that host, and run `setup/dump_mappings.sh` by hand to see any errors.

**Settings test says "Dispatcharr refused the API key".**
Create a new key in Dispatcharr (**Settings > API Keys**), paste it into the instance, and save. For redirect mode, the key needs admin rights.

**A secret field says "re-enter".**
The encryption key changed or was lost (for example, `SETTINGS_KEY` was changed or the data folder was restored without `settings.key`). Type the value in again and save.

**I changed the stream mode but nothing changed.**
The mode applies only after a container restart. The page shows "restart pending" until then.

**I changed `PLEX_TOKEN` (or a similar setting) in my `.env` file and nothing happened.**
A value saved in Settings overrides the environment. Clear the saved value in Settings, or update it there. For Dispatcharr, the environment is only read on the very first start; edit the instance in Settings instead.

**Series episodes don't show in Plex.**
- Check the series rclone mount (`/mnt/vod-bridge-series`).
- Check that the category's Plex library points at `<series mount root>/<category folder>`.
- Use the Plex **Test** in Settings: it flags categories with a missing or invalid library.
- **Sync Plex Folders** on the Series tab can fix a folder that doesn't match its library.

**A movie stops after a few seconds of playback with an error card.**
The card says why: dead, busy (too many sessions), or removed. If it says busy, wait a bit and try again. If it says dead, restore it later from Dead Movies.

**Playback works, but Plex is slowly reading entire files.**
Turn off video preview thumbnails, intro markers and credits markers for the bridge libraries in Plex.

**I forgot my password.**
There's no password reset in the UI. Back up the `/data` folder before you try anything, then ask in the project's issue tracker for the recovery steps.

---

## 10. Known limitations and coming soon

**Current limitations**

- **rclone must be set up by hand** on the Plex host (section 2).
- **One active Dispatcharr at a time.** Standby instances are stored but unused until made primary, and switching between different Dispatcharr servers removes movies the new one doesn't have.
- **Dead is manual-restore only.** Nothing brings a dead movie back automatically. The "try restoring" note is a hint, not a retry.
- **Scan Dead is disabled** while it's redesigned.
- **There's no per-provider-account connection limit.** Only the global and per-title limits exist.
- **Changing the stream mode needs a restart.**
- **Single account.** No multiple users or roles.
- **Jellyfin** isn't officially supported yet. The same rclone approach may work, but it isn't covered here.
- Dead Movies **Restore All / Delete All** have been unreliable in the past. The per-selection buttons are the dependable route.

**Coming soon / planned**

- **Built-in rclone** so Plex can see bridge content without manual mount setup.
- **Provider (XC) accounts in Settings.** The section is visible now but locked as "Future — pending".
- **Per-provider-account concurrency limits.**
- A redesigned **dead-stream scan**.
- Automatic Plex library scan right after activation.
