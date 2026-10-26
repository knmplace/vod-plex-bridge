# VOD Plex Bridge — User Guide

This guide covers VOD Plex Bridge **v0.40.0**. It explains what the app does, how to set it up, and how to use each tab. For a first-time install (Docker compose, then the Setup tab's commands), see [INSTALL.md](../INSTALL.md). Come back here once the container is running.

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
   - Movie categories: `http://<bridge-host>:8585/vod-movies/` (one sub-folder per movie category, see section 5.1)
   - Series: `http://<bridge-host>:8585/vod-series/`
4. **rclone**, running on the Plex host, mounts those listings as folders (for example `/mnt/vod-bridge`). It uses rclone's read-only HTTP backend.
   Movie files are named `Title (Year) {tmdb-<id>} [<bridge id>].mp4` and show folders `Show {tmdb-<id>}`. The `{tmdb-…}` part tells Plex the exact title to match; it's left out for titles without a TMDB ID.
5. **Plex** scans those folders like any other movie or TV library. When someone presses play, Plex reads the file through rclone. The bridge then streams the video from Dispatcharr.

No network share (NAS, SMB/CIFS, NFS) is involved. The only link between the bridge and Plex is HTTP.

---

## 2. The Setup tab

Plex and Jellyfin can only read folders on their own machine. So two small things run outside the bridge: an **agent on the Dispatcharr machine** that sends the bridge Dispatcharr's movie and series lists, and an **rclone mount on each Plex/Jellyfin machine** that shows the bridge's titles as folders. The **Setup** tab gives you one command for each machine and confirms that it worked. [INSTALL.md](../INSTALL.md) walks through it in full; this is the short version.

The Setup tab opens by itself on first sign-in until every step is done. Each step has a light that turns green when the bridge confirms it.

![The Setup tab](images/setup-overview.png)

| Step | What you do | Green when |
|---|---|---|
| **1. Bridge address** | Check the address other machines use to reach the bridge (filled in from `BRIDGE_HOST`), then **Save**. `localhost`, `127.*` and `0.0.0.0` are refused. | The address is saved |
| **2. Dispatcharr host** | Copy **Command A** and paste it on the machine where Dispatcharr runs. | The agent has reported and the first lists have arrived |
| **3. Media server machines** | One card per Plex/Jellyfin machine. Fill it in, **Save**, copy **Command B** and paste it on that machine. | The machine reported that its mounts are readable |
| **4. Libraries** | Press **Create Plex movie library**. Jellyfin libraries are added in Jellyfin. | A movie library ID is saved |
| **5. Done** | A summary, plus a reminder of what to do day to day. | — |

> **Tick Rclone Manager before you run Command B.** If you want the Rclone Manager web page for a machine, tick **"Also install Rclone Manager"** on its card and press **Save** *before* copying Command B. Command B installs it and reports it back, and that report is what adds the machine to **Maintenance → Rclone**. If you tick it afterwards, save and run Command B again on that machine.

### What the commands set up

- **Command A** installs `/opt/vod-plex-bridge/` and `/etc/cron.d/vod-plex-bridge` on the Dispatcharr machine. Every minute it asks the bridge whether fresh lists are wanted (the Daily Run and **Run Now** ask for them). Every 12 hours it sends them anyway.
- **Command B** installs rclone and FUSE, adds the `vodbridge-movies`, `vodbridge-movies-cat` and `vodbridge-series` remotes to `rclone.conf`, and starts `rclone-vod-bridge-movies.service`, `rclone-vod-bridge-movies-cat.service` and `rclone-vod-bridge-series.service`. They mount `/mnt/vod-bridge`, `/mnt/vod-bridge-movies` and `/mnt/vod-bridge-series` by default. The movie-categories mount stays empty until you add a movie category.

Both commands are safe to run again. A second run updates what is there and leaves working parts alone. To read a script before running it, download it with `curl -o` instead of piping it to `bash`.

The commands contain a private setup link (`/setup/<token>/...`). If it leaks, use **make a new link** at the bottom of the Setup tab. Old commands stop working after that, so run Command A again (and Command B, to update a machine later).

### Libraries

- **Plex:** **Create Plex movie library** makes a Movies library at the movie folder from your machine's card and saves its ID in Settings. This is the **Default** movie library. Each movie category you add gets its own library (section 5.1), and so does each series category (section 5.3).
- **Jellyfin:** **Dashboard → Libraries → Add Media Library**. Use **Movies** on `/mnt/vod-bridge` and **Shows** on `/mnt/vod-bridge-series`. Untick trickplay and chapter image extraction, tick the **Nfo** metadata reader, and set **Scheduled Tasks → Scan Media Library** to every hour. Jellyfin's real-time monitoring doesn't notice changes on an rclone mount.
- **On both:** turn off video preview thumbnails, intro and credits markers, and chapter thumbnails. Otherwise the server reads every movie in full through your provider.

The folders you use must match **Plex movie mount root**, **Plex movie category mount root** and **Plex series mount root** in Settings (defaults `/mnt/vod-bridge`, `/mnt/vod-bridge-movies` and `/mnt/vod-bridge-series`).

If Plex or Jellyfin runs in Docker, the mount is made on the host. Add the volume lines the machine card shows (`/mnt/vod-bridge:/mnt/vod-bridge:ro,rslave`), and recreate the container *after* the mount is up. For Unraid, Windows, macOS and NAS boxes, the card shows filled-in manual steps instead of Command B. These are marked untested.

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

> **Every section and panel in the app can be collapsed.** Click its header to open or close it. Panels start closed, and your browser remembers which ones you opened.

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
| Movie library ID | The numeric section ID of your movie library. **Setup → 4. Libraries** creates the library and fills this in. If you made the library by hand, the ID appears in the Plex Web URL: `.../library/sections/<id>/...` |
| Series library ID (fallback) | Used only for series categories that don't have their own library (see section 5) |
| Plex movie mount root | Where rclone mounts movies on the Plex host (default `/mnt/vod-bridge`) |
| Plex movie category mount root | Where rclone mounts movie categories on the Plex host (default `/mnt/vod-bridge-movies`). Each category's library points at a sub-folder here |
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
| Stream mode | **Redirect** or **Proxy**. Takes effect only after a container restart. Until then, the page shows a "restart pending" note. **Use redirect with caution:** Plex/Jellyfin connect to Dispatcharr directly for every scan and playback, and a VPN on the Plex/Jellyfin machine is recommended |
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

### 4.7 Media servers (Jellyfin and Emby)

Optional. Fill these in to see Jellyfin and Emby playback next to Plex.

| Setting | Notes |
|---|---|
| Jellyfin URL / API key | e.g. `http://<jellyfin-host>:8096`. Make the key in Jellyfin under **Dashboard → API Keys → +** |
| Emby URL / API key | e.g. `http://<emby-host>:8096`. Make the key in Emby under **Settings → Advanced → API Keys → New API Key** |

Once a server is set up, **Streams → Now Playing** lists its sessions, tagged with the server name, and the **Health** tab shows a status card for it. **Test** checks the address and key. The bridge only reads sessions from these servers. It does not create Jellyfin or Emby libraries.

### 4.8 Display

**Display size** scales the whole app. **Auto**, the default, scales it up on windows wider than 1920 pixels, up to 200%. Phones and normal screens stay at 100%. You can pick a fixed size from 80% to 200% instead. The choice is saved per browser.

---

## 5. Browsing and activating

> [!IMPORTANT]
> **Activate a few titles at a time. This app is not built for hoarding a large library.**
> It works best for picking what you want to watch next: about **2–3 movies**, or **one
> season of a series (10–15 episodes)**, at a time. Let Plex or Jellyfin finish scanning them
> before you activate more.
>
> **Every activation, and every Plex or Jellyfin library scan of a new title, uses an open
> active connection slot in Dispatcharr — the same slot a viewer uses to watch.**
>
> - **Activating a movie** reads its start and end once through Dispatcharr to check that it
>   plays.
> - **Plex reads the head and tail of every new file** when it scans it. For movies the bridge
>   answers those reads from what it saved at activation, so most never reach Dispatcharr.
>   Episodes have no saved copy, so **every new episode Plex scans opens a Dispatcharr
>   connection**.
> - **Jellyfin does the same.** Its library scan probes each new file (ffprobe), which reads
>   the start of the file and often the end.
> - **Redirect mode: use with caution.** It sends Plex and Jellyfin straight to Dispatcharr's
>   proxy, so every playback opens its own Dispatcharr connection and the bridge's shared
>   pipe doesn't apply. Plex's scan reads of a movie's start and end are still answered from
>   the copy saved at activation (since 0.41.2); anything else goes to Dispatcharr. **Running a VPN on the
>   Plex/Jellyfin machine is recommended** in this mode.
>
> Activate 50 episodes at once and the scan can fill every slot your provider allows, block
> live viewing, and look like rapid reconnects to the provider, which can get titles
> temporarily blocked or marked dead.

The header bar shows catalog, activated and STRM counts. On the Series tab it shows series counts instead. It also has these buttons:

| Button | What it does |
|---|---|
| Scan Plex Library | Marks movies and shows that are already in a Plex library |
| Clear Catalog / Clear STRMs | Wipe the stored catalog or the generated files. Shown on the Catalog tab only. Use with care |
| Scan Dead | Disabled for now ("pending redesign") |

### 5.1 Choose what to import (Catalog tab)

Start in **Catalog**.

![Catalog tab](images/catalog.png)

1. Use the **Providers** panel to pick which provider accounts to include (**All** / **None**).
2. Use the **Categories** panel to pick categories. You can filter the list by language or search it. Right-click a category to hide it from browsing.
3. Click **Sync Selected** to import. Click **Reload Categories** if the category list looks out of date.

Summary tables show counts **by provider** (fallback providers included), **by language**, and **by category**. The category table has a **Remove** mode.

> **Warning:** your provider and category selection also controls the Daily Run. If you un-select a category or provider, the next catalog refresh **removes its movies, including activated ones**. They are removed from Plex and their STRM folders are deleted.

### 5.2 Movies tab

![Movies tab](images/movies.png)

- **Search** by title, and **sort** by rating, year or name (ascending or descending).
- **Filters**: activated or not, decade, genre, language, category, provider (the last four are multi-select). Use **Clear filters** to reset them. Page size can be 60, 100, 200 or 300.
- **Activate or deactivate one movie** with the button on its card. **Activate** opens a small window where you pick its Plex library: **Auto category**, **Default library**, any movie category, or **+ New category…** (enter a name, an optional folder and a Plex library, and the bridge creates the category and its library, then activates the movie into it). **Remove** deactivates straight away. The card updates in place; the page doesn't reload.
- **Activity indicator**: while an activation, move or deactivation runs (single or bulk, movies or episodes), a spinning pill next to **Scan Plex Library** says what's happening. It turns into a green ✓ or red ✕ result for a few seconds when it's done.
- **Bulk actions**: click cards to select them. The selection bar then offers a category picker, **Activate**, **Move**, **Deactivate** and **Clear**. Language detection runs automatically in the Daily Run, and you can run it for a single movie from its details.
- **Categories**: the picker chooses where activated movies go. **Auto category** (the default, and what the card's own button uses) picks the movie category whose provider categories include the movie, or the Default library if none do. **Default library** or a named category sends them there. **Move** sends already-active movies to the picked category: they leave their old Plex library and the new one is scanned. A movie in a category shows the category name on its card.

What happens when you activate:

- The bridge opens the stream once, to confirm it really plays.
- **If it works**, the STRM, NFO and poster are written and Plex is asked to refresh. The movie appears in Plex after rclone's directory cache updates (up to about a minute) and Plex finishes scanning.
- **If it fails**, the movie is marked dead right away (one strike) and disappears from the grid. It's moved to **Maintenance > Dead Movies**, where you can restore it.

Activation is the only action, besides actually playing, that connects to the provider. Activate in reasonable batches rather than thousands at once.

**Movie Categories panel** (top of the Movies tab, like **Series Categories** on the Series tab)

Movie categories work like series categories: each one is its own folder and its own Plex library, so you can keep, for example, kids' movies or 4K movies apart from the rest.

1. Enter a **name**. The **folder** defaults to the name.
2. Pick a Plex library: **+ Create new Plex library** makes a Movies library pointed at `<movie category mount root>/<folder>`, or choose an existing library, or **No Plex library**.
3. Optionally pick one or more **provider categories** (Ctrl/Cmd-click for several). Movies from those provider categories go to this category when you activate them with **Auto category**.
4. Click **Add Category**. **Sync Plex Folders** re-adds every category's folder to its library if something got out of line.

The table shows every category with its folder, Plex library, number of active movies and provider categories. The **Default** row is the original movie library at `/vod/` (Settings → Movie library ID), and it gets every movie that isn't in a category. A movie is only ever in one place, so it never shows up twice in Plex.

**Click a row** (name, folder or Active count, including **Default**) to show only the movies active in that library, the same way series categories work. A banner above the grid names the library; click the row again or **Back to catalog** to return to the full catalog. Removing a movie in this view takes its card off the grid.

A category can't be deleted while it has active movies. Move or deactivate them first. Deleting a category also removes its folder from its Plex library.

> **Upgrading?** Movies that were active before you added categories stay in the Default library. Use **Move** (section 5.2) to put them in a category. The new category folder also needs the third rclone mount: run **Command B** again on each Plex/Jellyfin machine.

### 5.3 Series tab

![Series tab](images/series.png)

Series are activated **per episode** and grouped into **series categories**. Each category can have its own Plex library.

**Series categories panel**

1. Click **Add Category**. Enter a name and a STRM folder (a sub-folder under the series output).
2. Pick a Plex library for it:
   - **+ Create new Plex library** makes a TV library pointed at `<series mount root>/<folder>`, or
   - choose an existing library.

   If you set neither, the category uses the **Series library ID (fallback)** from Settings.
3. **Sync Plex Folders** makes sure each category's folder and library are lined up.

The category table shows each category's name, STRM folder, Plex library, **Active Shows** (shows with at least one active episode) and **Active Episodes**. Click a row to see the active shows in that library.

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

The **Maintenance** tab has the sub-tabs **Daily Updates**, **Dead Movies** and **Proxy Logs**, plus **Rclone** once a media machine runs Rclone Manager.

### 6.1 Daily Updates

- **Schedule**: Off, or **On — 6:00 AM daily** (in the container's time zone, set with `TZ`).
- **Run Now** starts a run immediately.
- **Last Run** shows the latest report. **Run History** keeps the last 10 runs; you can select and delete entries. Click a run to see its full report.
- Failed or timed-out steps are shown **in red**.

What a run does, in order:

| Step | What happens |
|---|---|
| 1. Dump refresh | Asks the Dispatcharr-host agent (Command A) for fresh mapping files, then waits up to 3 minutes. If nothing new arrives, the report shows a red **STALE dump** warning and the remaining steps use the older files. |
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

> **About the mapping files:** `stream_mapping.json`, `category_mapping.json` and the others come from Dispatcharr's database, so they are made on the **Dispatcharr host** by the agent that Command A installs. The Daily Run and **Run Now** flag that fresh files are wanted; the agent checks every minute, makes them, and uploads them to the bridge. It also sends them every 12 hours on its own. If you see STALE dump warnings, check that `/etc/cron.d/vod-plex-bridge` exists on the Dispatcharr host and look at the Dispatcharr light on the Setup tab. Running Command A again fixes most problems. Older setups that still use the `.dump-trigger` file and their own cron lines keep working.

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

### 6.4 Rclone

This sub-tab appears once a media machine reports that Rclone Manager is running. That happens when **Also install Rclone Manager** was ticked on the machine's Setup card *before* Command B ran (see section 2). Each machine gets a card with an **Open Rclone Manager** button. The page opens in a new tab, where you can check and restart the bridge mounts. Sign in as **admin**; the default password is `ChangeMeNow!` unless you set one while Command B ran, so change it right away. The Health tab shows the same link.

![Rclone sub-tab](images/rclone.png)

### Other monitoring tabs

- **Streams**: active bridge streams and **Now Playing**, each with auto-refresh. Now Playing covers Plex plus Jellyfin and Emby when they are set up (section 4.7), with each session tagged by server.
- **Health**: status cards for the bridge, Dispatcharr, the Plex library, and Jellyfin/Emby when set up (**Check Now** to refresh them). It also shows stream health counts (activated, healthy, striking, dead, never checked) and a health check log. The schedule itself lives in **Maintenance > Daily Updates**, not in Settings.

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
1. On the Plex host, run `ls /mnt/vod-bridge`. If it's empty or errors, rclone isn't mounted. Check `systemctl status rclone-vod-bridge-movies` (or Rclone Manager), and look at the machine's light on the Setup tab. Running Command B again fixes most mount problems. In a Proxmox LXC, the container needs FUSE turned on (`pct set <id> --features fuse=1` on the Proxmox host).
2. Open `http://<bridge-host>:8585/vod/` in a browser. It should list your activated movies. If it does but the mount is empty, check the URL in `rclone.conf`.
3. Make sure the Plex library points at the mount path, and that path matches **Plex movie mount root** in Settings.
4. Wait about a minute (rclone's directory cache), then scan the library in Plex.

**Plex matched a movie or show to the wrong title.**
Titles activated since 0.41.2 carry their TMDB ID in the name Plex sees, so this is mostly older titles that Plex matched before. Plex keeps an existing match. Use **Fix Match** on the item and search `{tmdb-<id>}`, with the ID from the bridge's movie card, or remove the item and scan again. If the title has no TMDB ID, Plex can only guess from the name and year.

**Activated movies vanished from the Movies grid.**
They failed activation and were marked dead. Look in **Maintenance > Dead Movies**.

**Activated movies disappeared from Plex after the morning run.**
Check that day's Daily Run report. The usual causes:
- the movie's category or provider was un-selected in Catalog,
- Dispatcharr no longer lists the movie, or
- you recently switched the primary Dispatcharr.

**The Daily Run shows a red STALE dump warning.**
The Dispatcharr-host agent didn't upload new mapping files within 3 minutes. Check the Dispatcharr light on the Setup tab and that `/etc/cron.d/vod-plex-bridge` exists on the Dispatcharr host. Run Command A again; it runs a dump straight away and prints any error. If you made a new setup link, the old agent stops working until Command A is run again.

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
Turn off video preview thumbnails, intro markers and credits markers for the bridge libraries in Plex. In Jellyfin, untick trickplay and chapter image extraction.

**Jellyfin doesn't pick up new titles.**
Jellyfin's real-time monitoring doesn't notice changes on an rclone mount. Set **Dashboard → Scheduled Tasks → Scan Media Library** to every hour, or press **Scan All Libraries**.

**The Rclone sub-tab is missing.**
It only appears after a machine reports Rclone Manager. Tick **Also install Rclone Manager** on that machine's Setup card, press **Save**, and run Command B again on that machine.

**I forgot my password.**
There's no password reset in the UI. Back up the `/data` folder before you try anything, then ask in the project's issue tracker for the recovery steps.

---

## 10. Known limitations and coming soon

**Current limitations**

- **Only Linux / Proxmox LXC media machines are tested.** Unraid, Windows, macOS and NAS get filled-in manual steps marked untested.
- **One active Dispatcharr at a time.** Standby instances are stored but unused until made primary, and switching between different Dispatcharr servers removes movies the new one doesn't have.
- **Dead is manual-restore only.** Nothing brings a dead movie back automatically. The "try restoring" note is a hint, not a retry.
- **Scan Dead is disabled** while it's redesigned.
- **There's no per-provider-account connection limit.** Only the global and per-title limits exist.
- **Changing the stream mode needs a restart.**
- **Single account.** No multiple users or roles.
- **Jellyfin libraries are made by hand** in Jellyfin (section 2). The bridge creates Plex libraries only, including the ones for movie and series categories. In Jellyfin, add a Movies library per movie category pointed at its sub-folder of `/mnt/vod-bridge-movies`.
- Dead Movies **Restore All / Delete All** have been unreliable in the past. The per-selection buttons are the dependable route.

**Coming soon / planned**

- **Provider (XC) accounts in Settings.** The section is visible now but locked as "Future — pending".
- **Per-provider-account concurrency limits.**
- A redesigned **dead-stream scan**.
- Automatic Plex library scan right after activation.
