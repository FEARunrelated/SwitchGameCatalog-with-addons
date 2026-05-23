from __future__ import annotations

import base64
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

import pytest

from switch_catalog import db
from switch_catalog.server import CatalogServer, human_size

USERNAME = "switch"
PASSWORD = "s3cret"
FILE_BYTES = b"NSP-CONTENT-0123456789"
COVER = "https://images.igdb.com/igdb/image/upload/t_cover_big/abc.jpg"


def _seed_db(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    conn = db.connect(db_path)
    db.init_db(conn)
    game_file = tmp_path / "Test Game [0100].nsp"
    game_file.write_bytes(FILE_BYTES)
    game_id = conn.execute(
        "INSERT INTO games(display_title, cleaned_title, cover_image_url, favorite) VALUES (?, ?, ?, 1)",
        ("Test Game", "test game", COVER),
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


def _serve(tmp_path, theme="Dracula"):
    server = CatalogServer()
    server.start("127.0.0.1", 0, USERNAME, PASSWORD, db_path=_seed_db(tmp_path), theme=theme)
    return server


@pytest.fixture
def served(tmp_path):
    server = _serve(tmp_path)
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


def _body(served: str) -> str:
    return _request(f"{served}/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")


def test_unauthenticated_shows_login(served):
    # No Basic Auth popup (the Switch browser can't handle it): a 200 login page instead.
    resp = _request(f"{served}/")
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert 'name="password"' in body
    assert "${" not in body  # all theme placeholders were substituted
    assert "Test Game" not in body  # catalog stays hidden until signed in


def test_wrong_basic_shows_login(served):
    body = _request(f"{served}/", auth=(USERNAME, "wrong")).read().decode("utf-8")
    assert 'name="password"' in body
    assert "Test Game" not in body


def test_login_form_grants_access(served):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"password": PASSWORD}).encode()
    body = opener.open(f"{served}/login", data=data, timeout=5).read().decode("utf-8")
    assert "Test Game" in body  # redirected to the catalog via the session cookie
    assert any(cookie.name == "sgc" for cookie in jar)


def test_login_wrong_password_denied(served):
    data = urllib.parse.urlencode({"password": "nope"}).encode()
    body = urllib.request.urlopen(f"{served}/login", data=data, timeout=5).read().decode("utf-8")
    assert 'name="password"' in body  # back to the login form
    assert "Test Game" not in body


def test_index_lists_games(served):
    body = _body(served)
    assert "Test Game" in body
    assert "/dl/game/1" in body


def test_index_grid_card(served):
    body = _body(served)
    # favorite game -> highlighted card + heart, and a cover-art tile
    assert 'class="card favorite"' in body
    assert "♥" in body  # heart
    # IGDB cover URL is upgraded to the high-res variant, like the app's grid
    assert "t_cover_big_2x/abc.jpg" in body
    assert 'class="art"' in body
    assert 'class="grid"' in body


def test_index_uses_dracula_colors(served):
    assert "#282a36" in _body(served)  # Dracula background


def test_index_uses_oled_colors(tmp_path):
    server = _serve(tmp_path, theme="OLED Dark")
    try:
        body = _body(f"http://127.0.0.1:{server.port}")
    finally:
        server.stop()
    assert "#000000" in body  # OLED true-black background
    assert "#282a36" not in body  # not the Dracula background


def test_download_file(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    assert resp.headers.get("Accept-Ranges") == "bytes"


def test_tinfoil_index(served):
    import json

    data = json.loads(_request(f"{served}/tinfoil", auth=(USERNAME, PASSWORD)).read().decode("utf-8"))
    assert isinstance(data.get("files"), list) and data["files"]
    entry = data["files"][0]
    assert "/dl/game/1/" in entry["url"]  # url carries the filename for title-id parsing
    assert entry["size"] == len(FILE_BYTES)


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


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024**3) == "5.0 GB"
