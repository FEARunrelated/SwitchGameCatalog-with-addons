"""Embedded HTTP server that lets other devices on the network browse and
download the catalog's game files over Wi-Fi.

Design notes / safety:
- Files are exposed only by their database id (``/dl/game/<id>`` and
  ``/dl/update/<id>``). The on-disk path is looked up from the catalog and the
  file is served only if it actually exists, so there is no way to request an
  arbitrary path on disk (no directory traversal, no filesystem listing).
- Access requires a password. Browsers (including the limited Nintendo Switch
  browser, which can't show the HTTP Basic Auth popup) get a normal login page
  whose form sets a signed session cookie; a Basic auth header is also accepted
  for tools / "user:pass@host" URLs. Secrets are compared in constant time.
- The server runs in a background daemon thread so the Qt UI stays responsive.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import threading
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, quote, urlparse

from .db import connect
from .paths import DB_PATH
from .theme import web_palette

_CHUNK = 256 * 1024
# Optional trailing "/<filename>" lets installers like Tinfoil read the title id
# from the URL; it is ignored for lookup (routing is by id).
_DL_PATTERN = re.compile(r"^/dl/(game|update)/(\d+)(?:/.*)?$")
_TABLE = {"game": "game_files", "update": "updates"}


def get_lan_ip() -> str:
    """Best-effort primary LAN IP of this machine (no traffic is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


class _Handler(BaseHTTPRequestHandler):
    server_version = "SwitchGameCatalog"
    protocol_version = "HTTP/1.1"

    # -- lifecycle helpers -------------------------------------------------
    def log_message(self, *args) -> None:  # silence stderr access logging
        pass

    @property
    def _config(self) -> dict:
        return self.server.catalog_config  # type: ignore[attr-defined]

    def _db(self) -> sqlite3.Connection:
        return connect(self._config["db_path"])

    # -- auth --------------------------------------------------------------
    def _session_value(self) -> str:
        return hmac.new(
            self._config["password"].encode("utf-8"), b"sgc-session", hashlib.sha256
        ).hexdigest()

    def _authorized(self) -> bool:
        cfg = self._config
        basic = "Basic " + base64.b64encode(
            f"{cfg['username']}:{cfg['password']}".encode("utf-8")
        ).decode("ascii")
        if hmac.compare_digest(self.headers.get("Authorization", ""), basic):
            return True
        token = self._cookie("sgc")
        return bool(token) and hmac.compare_digest(token, self._session_value())

    def _cookie(self, name: str) -> str:
        header = self.headers.get("Cookie", "")
        if not header:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(header)
        except Exception:
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    # -- entry points ------------------------------------------------------
    def do_GET(self) -> None:
        self._handle(include_body=True)

    def do_HEAD(self) -> None:
        self._handle(include_body=False)

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/login":
            self._handle_login()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle(self, *, include_body: bool) -> None:
        # The Nintendo Switch browser (and other limited browsers) can't show the
        # Basic Auth popup, so unauthenticated requests get a normal login page that
        # sets a session cookie. A Basic auth header is still honored above.
        if not self._authorized():
            self._serve_login(include_body=include_body)
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index(include_body=include_body)
            return
        if path in ("/tinfoil", "/tinfoil.json", "/index.json"):
            self._serve_tinfoil(include_body=include_body)
            return
        match = _DL_PATTERN.match(path)
        if match:
            self._serve_file(match.group(1), int(match.group(2)), include_body=include_body)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _base_url(self) -> str:
        host = self.headers.get("Host") or f"{get_lan_ip()}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _serve_tinfoil(self, *, include_body: bool) -> None:
        """JSON index understood by Tinfoil/DBI network sources.

        Each file URL carries the real filename so the installer can read the
        title id/version; the installer applies the host's Basic auth to them.
        """
        base = self._base_url()
        conn = self._db()
        try:
            files = [
                {"url": f"{base}/dl/game/{row['id']}/{quote(row['file_name'])}", "size": row["file_size"]}
                for row in conn.execute("SELECT id, file_name, file_size FROM game_files")
            ]
            files += [
                {"url": f"{base}/dl/update/{row['id']}/{quote(row['file_name'])}", "size": row["file_size"]}
                for row in conn.execute("SELECT id, file_name, file_size FROM updates")
            ]
        finally:
            conn.close()
        payload = {
            "files": files,
            "directories": [],
            "success": f"Switch Game Catalog — {len(files)} file(s)",
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self._write(data)

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        password = (parse_qs(data).get("password") or [""])[0]
        if hmac.compare_digest(password, self._config["password"]):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"sgc={self._session_value()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._serve_login(error=True)

    def _serve_login(self, *, include_body: bool = True, error: bool = False) -> None:
        palette = web_palette(self._config.get("theme", "Dracula"))
        note = '<p class="err">Wrong password &mdash; try again.</p>' if error else ""
        data = Template(_LOGIN_PAGE).safe_substitute(error=note, **palette).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self._write(data)

    # -- catalog listing ---------------------------------------------------
    def _serve_index(self, *, include_body: bool) -> None:
        try:
            body = self._render_index().encode("utf-8")
        except sqlite3.Error as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Catalog error: {exc}")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self._write(body)

    def _render_index(self) -> str:
        conn = self._db()
        try:
            games = conn.execute(
                "SELECT id, display_title, cover_image_url, favorite "
                "FROM games ORDER BY favorite DESC, display_title COLLATE NOCASE"
            ).fetchall()
            files: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(
                "SELECT id, game_id, file_name, file_size, is_base_game FROM game_files"
            ):
                files.setdefault(row["game_id"], []).append(row)
            updates: dict[int | None, list[sqlite3.Row]] = {}
            for row in conn.execute(
                "SELECT id, game_id, file_name, file_size, detected_version FROM updates"
            ):
                updates.setdefault(row["game_id"], []).append(row)
        finally:
            conn.close()

        cards: list[str] = []
        total = 0
        for game in games:
            entries: list[str] = []
            for row in sorted(files.get(game["id"], []), key=lambda r: (not r["is_base_game"], r["file_name"].lower())):
                entries.append(self._entry("game", row["id"], row["file_name"], row["file_size"]))
            for row in sorted(updates.get(game["id"], []), key=lambda r: r["file_name"].lower()):
                label = row["file_name"]
                if row["detected_version"]:
                    label += f"  (v{row['detected_version']})"
                entries.append(self._entry("update", row["id"], label, row["file_size"]))
            if not entries:
                continue
            total += len(entries)
            cards.append(
                self._card(
                    title=game["display_title"],
                    cover=self._cover_url(game["cover_image_url"]),
                    favorite=bool(game["favorite"]),
                    entries=entries,
                )
            )

        orphans = updates.get(None, [])
        if orphans:
            entries = [
                self._entry("update", row["id"], row["file_name"], row["file_size"])
                for row in sorted(orphans, key=lambda r: r["file_name"].lower())
            ]
            total += len(entries)
            cards.append(self._card(title="Unmatched updates", cover="", favorite=False, entries=entries))

        grid = (
            f'<div class="grid">{"".join(cards)}</div>'
            if cards
            else '<p class="empty">No games in the catalog yet. Run a scan in the app.</p>'
        )
        footer = (
            '<p class="foot">Installing to a Switch? Add this as a Tinfoil / DBI network source: '
            f"<code>{escape(self._base_url())}/tinfoil</code> (use the same username &amp; password).</p>"
        )
        palette = web_palette(self._config.get("theme", "Dracula"))
        return Template(_PAGE).safe_substitute(count=total, body=grid + footer, **palette)

    @staticmethod
    def _card(*, title: str, cover: str, favorite: bool, entries: list[str]) -> str:
        if cover:
            art = f'<img class="art" loading="lazy" src="{escape(cover, quote=True)}" alt="">'
        else:
            art = f'<div class="art placeholder">{escape(title[:1].upper()) or "?"}</div>'
        heart = "♥ " if favorite else ""
        cls = "card favorite" if favorite else "card"
        return (
            f'<details class="{cls}" data-name="{escape(title.lower(), quote=True)}">'
            f"<summary>{art}"
            f'<div class="title">{heart}{escape(title)}</div>'
            f'<div class="count">{len(entries)} file(s)</div></summary>'
            f'<ul>{"".join(entries)}</ul></details>'
        )

    @staticmethod
    def _entry(kind: str, ident: int, label: str, size: int) -> str:
        return (
            f'<li><a href="/dl/{kind}/{ident}">{escape(label)}</a>'
            f'<span class="size">{human_size(size)}</span></li>'
        )

    @staticmethod
    def _cover_url(url: str | None) -> str:
        """Mirror the app's cover-art URL upgrade for IGDB images."""
        if not url:
            return ""
        if "images.igdb.com" not in url or "t_cover_big_2x" in url:
            return url or ""
        for token in ("t_thumb", "t_cover_small", "t_cover_med", "t_cover_big"):
            if token in url:
                return url.replace(token, "t_cover_big_2x")
        return url

    # -- file download (with Range support) --------------------------------
    def _serve_file(self, kind: str, ident: int, *, include_body: bool) -> None:
        conn = self._db()
        try:
            row = conn.execute(
                f"SELECT file_path, file_name FROM {_TABLE[kind]} WHERE id = ?", (ident,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["file_path"] or not os.path.isfile(row["file_path"]):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        path = Path(row["file_path"])
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            parsed = self._parse_range(range_header, size)
            if parsed is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", self._disposition(row["file_name"]))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not include_body:
            return
        self._stream(path, start, length)

    @staticmethod
    def _parse_range(header: str, size: int) -> tuple[int, int] | None:
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
        if not match:
            return None
        start_s, end_s = match.group(1), match.group(2)
        if start_s == "" and end_s == "":
            return None
        if start_s == "":  # suffix range: last N bytes
            length = int(end_s)
            if length == 0:
                return None
            start = max(0, size - length)
            return start, size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return None
        return start, end

    @staticmethod
    def _disposition(file_name: str) -> str:
        ascii_name = file_name.encode("ascii", "replace").decode("ascii").replace('"', "")
        return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"

    def _stream(self, path: Path, start: int, length: int) -> None:
        remaining = length
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client cancelled the download

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class CatalogServer:
    """Start/stop wrapper around a threaded HTTP server for the catalog."""

    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> int | None:
        return self._httpd.server_address[1] if self._httpd else None

    def start(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        db_path: Path = DB_PATH,
        theme: str = "Dracula",
    ) -> None:
        self.stop()
        httpd = ThreadingHTTPServer((host, port), _Handler)
        httpd.daemon_threads = True
        httpd.catalog_config = {  # type: ignore[attr-defined]
            "username": username,
            "password": password,
            "db_path": db_path,
            "theme": theme,
        }
        thread = threading.Thread(target=httpd.serve_forever, name="catalog-server", daemon=True)
        thread.start()
        self._httpd = httpd
        self._thread = thread

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in &mdash; Switch Game Catalog</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         font: 15px system-ui, sans-serif; background: ${bg}; color: ${text}; }
  form { background: ${surface}; border: 1px solid ${border}; border-radius: 12px;
         padding: 28px; width: 300px; max-width: 90vw; }
  h1 { font-size: 18px; margin: 0 0 16px; }
  input { width: 100%; padding: 11px; border-radius: 8px; border: 1px solid ${border};
          background: ${bg}; color: ${text}; font-size: 16px; margin-bottom: 14px; }
  button { width: 100%; padding: 11px; border: 0; border-radius: 8px; cursor: pointer;
           background: ${accent}; color: ${bg}; font-size: 16px; font-weight: 700; }
  .err { color: #ff5555; margin: 0 0 12px; font-size: 14px; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Switch Game Catalog</h1>
  ${error}
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Sign in</button>
</form>
</body>
</html>"""


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switch Game Catalog</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px system-ui, sans-serif; background: ${bg}; color: ${text}; }
  header { position: sticky; top: 0; z-index: 5; background: ${surface};
           padding: 14px 18px; border-bottom: 1px solid ${border}; }
  header h1 { margin: 0 0 10px; font-size: 18px; }
  #filter { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid ${border};
            background: ${bg}; color: ${text}; font-size: 15px; }
  main { padding: 18px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .card { background: ${surface}; border: 1px solid ${border}; border-radius: 10px; overflow: hidden; }
  .card[open] { border-color: ${accent}; }
  .card.favorite { border-color: ${highlight}; }
  summary { list-style: none; cursor: pointer; }
  summary::-webkit-details-marker { display: none; }
  .art { width: 100%; aspect-ratio: 3 / 4; object-fit: cover; display: block; background: ${bg}; }
  .placeholder { display: flex; align-items: center; justify-content: center;
                 font-size: 42px; font-weight: 700; color: ${muted}; }
  .title { padding: 8px 10px 2px; font-size: 13px; font-weight: 600; line-height: 1.3; }
  .count { padding: 0 10px 9px; font-size: 12px; color: ${muted}; }
  .card ul { list-style: none; margin: 0; padding: 6px; border-top: 1px solid ${border}; }
  .card li { display: flex; justify-content: space-between; gap: 8px; align-items: center;
             padding: 7px 6px; border-radius: 6px; font-size: 13px; }
  .card li:hover { background: ${border}; }
  a { color: ${accent}; text-decoration: none; word-break: break-all; }
  a:hover { text-decoration: underline; }
  .size { color: ${muted}; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .empty { color: ${muted}; }
  .foot { margin-top: 24px; color: ${muted}; font-size: 13px; line-height: 1.6; }
  code { background: ${surface}; border: 1px solid ${border}; padding: 2px 6px; border-radius: 5px; }
</style>
</head>
<body>
<header>
  <h1>Switch Game Catalog &mdash; ${count} file(s)</h1>
  <input id="filter" type="search" placeholder="Filter by name&hellip;" autocomplete="off">
</header>
<main>${body}</main>
<script>
  var box = document.getElementById('filter');
  box.addEventListener('input', function () {
    var q = box.value.toLowerCase();
    var cards = document.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      cards[i].style.display = cards[i].dataset.name.indexOf(q) !== -1 ? '' : 'none';
    }
  });
</script>
</body>
</html>"""
