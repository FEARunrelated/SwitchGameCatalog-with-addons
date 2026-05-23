"""Embedded HTTP server that exposes the catalog as a Tinfoil/DBI network source,
so a homebrew Switch installer can download and install games over Wi-Fi.

Design notes / safety:
- Files are exposed only by their database id (``/dl/game/<id>`` and
  ``/dl/update/<id>``, with an optional trailing ``/<filename>`` the installer
  reads the title id from). The on-disk path is looked up from the catalog and
  the file is served only if it exists, so there is no directory traversal or
  arbitrary filesystem access.
- Access requires HTTP Basic Auth (what Tinfoil/DBI send); secrets are compared
  in constant time.
- The server runs in a background daemon thread so the Qt UI stays responsive.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import socket
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from .db import connect
from .paths import APP_DIR, DB_PATH

_CHUNK = 256 * 1024
# Optional trailing "/<filename>" lets installers like Tinfoil read the title id
# from the URL; it is ignored for lookup (routing is by id).
_DL_PATTERN = re.compile(r"^/dl/(game|update)/(\d+)(?:/.*)?$")
_INDEX_PATHS = ("/", "/tinfoil", "/tinfoil.json", "/index.json")
_LIST_PATHS = ("/list.txt", "/awoo.txt")
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


class _Handler(BaseHTTPRequestHandler):
    server_version = "SwitchGameCatalog"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Append a one-line access record (with any Range header) to server.log
        # to help diagnose installer downloads. Best-effort; never raises.
        try:
            rng = self.headers.get("Range", "-") if self.headers else "-"
            with open(APP_DIR / "server.log", "a", encoding="utf-8") as handle:
                handle.write(f"{self.log_date_time_string()} {fmt % args} Range={rng}\n")
        except Exception:
            pass

    @property
    def _config(self) -> dict:
        return self.server.catalog_config  # type: ignore[attr-defined]

    def _db(self) -> sqlite3.Connection:
        return connect(self._config["db_path"])

    # -- auth --------------------------------------------------------------
    def _authorized(self) -> bool:
        cfg = self._config
        if not cfg["password"]:
            return True  # no password configured -> open server (LAN use)
        expected = "Basic " + base64.b64encode(
            f"{cfg['username']}:{cfg['password']}".encode("utf-8")
        ).decode("ascii")
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

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
        if path in _INDEX_PATHS:
            self._serve_tinfoil(include_body=include_body)
            return
        if path in _LIST_PATHS:
            self._serve_url_list(include_body=include_body)
            return
        match = _DL_PATTERN.match(path)
        if match:
            self._serve_file(match.group(1), int(match.group(2)), include_body=include_body)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    # -- tinfoil index -----------------------------------------------------
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
            "success": f"Switch Game Catalog - {len(files)} file(s)",
        }
        data = json.dumps(payload).encode("utf-8")
        self._send_payload(data, "application/json", include_body=include_body)

    def _serve_url_list(self, *, include_body: bool) -> None:
        """Plain text, one download URL per line, for Awoo Installer's "Install
        from URL". Awoo has no separate credential fields, so the username and
        password are embedded in each URL (the list itself is auth-protected).
        """
        cfg = self._config
        host = self.headers.get("Host") or f"{get_lan_ip()}:{self.server.server_address[1]}"
        cred = ""
        if cfg["password"]:
            cred = f"{quote(cfg['username'], safe='')}:{quote(cfg['password'], safe='')}@"
        base = f"http://{cred}{host}"
        conn = self._db()
        try:
            lines = [
                f"{base}/dl/game/{row['id']}/{quote(row['file_name'])}"
                for row in conn.execute("SELECT id, file_name FROM game_files")
            ]
            lines += [
                f"{base}/dl/update/{row['id']}/{quote(row['file_name'])}"
                for row in conn.execute("SELECT id, file_name FROM updates")
            ]
        finally:
            conn.close()
        data = ("\n".join(lines) + "\n").encode("utf-8")
        self._send_payload(data, "text/plain; charset=utf-8", include_body=include_body)

    def _send_payload(self, data: bytes, content_type: str, *, include_body: bool) -> None:
        """Send an in-memory body with Range support, so installers that probe
        the index/list URL for range support (and then range-download) are happy.
        """
        size = len(data)
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
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if include_body:
            self._write(chunk)

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
            return max(0, size - length), size - 1
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
