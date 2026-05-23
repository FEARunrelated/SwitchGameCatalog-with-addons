from __future__ import annotations

import base64
import urllib.error
import urllib.request

import pytest

from switch_catalog import db
from switch_catalog.server import CatalogServer, human_size

USERNAME = "switch"
PASSWORD = "s3cret"
FILE_BYTES = b"NSP-CONTENT-0123456789"


@pytest.fixture
def served(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    conn = db.connect(db_path)
    db.init_db(conn)
    game_file = tmp_path / "Test Game [0100].nsp"
    game_file.write_bytes(FILE_BYTES)
    cur = conn.execute(
        "INSERT INTO games(display_title, cleaned_title) VALUES (?, ?)",
        ("Test Game", "test game"),
    )
    game_id = cur.lastrowid
    conn.execute(
        """INSERT INTO game_files
           (game_id, file_path, file_name, file_extension, file_size, modified_time, file_type, is_base_game)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (game_id, str(game_file), game_file.name, ".nsp", len(FILE_BYTES), 0.0, "base"),
    )
    conn.commit()
    conn.close()

    server = CatalogServer()
    server.start("127.0.0.1", 0, USERNAME, PASSWORD, db_path=db_path)
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
        _request(f"{served}/")
    assert exc.value.code == 401
    assert "Basic" in exc.value.headers.get("WWW-Authenticate", "")


def test_rejects_wrong_password(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/", auth=(USERNAME, "wrong"))
    assert exc.value.code == 401


def test_index_lists_games(served):
    resp = _request(f"{served}/", auth=(USERNAME, PASSWORD))
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "Test Game" in body
    assert "/dl/game/1" in body


def test_download_file(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert resp.headers.get("Accept-Ranges") == "bytes"


def test_range_request(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD), headers={"Range": "bytes=0-3"})
    assert resp.status == 206
    assert resp.read() == FILE_BYTES[:4]
    assert resp.headers.get("Content-Range") == f"bytes 0-3/{len(FILE_BYTES)}"


def test_missing_file_is_404(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/dl/game/9999", auth=(USERNAME, PASSWORD))
    assert exc.value.code == 404


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024**3) == "5.0 GB"
