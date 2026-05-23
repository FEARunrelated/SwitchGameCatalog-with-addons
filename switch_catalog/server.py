"""Embedded HTTP server that lets other devices on the network browse and
download the catalog's game files over Wi-Fi.

Design notes / safety:
- Files are exposed only by their database id (``/dl/game/<id>`` and
  ``/dl/update/<id>``). The on-disk path is looked up from the catalog and the
  file is served only if it actually exists, so there is no way to request an
  arbitrary path on disk (no directory traversal, no filesystem listing).
- Access requires HTTP Basic Auth. Credentials are compared in constant time.
- The server runs in a background daemon thread so the Qt UI stays responsive.
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import socket
import sqlite3
import threading
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from .db import connect
from .paths import DB_PATH

_CHUNK = 256 * 1024
_DL_PATTERN = re.compile(r"^/dl/(game|update)/(\d+)$")
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
    def _authorized(self) -> bool:
        cfg = self._config
        expected = "Basic " + base64.b64encode(
            f"{cfg['username']}:{cfg['password']}".encode("utf-8")
        ).decode("ascii")
        provided = self.headers.get("Authorization", "")
        return hmac.compare_digest(provided, expected)

    def _send_auth_challenge(self) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Switch Game Catalog"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- entry points ------------------------------------------------------
    def do_GET(self) -> None:
        self._handle(include_body=True)

    def do_HEAD(self) -> None:
        self._handle(include_body=False)

    def _handle(self, *, include_body: bool) -> None:
        if not self._authorized():
            self._send_auth_challenge()
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index(include_body=include_body)
            return
        match = _DL_PATTERN.match(path)
        if match:
            self._serve_file(match.group(1), int(match.group(2)), include_body=include_body)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

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
                "SELECT id, display_title FROM games ORDER BY display_title COLLATE NOCASE"
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

        sections: list[str] = []
        total = 0
        for game in games:
            entries: list[str] = []
            for row in sorted(files.get(game["id"], []), key=lambda r: (not r["is_base_game"], r["file_name"].lower())):
                entries.append(self._entry("game", row["id"], row["file_name"], row["file_size"]))
                total += 1
            for row in sorted(updates.get(game["id"], []), key=lambda r: r["file_name"].lower()):
                label = row["file_name"]
                if row["detected_version"]:
                    label += f"  (v{row['detected_version']})"
                entries.append(self._entry("update", row["id"], label, row["file_size"]))
                total += 1
            if entries:
                sections.append(
                    f'<section class="game"><h2>{escape(game["display_title"])}</h2>'
                    f'<ul>{"".join(entries)}</ul></section>'
                )

        orphans = updates.get(None, [])
        if orphans:
            entries = [
                self._entry("update", row["id"], row["file_name"], row["file_size"])
                for row in sorted(orphans, key=lambda r: r["file_name"].lower())
            ]
            sections.append(
                f'<section class="game"><h2>Unmatched updates</h2>'
                f'<ul>{"".join(entries)}</ul></section>'
            )

        body = "".join(sections) or '<p class="empty">No games in the catalog yet. Run a scan in the app.</p>'
        return _PAGE.format(count=total, body=body)

    @staticmethod
    def _entry(kind: str, ident: int, label: str, size: int) -> str:
        return (
            f'<li data-name="{escape(label.lower(), quote=True)}">'
            f'<a href="/dl/{kind}/{ident}">{escape(label)}</a>'
            f'<span class="size">{human_size(size)}</span></li>'
        )

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
    ) -> None:
        self.stop()
        httpd = ThreadingHTTPServer((host, port), _Handler)
        httpd.daemon_threads = True
        httpd.catalog_config = {  # type: ignore[attr-defined]
            "username": username,
            "password": password,
            "db_path": db_path,
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


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switch Game Catalog</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; font: 15px system-ui, sans-serif; background: #282a36; color: #f8f8f2; }}
  header {{ position: sticky; top: 0; background: #21222c; padding: 16px 20px; border-bottom: 1px solid #44475a; }}
  header h1 {{ margin: 0 0 10px; font-size: 20px; }}
  #filter {{ width: 100%; box-sizing: border-box; padding: 10px; border-radius: 6px;
            border: 1px solid #44475a; background: #282a36; color: #f8f8f2; font-size: 15px; }}
  main {{ padding: 12px 20px 40px; }}
  .game {{ margin: 18px 0; }}
  .game h2 {{ font-size: 16px; color: #8be9fd; border-bottom: 1px solid #44475a; padding-bottom: 6px; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ display: flex; justify-content: space-between; align-items: center; gap: 12px;
        padding: 9px 10px; border-radius: 6px; }}
  li:hover {{ background: #44475a; }}
  a {{ color: #50fa7b; text-decoration: none; word-break: break-all; }}
  a:hover {{ text-decoration: underline; }}
  .size {{ color: #6272a4; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .empty {{ color: #6272a4; }}
</style>
</head>
<body>
<header>
  <h1>Switch Game Catalog &mdash; {count} file(s)</h1>
  <input id="filter" type="search" placeholder="Filter by name&hellip;" autocomplete="off">
</header>
<main>{body}</main>
<script>
  const box = document.getElementById('filter');
  box.addEventListener('input', () => {{
    const q = box.value.toLowerCase();
    for (const li of document.querySelectorAll('li')) {{
      li.style.display = li.dataset.name.includes(q) ? '' : 'none';
    }}
    for (const sec of document.querySelectorAll('section.game')) {{
      const any = [...sec.querySelectorAll('li')].some(li => li.style.display !== 'none');
      sec.style.display = any ? '' : 'none';
    }}
  }});
</script>
</body>
</html>"""
