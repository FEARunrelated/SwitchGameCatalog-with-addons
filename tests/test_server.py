from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from switch_catalog import db
from switch_catalog.server import CatalogServer

USERNAME = "switch"
PASSWORD = "s3cret"
FILE_BYTES = b"NSP-CONTENT-0123456789"


def _seed_db(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    conn = db.connect(db_path)
    db.init_db(conn)
    game_file = tmp_path / "Test Game [0100000000010000][v0].nsp"
    game_file.write_bytes(FILE_BYTES)
    game_id = conn.execute(
        "INSERT INTO games(display_title, cleaned_title) VALUES (?, ?)",
        ("Test Game", "test game"),
    ).lastrowid
    conn.execute(
        """INSERT INTO game_files
           (game_id, file_path, file_name, file_extension, file_size, modified_time, file_type, is_base_game)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (game_id, str(game_file), game_file.name, ".nsp", len(FILE_BYTES), 0.0, "base"),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def served(tmp_path):
    server = CatalogServer()
    server.start("127.0.0.1", 0, USERNAME, PASSWORD, db_path=_seed_db(tmp_path))
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.stop()


def _request(url: str, *, auth: tuple[str, str] | None = None, headers: dict | None = None):
    req = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return urllib.request.urlopen(req, timeout=5)


def test_requires_auth(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/tinfoil")
    assert exc.value.code == 401
    assert "Basic" in exc.value.headers.get("WWW-Authenticate", "")


def test_rejects_wrong_password(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/tinfoil", auth=(USERNAME, "wrong"))
    assert exc.value.code == 401


def test_tinfoil_index(served):
    data = json.loads(_request(f"{served}/tinfoil", auth=(USERNAME, PASSWORD)).read().decode("utf-8"))
    assert isinstance(data.get("files"), list) and data["files"]
    entry = data["files"][0]
    assert "/dl/game/1/" in entry["url"]  # url carries the filename for title-id parsing
    assert "%5B0100000000010000%5D" in entry["url"]  # the title id, url-encoded
    assert entry["size"] == len(FILE_BYTES)


def test_url_list_for_awoo(served):
    body = _request(f"{served}/list.txt", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    lines = [line for line in body.splitlines() if line]
    assert lines
    # Awoo has no credential fields, so the URLs carry user:pass and the filename
    assert lines[0].startswith(f"http://{USERNAME}:{PASSWORD}@")
    assert "/dl/game/1/" in lines[0]


def test_url_list_requires_auth(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/list.txt")
    assert exc.value.code == 401


def test_open_when_no_password(tmp_path):
    server = CatalogServer()
    server.start("127.0.0.1", 0, "switch", "", db_path=_seed_db(tmp_path))
    try:
        base = f"http://127.0.0.1:{server.port}"
        # no auth required
        assert json.loads(_request(f"{base}/tinfoil").read().decode())["files"]
        assert _request(f"{base}/dl/game/1").read() == FILE_BYTES
        # list URLs carry no embedded credentials when there is no password
        first = _request(f"{base}/list.txt").read().decode().splitlines()[0]
        assert first.startswith(f"http://127.0.0.1:{server.port}/dl/")
    finally:
        server.stop()


def test_trailing_slash_is_tolerated(served):
    # DBI requests the source URL with a trailing slash (e.g. /list.txt/)
    assert _request(f"{served}/list.txt/", auth=(USERNAME, PASSWORD)).status == 200
    assert _request(f"{served}/tinfoil/", auth=(USERNAME, PASSWORD)).status == 200


def test_root_serves_index(served):
    # "/" returns the same Tinfoil index, so any configured path works
    data = json.loads(_request(f"{served}/", auth=(USERNAME, PASSWORD)).read().decode("utf-8"))
    assert data["files"][0]["size"] == len(FILE_BYTES)


def test_index_advertises_and_honors_ranges(served):
    # installers may probe the index/list URL for range support before downloading
    resp = _request(f"{served}/tinfoil", auth=(USERNAME, PASSWORD))
    assert resp.headers.get("Accept-Ranges") == "bytes"
    ranged = _request(f"{served}/list.txt", auth=(USERNAME, PASSWORD), headers={"Range": "bytes=0-3"})
    assert ranged.status == 206
    assert len(ranged.read()) == 4
    assert ranged.headers.get("Accept-Ranges") == "bytes"


def test_download_file(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert resp.headers.get("Accept-Ranges") == "bytes"


def test_download_with_filename_suffix(served):
    # Tinfoil-style URL with a trailing filename still serves the file (routed by id)
    resp = _request(f"{served}/dl/game/1/Test%20Game.nsp", auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES


def test_range_request(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD), headers={"Range": "bytes=0-3"})
    assert resp.status == 206
    assert resp.read() == FILE_BYTES[:4]
    assert resp.headers.get("Content-Range") == f"bytes 0-3/{len(FILE_BYTES)}"


def test_missing_file_is_404(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/dl/game/9999", auth=(USERNAME, PASSWORD))
    assert exc.value.code == 404
