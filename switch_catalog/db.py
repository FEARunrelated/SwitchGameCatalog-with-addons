from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .paths import DB_PATH, ensure_app_dirs


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_app_dirs()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_title TEXT NOT NULL,
            cleaned_title TEXT NOT NULL UNIQUE,
            metadata_provider TEXT,
            metadata_provider_id TEXT,
            description TEXT,
            release_date TEXT,
            developer TEXT,
            publisher TEXT,
            genres TEXT,
            cover_image_path TEXT,
            cover_image_url TEXT,
            date_added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_scanned TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata_locked INTEGER NOT NULL DEFAULT 0,
            needs_review INTEGER NOT NULL DEFAULT 0,
            favorite INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS game_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL,
            file_extension TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            modified_time REAL NOT NULL,
            file_type TEXT NOT NULL,
            is_base_game INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
            file_path TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL,
            detected_version TEXT,
            file_size INTEGER NOT NULL,
            modified_time REAL NOT NULL,
            match_confidence REAL NOT NULL DEFAULT 0,
            manual_match INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS screenshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            image_url TEXT NOT NULL,
            local_path TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(game_id, image_url)
        );

        CREATE TABLE IF NOT EXISTS metadata_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            query TEXT NOT NULL,
            response_json TEXT NOT NULL,
            cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, query)
        );

        CREATE TABLE IF NOT EXISTS catalog_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        -- Membership is keyed by the stable cleaned_title (not games.id) so that
        -- groups survive a rescan, which deletes and re-inserts game rows.
        CREATE TABLE IF NOT EXISTS game_group_members (
            group_id INTEGER NOT NULL REFERENCES catalog_groups(id) ON DELETE CASCADE,
            cleaned_title TEXT NOT NULL,
            PRIMARY KEY (group_id, cleaned_title)
        );
        """
    )
    _ensure_column(conn, "updates", "manual_match", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "games", "favorite", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def reset_library_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM updates")
    conn.execute("DELETE FROM screenshots")
    conn.execute("DELETE FROM game_files")
    conn.execute("DELETE FROM games")
    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN ('updates', 'screenshots', 'game_files', 'games')"
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("genres"):
        try:
            data["genres"] = json.loads(data["genres"])
        except json.JSONDecodeError:
            data["genres"] = []
    return data


def upsert_cache(conn: sqlite3.Connection, provider: str, query: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO metadata_cache(provider, query, response_json, cached_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(provider, query) DO UPDATE SET
            response_json=excluded.response_json,
            cached_at=CURRENT_TIMESTAMP
        """,
        (provider, query, json.dumps(payload)),
    )
    conn.commit()


GROUP_ALL = "All Games"


def list_group_names(conn: sqlite3.Connection) -> list[str]:
    return [row["name"] for row in conn.execute("SELECT name FROM catalog_groups ORDER BY name COLLATE NOCASE")]


def create_group(conn: sqlite3.Connection, name: str) -> int | None:
    name = name.strip().replace("/", " ").strip()
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO catalog_groups(name) VALUES (?)", (name,))
    conn.commit()
    row = conn.execute("SELECT id FROM catalog_groups WHERE name=?", (name,)).fetchone()
    return int(row["id"]) if row else None


def delete_group(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM catalog_groups WHERE name=?", (name,))
    conn.commit()


def _cleaned_title(conn: sqlite3.Connection, game_id: int) -> str | None:
    row = conn.execute("SELECT cleaned_title FROM games WHERE id=?", (game_id,)).fetchone()
    return row["cleaned_title"] if row else None


def add_game_to_group(conn: sqlite3.Connection, game_id: int, group_id: int) -> None:
    cleaned = _cleaned_title(conn, game_id)
    if cleaned is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO game_group_members(group_id, cleaned_title) VALUES (?, ?)",
        (group_id, cleaned),
    )
    conn.commit()


def remove_game_from_group(conn: sqlite3.Connection, game_id: int, group_name: str) -> None:
    cleaned = _cleaned_title(conn, game_id)
    if cleaned is None:
        return
    conn.execute(
        "DELETE FROM game_group_members WHERE cleaned_title=? AND "
        "group_id IN (SELECT id FROM catalog_groups WHERE name=?)",
        (cleaned, group_name),
    )
    conn.commit()


def group_game_titles(conn: sqlite3.Connection, group_name: str) -> list[str] | None:
    """Display titles of the games in a group (or all games for GROUP_ALL).
    Returns None if the named group does not exist."""
    if group_name == GROUP_ALL:
        rows = conn.execute(
            "SELECT DISTINCT display_title FROM games ORDER BY display_title COLLATE NOCASE"
        )
        return [row["display_title"] for row in rows]
    group = conn.execute("SELECT id FROM catalog_groups WHERE name=?", (group_name,)).fetchone()
    if group is None:
        return None
    rows = conn.execute(
        "SELECT DISTINCT display_title FROM games WHERE cleaned_title IN "
        "(SELECT cleaned_title FROM game_group_members WHERE group_id=?) "
        "ORDER BY display_title COLLATE NOCASE",
        (group["id"],),
    )
    return [row["display_title"] for row in rows]


def game_file_names(conn: sqlite3.Connection, display_title: str) -> list[str]:
    """The base file(s) and matched update/DLC files for a game, base game first."""
    ids = [row["id"] for row in conn.execute("SELECT id FROM games WHERE display_title=?", (display_title,))]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    base = [row["file_name"] for row in conn.execute(
        f"SELECT file_name FROM game_files WHERE game_id IN ({placeholders}) ORDER BY is_base_game DESC, file_name", ids)]
    updates = [row["file_name"] for row in conn.execute(
        f"SELECT file_name FROM updates WHERE game_id IN ({placeholders}) ORDER BY file_name", ids)]
    return base + updates


def group_file_names(conn: sqlite3.Connection, group_name: str) -> list[str] | None:
    """All file names (base games + updates) for a group, or all games when
    group_name is GROUP_ALL. Returns None if the named group does not exist.
    Used by /list.txt; the DBI folder view uses group_game_titles/game_file_names."""
    if group_name == GROUP_ALL:
        rows = conn.execute("SELECT file_name FROM game_files").fetchall()
        rows += conn.execute("SELECT file_name FROM updates").fetchall()
        return [row["file_name"] for row in rows]
    group = conn.execute("SELECT id FROM catalog_groups WHERE name=?", (group_name,)).fetchone()
    if group is None:
        return None
    member = "(SELECT id FROM games WHERE cleaned_title IN (SELECT cleaned_title FROM game_group_members WHERE group_id=?))"
    rows = conn.execute(f"SELECT file_name FROM game_files WHERE game_id IN {member}", (group["id"],)).fetchall()
    rows += conn.execute(f"SELECT file_name FROM updates WHERE game_id IN {member}", (group["id"],)).fetchall()
    return [row["file_name"] for row in rows]


def get_cache(conn: sqlite3.Connection, provider: str, query: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT response_json FROM metadata_cache WHERE provider=? AND query=?",
        (provider, query),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["response_json"])
    except json.JSONDecodeError:
        return None
