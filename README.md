# Switch Game Catalog

A local Windows desktop catalog for personal Nintendo Switch game files. It scans user-selected `.nsp`, `.nsz`, and `.xci` folders, stores records in SQLite, matches update files from a separate updates folder, and displays the collection in a two-pane library interface.

<img width="2560" height="1390" alt="image" src="https://github.com/user-attachments/assets/8d5eeac6-0ef0-4e85-aa37-c6a1e8628962" />
<img width="2560" height="1390" alt="image" src="https://github.com/user-attachments/assets/0bc17ebb-97d3-4332-b305-c76c36747506" />

## Features

- Recursive base-game scan for `.nsp`, `.nsz`, and `.xci`
- Separate recursive updates-folder scan
- Fuzzy update-to-game matching
- SQLite catalog at `~/.switch_library_catalog/library.sqlite3`
- Settings stored at `~/.switch_library_catalog/settings.json`
- Cover grid view with adjustable art size and double-click navigation back to the library/details view
- Favorites with a heart in the list and a highlighted frame in grid view
- Right-click option to move a mistaken game entry into Unmatched Updates for DLC/update matching
- Larger screenshot browser with Previous/Next controls
- Unmatched updates view
- Optional IGDB metadata lookup for real cover art
- Manual metadata rematching from the game list right-click menu
- Install button that moves the base game first, then selected updates/DLC, into a configured install folder
- Right-click install for selected update/DLC files when the base game is already installed
- Details view compares local update versions against the cached titledb `versions.json` latest release data
- Titledb version lists refresh automatically when the cached files are older than 24 hours
- Right-click deletion for duplicate game files and old update/DLC files
- Installable themes: ships with **Dracula** and **OLED Dark**, plus support for your own `.qss` themes
- Built-in server that installs your catalog to a Switch over Wi-Fi via DBI (HTTP directory listing)
- Custom **groups** (collections) you create and assign games to — also browsable as folders in DBI

## Themes

The app ships with two built-in themes — **Dracula** (default) and **OLED Dark** (true black). You can also install your own.

A theme is a standard Qt stylesheet (`.qss`) file. To install one:

- Open **Settings → Theme**, click **Install Theme…**, and pick a `.qss` file, **or**
- Click **Open Themes Folder** (or browse to `%LOCALAPPDATA%\Switch Game Catalog\themes`) and drop `.qss` files in there.

Installed themes appear in the **Theme** dropdown by file name. Pick one and click **Save** to apply it immediately — your choice is remembered in `settings.json` across restarts.

## Groups

You can organize games into your own **groups** (collections). In the Library, right-click a game and choose **Add to group ▸ New group…** (or an existing group). Use the **group dropdown** at the top of the Library to filter the list to one group; right-click also offers **Remove from / Delete group** when a group is selected.

Groups are also exposed by the wireless server: each group becomes a folder in DBI (see below), so you can, say, make a "To Install" group on the PC and install exactly those over Wi-Fi. Group membership is stored by game title, so it survives a rescan.

## Wireless install server (DBI)

The app can run a small built-in HTTP server that exposes your catalog as an HTTP **directory listing**, so a homebrew installer can install games to a Switch over Wi-Fi. (The Switch's built-in browser can't download files, which is why installing uses a homebrew installer rather than a plain web page.)

Enable it in **Settings → Wireless download server**:

- **Enable** — turn the server on/off (applies as soon as you click Save).
- **Network access** — checked: reachable from other devices on your network (binds `0.0.0.0`). Unchecked: this PC only (`127.0.0.1`), useful for testing.
- **Port** — defaults to `8000`.
- **Username / Password** — when a password is set, access is protected with HTTP Basic Auth. **Leave the password blank to run the server open** (no auth) — simplest, and fine on a trusted network.

The Settings dialog shows the exact URL to use.

### DBI

[DBI](https://github.com/rashevskyv/dbi) is the recommended installer. In DBI's network-install screen, enter the server's directory URL (the Settings dialog shows it):

```
http://192.168.1.20:8000/dir/
```

(keep the trailing slash; use your PC's IP and port). If you set a password, embed it: `http://user:password@192.168.1.20:8000/dir/`. DBI shows a folder for each of your **groups** plus an **All Games** folder; open one to see its games (base files + their updates) and select what to install.

Tips:
- Launch DBI in **full-RAM/application mode** (hold `R` while opening an installed game, then start DBI from the homebrew menu). Heavy installers crash if launched from the Album (applet mode).
- Use a DBI build that matches your **firmware** — old builds crash on newer firmware.

### Other endpoints

- **`/dir/`** — Apache-style HTML directory listing (what DBI reads).
- **`/list.txt`** — a plain list of direct file URLs, one per line, for **download managers** (`wget -i`, JDownloader) or pasting a single URL into another installer. If a password is set the URLs embed it (`http://user:pass@host/...`).
- **`/dl/<game|update>/<id>`** — direct download of one file by catalog id.

Only files already in your catalog are exposed (by id or by a name that exists in the catalog) — the server never serves arbitrary paths from disk, and downloads support resuming (HTTP range requests).

### Reaching it from any network (not just home Wi-Fi)

Basic Auth over plain HTTP is fine on a network you trust, but the credentials are not encrypted, so **do not port-forward this straight to the public internet.** To reach your catalog securely from anywhere, put the PC and your phone/laptop on your own private network with [Tailscale](https://tailscale.com) (or WireGuard): install it on both devices, then open the server at the PC's Tailscale IP. The connection is encrypted, no ports are exposed publicly, and the password still applies.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py

**or just run the exe**
```

Open **Settings**, choose a base games folder and updates folder, then run **Rescan**.

IGDB metadata requires a Twitch/IGDB client ID and client secret. Without API credentials, scanning and cataloging still work.

## Project Structure

```text
main.py
switch_catalog/
  app.py
  db.py
  filename.py
  metadata.py
  paths.py
  scanner.py
  server.py
  settings.py
  theme.py
  ui.py
```
