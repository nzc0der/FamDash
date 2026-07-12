"""
settings.py - Central configuration and utility module.

This module is a pure library. It does NOT start a server, run interactive
prompts, or perform any I/O side-effects at import time. All functions are
safe to call from any context.

Responsibilities:
  - Loading / saving / resetting config.json.
  - SQLite user management (get, add, verify).\
  - System-level helpers (is_initialized, path resolution).
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

import bcrypt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

# The project root is always the directory that contains this file.
PROJECT_ROOT: str = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH: str = os.path.join(PROJECT_ROOT, "config.json")

# Default paths written by setup.py; settings.py can override via config.
_DEFAULT_DB_PATH: str = os.path.join(PROJECT_ROOT, "data", "family.db")
_DEFAULT_BACKUP_DIR: str = os.path.join(PROJECT_ROOT, "backups")

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """
    Load and return the parsed config.json dictionary.

    Returns an empty dict if the file does not yet exist (pre-setup state).
    Raises ValueError if the file exists but is not valid JSON.
    """
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config.json is malformed: {exc}") from exc


def save_config(data: dict) -> None:
    """
    Atomically write *data* to config.json.

    Uses a temporary sibling file and os.replace() so the config is never
    left in a partially-written state on power loss.
    """
    tmp_path = CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        logger.debug("config.json saved successfully.")
    except OSError as exc:
        logger.error("Failed to save config.json: %s", exc)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def reset_config() -> None:
    """
    Remove config.json, causing is_initialized() to return False.

    This will force setup.py to run on next server start.  Should only be
    called deliberately during a manual system reset.
    """
    if os.path.isfile(CONFIG_PATH):
        os.remove(CONFIG_PATH)
        logger.warning("config.json removed. System will reinitialize on next start.")


def is_initialized() -> bool:
    """
    Return True if and only if config.json exists and is non-empty.

    This is the single source of truth that server.py uses to decide whether
    setup.py must be invoked first.
    """
    try:
        cfg = load_config()
        return bool(cfg)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Database path resolution
# ---------------------------------------------------------------------------


def _db_path() -> str:
    """Return the absolute path to the SQLite database from config or default."""
    cfg = load_config()
    return cfg.get("db_path", _DEFAULT_DB_PATH)


def _backup_dir() -> str:
    """Return the absolute path to the backups directory from config or default."""
    cfg = load_config()
    return cfg.get("backup_dir", _DEFAULT_BACKUP_DIR)


# ---------------------------------------------------------------------------
# Database connection context manager
# ---------------------------------------------------------------------------


@contextmanager
def _get_db():
    """
    Yield an open sqlite3 Connection with WAL journal mode enabled.

    WAL (Write-Ahead Logging) is chosen because it allows concurrent reads
    during a write and significantly reduces corruption risk on power failure.
    Row factory is set so that rows behave like dicts.
    """
    db_path = _db_path()
    # Ensure the parent directory exists.
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def init_schema() -> None:
    """
    Create all required tables if they do not already exist.

    Every statement uses CREATE TABLE IF NOT EXISTS so that this function is
    idempotent and safe to call on an already-populated database.  We NEVER
    use DROP TABLE here.
    """
    with _get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    NOT NULL UNIQUE,
                password_hash TEXT,
                role        TEXT    NOT NULL DEFAULT 'user',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS todos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content     TEXT    NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title       TEXT    NOT NULL DEFAULT '',
                content     TEXT    NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
                title       TEXT    NOT NULL,
                body        TEXT    NOT NULL DEFAULT '',
                pinned      INTEGER NOT NULL DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bookmarks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                url         TEXT    NOT NULL,
                icon        TEXT    DEFAULT '🌐',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                event_date  DATETIME NOT NULL,
                author_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS shopping_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name   TEXT    NOT NULL,
                added_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        
        try:
            conn.execute("ALTER TABLE users ADD COLUMN shopping_permission TEXT DEFAULT 'full';")
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE events ADD COLUMN end_date DATETIME;")
        except sqlite3.OperationalError:
            pass
    logger.info("Database schema verified / created.")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


def get_users() -> list[dict]:
    """
    Return a list of all user records as plain dicts.

    Passwords hashes are intentionally included because verify_user() needs
    them.  Callers that expose user data to templates should omit the hash.
    """
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, password_hash, role, created_at, shopping_permission FROM users ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_user_by_username(username: str) -> Optional[dict]:
    """Return a single user dict or None if not found."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, shopping_permission FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Return a single user dict by primary key, or None."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, shopping_permission FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def add_user(username: str, password: Optional[str], role: str = "user", shopping_permission: str = "full") -> int:
    """
    Insert a new user and return the new row id.

    Parameters
    ----------
    username : str
        Must be unique.
    password : Optional[str]
        Plain-text password.  Pass None for the guest account (no password).
    role : str
        Either "user" or "admin".  Guest uses "guest".
    shopping_permission: str
        "read", "add", or "full"

    Returns the new user id.
    Raises ValueError on duplicate username.
    """
    if get_user_by_username(username) is not None:
        raise ValueError(f"Username '{username}' already exists.")

    password_hash: Optional[str] = None
    if password is not None:
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("utf-8")

    with _get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role, shopping_permission) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, shopping_permission),
        )
        new_id = cursor.lastrowid

    logger.info("User '%s' added with role '%s'.", username, role)
    return new_id


def verify_user(username: str, password: str) -> Optional[dict]:
    """
    Verify credentials and return the user dict on success, else None.

    The guest account (password_hash IS NULL) is never authenticated through
    this function; guest login is handled separately in server.py.
    """
    user = get_user_by_username(username)
    if user is None:
        return None
    if user["password_hash"] is None:
        # Guest account — not authenticated via password.
        return None
    try:
        if bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
            return user
    except Exception as exc:
        logger.error("bcrypt verification error for user '%s': %s", username, exc)
    return None


def change_password(user_id: int, new_password: str) -> None:
    """Update the password for a given user."""
    password_hash = bcrypt.hashpw(
        new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")
    with _get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id)
        )
    logger.info("Password changed for user id %d.", user_id)


def rename_user(user_id: int, new_username: str) -> None:
    """Change the username for a given user."""
    # Check for duplicates
    existing = get_user_by_username(new_username)
    if existing and existing["id"] != user_id:
        raise ValueError(f"Username '{new_username}' is already taken.")
        
    with _get_db() as conn:
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username.lower().strip(), user_id))
    logger.info("User id %d renamed to '%s'.", user_id, new_username)


def set_shopping_permission(user_id: int, permission: str) -> None:
    """Change the shopping permission for a given user."""
    if permission not in ("read", "add", "full"):
        raise ValueError("Invalid permission level")
    with _get_db() as conn:
        conn.execute("UPDATE users SET shopping_permission = ? WHERE id = ?", (permission, user_id))
    logger.info("User id %d shopping permission set to '%s'.", user_id, permission)


def delete_user(user_id: int) -> None:
    """Delete a user account."""
    with _get_db() as conn:
        # Check if we are deleting the last admin
        user = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if user and user["role"] == "admin":
            admin_count = conn.execute("SELECT COUNT(*) as count FROM users WHERE role = 'admin'").fetchone()
            if admin_count["count"] <= 1:
                raise ValueError("Cannot delete the last admin account.")
                
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    logger.info("User id %d deleted.", user_id)


# ---------------------------------------------------------------------------
# Todo helpers
# ---------------------------------------------------------------------------


def get_todos(user_id: int) -> list[dict]:
    """Return all to-dos for a user, ordered by done status then date."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT t.*, u.username as author
            FROM todos t
            JOIN users u ON u.id = t.user_id
            WHERE t.user_id = ?
            ORDER BY t.done ASC, t.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_todo(user_id: int, content: str) -> int:
    """Add a to-do item and return its id."""
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO todos (user_id, content) VALUES (?, ?)",
            (user_id, content.strip()),
        )
        return cur.lastrowid


def toggle_todo(todo_id: int, user_id: int) -> None:
    """Flip the done flag on a to-do item owned by user_id."""
    with _get_db() as conn:
        conn.execute(
            """
            UPDATE todos
               SET done = CASE WHEN done = 0 THEN 1 ELSE 0 END,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND user_id = ?
            """,
            (todo_id, user_id),
        )


def delete_todo(todo_id: int, user_id: int) -> None:
    """Delete a to-do item owned by user_id."""
    with _get_db() as conn:
        conn.execute(
            "DELETE FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, user_id),
        )


# ---------------------------------------------------------------------------
# Note helpers
# ---------------------------------------------------------------------------


def get_notes(user_id: int) -> list[dict]:
    """Return all notes for a user, ordered newest first."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT n.*, u.username as author
            FROM notes n
            JOIN users u ON u.id = n.user_id
            WHERE n.user_id = ?
            ORDER BY n.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_note(user_id: int, note_id: Optional[int], title: str, content: str) -> int:
    """
    Insert a new note or update an existing one.

    Returns the note id.
    """
    with _get_db() as conn:
        if note_id is None:
            cur = conn.execute(
                "INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)",
                (user_id, title.strip(), content.strip()),
            )
            return cur.lastrowid
        else:
            conn.execute(
                """
                UPDATE notes
                   SET title = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ? AND user_id = ?
                """,
                (title.strip(), content.strip(), note_id, user_id),
            )
            return note_id


def delete_note(note_id: int, user_id: int) -> None:
    """Delete a note owned by user_id."""
    with _get_db() as conn:
        conn.execute(
            "DELETE FROM notes WHERE id = ? AND user_id = ?",
            (note_id, user_id),
        )


# ---------------------------------------------------------------------------
# Announcement helpers
# ---------------------------------------------------------------------------


def get_announcements() -> list[dict]:
    """
    Return all announcements, pinned first then newest first.

    Joins the username from the users table for display.
    """
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.body, a.pinned, a.created_at, a.updated_at,
                   u.username AS author
              FROM announcements a
              LEFT JOIN users u ON u.id = a.author_id
             ORDER BY a.pinned DESC, a.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def add_announcement(author_id: int, title: str, body: str, pinned: bool = False) -> int:
    """Insert a new announcement and return its id."""
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO announcements (author_id, title, body, pinned) VALUES (?, ?, ?, ?)",
            (author_id, title.strip(), body.strip(), int(pinned)),
        )
        return cur.lastrowid


def delete_announcement(ann_id: int) -> None:
    """Delete an announcement by id (admin action)."""
    with _get_db() as conn:
        conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))


def toggle_pin(ann_id: int) -> None:
    """Flip the pinned flag on an announcement."""
    with _get_db() as conn:
        conn.execute(
            "UPDATE announcements SET pinned = CASE WHEN pinned=0 THEN 1 ELSE 0 END WHERE id = ?",
            (ann_id,),
        )


# ---------------------------------------------------------------------------
# Bookmark helpers
# ---------------------------------------------------------------------------


def get_bookmarks() -> list[dict]:
    """Return all bookmarks, ordered newest first."""
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookmarks ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_bookmark(title: str, url: str, icon: str = '🌐') -> int:
    """Insert a new bookmark and return its id."""
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO bookmarks (title, url, icon) VALUES (?, ?, ?)",
            (title.strip(), url.strip(), icon.strip() or '🌐'),
        )
        return cur.lastrowid


def delete_bookmark(bookmark_id: int) -> None:
    """Delete a bookmark by id."""
    with _get_db() as conn:
        conn.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def get_events() -> list[dict]:
    """Return upcoming events, ordered by date."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.title, e.event_date, e.end_date, e.author_id, u.username as author
            FROM events e
            LEFT JOIN users u ON u.id = e.author_id
            WHERE e.event_date >= date('now', '-1 day')
            ORDER BY e.event_date ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_events() -> list[dict]:
    """Return all events."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.title, e.event_date, e.end_date, e.author_id, u.username as author
            FROM events e
            LEFT JOIN users u ON u.id = e.author_id
            ORDER BY e.event_date ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_event(event_id: int) -> dict | None:
    """Return a single event by id."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?",
            (event_id,)
        ).fetchone()
    return dict(row) if row else None


def add_event(author_id: int, title: str, event_date: str, end_date: str = None) -> int:
    """Insert a new event and return its id."""
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO events (author_id, title, event_date, end_date) VALUES (?, ?, ?, ?)",
            (author_id, title.strip(), event_date.strip(), end_date.strip() if end_date else None),
        )
        return cur.lastrowid


def delete_event(event_id: int) -> None:
    """Delete an event by id."""
    with _get_db() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


# ---------------------------------------------------------------------------
# Shopping List helpers
# ---------------------------------------------------------------------------

def get_shopping_items() -> list[dict]:
    """Return all shopping list items."""
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.item_name AS content,
                s.created_at,
                u.username AS author
            FROM shopping_items s
            LEFT JOIN users u ON u.id = s.added_by
            ORDER BY s.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]

def add_shopping_item(item_name: str, added_by: int) -> int:
    """Insert a new shopping item."""
    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO shopping_items (item_name, added_by) VALUES (?, ?)",
            (item_name.strip(), added_by),
        )
        return cur.lastrowid

def delete_shopping_item(item_id: int) -> None:
    """Delete a shopping item by id."""
    with _get_db() as conn:
        conn.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))

