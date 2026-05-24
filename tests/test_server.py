from __future__ import annotations

import base64
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

from switch_catalog import db
from switch_catalog.server import CatalogServer

USERNAME = "switch"
PASSWORD = "s3cret"
FILE_BYTES = b"NSP-CONTENT-0123456789"
GAME_NAME = "Test Game [0100000000010000][v0].nsp"
ALL = "All Games"


def _seed_db(tmp_path):
    db_path = tmp_path / "library.sqlite3"
    conn = db.connect(db_path)
    db.init_db(conn)
    game_file = tmp_path / GAME_NAME
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


# -- auth -----------------------------------------------------------------
def test_requires_auth(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/dir/")
    assert exc.value.code == 401
    assert "Basic" in exc.value.headers.get("WWW-Authenticate", "")


def test_rejects_wrong_password(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/dir/", auth=(USERNAME, "wrong"))
    assert exc.value.code == 401


GAME_TITLE = "Test Game"


def test_open_when_no_password(tmp_path):
    server = CatalogServer()
    server.start("127.0.0.1", 0, "switch", "", db_path=_seed_db(tmp_path))
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert f'{quote(ALL, safe="")}/' in _request(f"{base}/dir/").read().decode("utf-8")
        game_url = f"{base}/dir/{quote(ALL, safe='')}/{quote(GAME_TITLE, safe='')}/"
        assert f'href="{quote(GAME_NAME)}"' in _request(game_url).read().decode("utf-8")
        assert _request(game_url + quote(GAME_NAME)).read() == FILE_BYTES
        first = _request(f"{base}/list.txt").read().decode("utf-8").splitlines()[0]
        assert first.startswith(f"http://127.0.0.1:{server.port}/dl/")  # no embedded creds
    finally:
        server.stop()


# -- directory listing / groups (DBI ApacheHTTP) --------------------------
def test_dir_lists_group_folders(served):
    body = _request(f"{served}/dir/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    assert f'href="{quote(ALL, safe="")}/"' in body  # built-in "All Games" folder


def test_root_serves_group_folders(served):
    body = _request(f"{served}/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    assert f"{ALL}/" in body


def test_group_lists_game_folders_then_files(served):
    # /dir/<group>/ lists games (not files), /dir/<group>/<game>/ lists the files
    games = _request(f"{served}/dir/{quote(ALL, safe='')}/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    assert f'href="{quote(GAME_TITLE, safe="")}/"' in games  # a game folder, not a file
    assert ".nsp" not in games  # files are not listed at the group level

    game_url = f"{served}/dir/{quote(ALL, safe='')}/{quote(GAME_TITLE, safe='')}/"
    files = _request(game_url, auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    assert f'href="{quote(GAME_NAME)}"' in files

    resp = _request(game_url + quote(GAME_NAME), auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES
    assert resp.headers.get("Accept-Ranges") == "bytes"


def test_game_folder_includes_update(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = db.connect(db_path)
    update_file = tmp_path / "Test Game [0100000000010800][v131072].nsp"
    update_file.write_bytes(b"UPDATE")
    conn.execute(
        "INSERT INTO updates(game_id, file_path, file_name, file_size, modified_time) VALUES (1, ?, ?, ?, 0.0)",
        (str(update_file), update_file.name, 6),
    )
    conn.commit()
    conn.close()
    server = CatalogServer()
    server.start("127.0.0.1", 0, USERNAME, PASSWORD, db_path=db_path)
    try:
        base = f"http://127.0.0.1:{server.port}"
        files = _request(
            f"{base}/dir/{quote(ALL, safe='')}/{quote(GAME_TITLE, safe='')}/", auth=(USERNAME, PASSWORD)
        ).read().decode("utf-8")
        assert f'href="{quote(GAME_NAME)}"' in files  # base game
        assert f'href="{quote(update_file.name)}"' in files  # its update, alongside
    finally:
        server.stop()


def test_custom_group_folder(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = db.connect(db_path)
    group_id = db.create_group(conn, "Favorites")
    db.add_game_to_group(conn, 1, group_id)  # game id 1 from _seed_db
    conn.close()
    server = CatalogServer()
    server.start("127.0.0.1", 0, USERNAME, PASSWORD, db_path=db_path)
    try:
        base = f"http://127.0.0.1:{server.port}"
        groups = _request(f"{base}/dir/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
        assert 'href="Favorites/"' in groups
        games = _request(f"{base}/dir/Favorites/", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
        assert f'href="{quote(GAME_TITLE, safe="")}/"' in games
    finally:
        server.stop()


def test_unknown_group_is_404(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request(f"{served}/dir/Nope/", auth=(USERNAME, PASSWORD))
    assert exc.value.code == 404


def test_group_db_helpers(tmp_path):
    conn = db.connect(_seed_db(tmp_path))
    try:
        assert db.list_group_names(conn) == []
        group_id = db.create_group(conn, "RPGs")
        db.add_game_to_group(conn, 1, group_id)
        assert db.list_group_names(conn) == ["RPGs"]
        assert db.group_game_titles(conn, "RPGs") == [GAME_TITLE]
        assert db.group_game_titles(conn, "Nope") is None
        assert GAME_NAME in db.game_file_names(conn, GAME_TITLE)
        db.remove_game_from_group(conn, 1, "RPGs")
        assert db.group_game_titles(conn, "RPGs") == []
        db.delete_group(conn, "RPGs")
        assert db.list_group_names(conn) == []
    finally:
        conn.close()


def test_trailing_slash_is_tolerated(served):
    assert _request(f"{served}/dir/", auth=(USERNAME, PASSWORD)).status == 200
    assert _request(f"{served}/list.txt/", auth=(USERNAME, PASSWORD)).status == 200


# -- url list / direct download -------------------------------------------
def test_url_list(served):
    body = _request(f"{served}/list.txt", auth=(USERNAME, PASSWORD)).read().decode("utf-8")
    lines = [line for line in body.splitlines() if line]
    assert lines
    assert lines[0].startswith(f"http://{USERNAME}:{PASSWORD}@")  # embedded creds when password set
    assert "/dl/game/1/" in lines[0]


def test_download_by_id(served):
    resp = _request(f"{served}/dl/game/1", auth=(USERNAME, PASSWORD))
    assert resp.status == 200
    assert resp.read() == FILE_BYTES


def test_download_with_filename_suffix(served):
    resp = _request(f"{served}/dl/game/1/Test%20Game.nsp", auth=(USERNAME, PASSWORD))
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
