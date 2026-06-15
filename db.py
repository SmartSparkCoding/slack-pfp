"""Per-user data model for the multi-tenant Slack PFP service.

Phase 1 first slice: load/save one signed-in Slack user without touching the
rendering pipeline. One SQLite row per user holds their team, encrypted Slack
token, Last.fm username, and per-user config/state JSON (today these still
mirror the single-user shapes from ``core.py``).

Slack tokens are encrypted at rest with Fernet; the key comes from the
``FERNET_KEY`` env var and is never stored in the database or the repo. Generate
one with ``python -m db keygen`` and put it in ``.env``.
"""
import json
import os
import re
import sqlite3
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

import core

DB_PATH = os.path.join(core.BASE_DIR, "users.db")

# Onboarding state machine (see goals.md "Onboarding state machine").
ONBOARDING_STATES = (
    "new",              # row created, nothing connected yet
    "slack_connected",  # Slack OAuth done, token stored
    "lastfm_set",       # Last.fm username validated
    "active",           # updater is driving this user's profile
    "paused",           # user paused updates
    "disconnected",     # token revoked / user disconnected
)

_local = threading.local()


# --------------------------------------------------------------------------- #
# RAM-backed runtime state (now-playing) — keeps the SD card alive
# --------------------------------------------------------------------------- #
# The worker would otherwise write a SQLite row for every user every poll cycle
# just to record the current track. That volatile state instead lives in tmpfs
# (RAM): the web app reads it for the live dashboard, and it is simply
# repopulated by the worker after a reboot. Durable data (tokens, config,
# Last.fm username, onboarding) still lives in ``users.db`` on disk.

def _runtime_state_dir() -> str:
    """A writable RAM-backed dir (tmpfs) for volatile per-user state."""
    for base in ("/dev/shm", tempfile.gettempdir()):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            path = os.path.join(base, "slack-pfp-state")
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except OSError:
                continue
    # Last resort (no tmpfs available): a local dir. Still avoids the DB churn.
    path = os.path.join(core.BASE_DIR, ".state-cache")
    os.makedirs(path, exist_ok=True)
    return path


RUNTIME_STATE_DIR = _runtime_state_dir()


def _state_file(slack_user_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", slack_user_id or "_")
    return os.path.join(RUNTIME_STATE_DIR, f"{safe}.json")


def load_runtime_state(slack_user_id: str) -> dict:
    """Read a user's volatile now-playing state from RAM (``{}`` if none)."""
    try:
        with open(_state_file(slack_user_id)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_runtime_state(slack_user_id: str, state: dict) -> None:
    """Write a user's volatile state to RAM atomically (never touches the DB)."""
    path = _state_file(slack_user_id)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f"runtime state write failed ({slack_user_id}): {e}")


def delete_runtime_state(slack_user_id: str) -> None:
    try:
        os.remove(_state_file(slack_user_id))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #

def _fernet() -> Fernet:
    key = os.getenv("FERNET_KEY", "")
    if not key:
        raise RuntimeError(
            "FERNET_KEY is not set. Generate one with `python -m db keygen` "
            "and add it to your .env (never commit it)."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"FERNET_KEY is invalid: {e}") from e


def encrypt_token(token: str) -> bytes:
    """Encrypt a Slack token for storage. Empty/None -> b'' (no token)."""
    if not token:
        return b""
    return _fernet().encrypt(token.encode())


def decrypt_token(blob: bytes) -> str:
    """Decrypt a stored Slack token. Empty/garbage -> '' (treated as no token)."""
    if not blob:
        return ""
    try:
        return _fernet().decrypt(blob).decode()
    except (InvalidToken, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    slack_user_id   TEXT PRIMARY KEY,
    team_id         TEXT NOT NULL,
    slack_token_enc BLOB,
    lastfm_username TEXT,
    config_json     TEXT,
    state_json      TEXT,
    onboarding      TEXT NOT NULL DEFAULT 'new',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def _connect(path: str | None = None) -> sqlite3.Connection:
    """Return a per-thread SQLite connection with the schema applied."""
    db_path = path or DB_PATH
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == db_path:
        return conn
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _local.conn = conn
    _local.path = db_path
    return conn


def init_db(path: str | None = None) -> None:
    """Create the database/schema if it does not exist."""
    _connect(path).commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# User record
# --------------------------------------------------------------------------- #

def _default_config() -> dict:
    return deepcopy(core.DEFAULT_CONFIG)


@dataclass
class User:
    """One signed-in Slack user and their per-user settings/state."""

    slack_user_id: str
    team_id: str
    slack_token: str = ""               # plaintext in memory, encrypted at rest
    lastfm_username: str = ""
    config: dict = field(default_factory=_default_config)
    state: dict = field(default_factory=dict)
    onboarding: str = "new"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        cfg = _default_config()
        try:
            stored = json.loads(row["config_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored = {}
        # Backfill any missing top-level keys from defaults (matches core.load_config).
        cfg.update(stored)
        for key, val in core.DEFAULT_CONFIG.items():
            cfg.setdefault(key, deepcopy(val))
        try:
            state = json.loads(row["state_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            state = {}
        return cls(
            slack_user_id=row["slack_user_id"],
            team_id=row["team_id"],
            slack_token=decrypt_token(row["slack_token_enc"]),
            lastfm_username=row["lastfm_username"] or "",
            config=cfg,
            state=state,
            onboarding=row["onboarding"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def load_user(slack_user_id: str, path: str | None = None) -> User | None:
    """Load one user by Slack user id, or None if not found."""
    cur = _connect(path).execute(
        "SELECT * FROM users WHERE slack_user_id = ?", (slack_user_id,)
    )
    row = cur.fetchone()
    return User.from_row(row) if row else None


def save_user(user: User, path: str | None = None) -> User:
    """Insert or update a user (upsert on slack_user_id). Returns the user."""
    if user.onboarding not in ONBOARDING_STATES:
        raise ValueError(f"invalid onboarding state: {user.onboarding!r}")
    conn = _connect(path)
    now = _now()
    if not user.created_at:
        user.created_at = now
    user.updated_at = now
    conn.execute(
        """
        INSERT INTO users (
            slack_user_id, team_id, slack_token_enc, lastfm_username,
            config_json, state_json, onboarding, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slack_user_id) DO UPDATE SET
            team_id=excluded.team_id,
            slack_token_enc=excluded.slack_token_enc,
            lastfm_username=excluded.lastfm_username,
            config_json=excluded.config_json,
            state_json=excluded.state_json,
            onboarding=excluded.onboarding,
            updated_at=excluded.updated_at
        """,
        (
            user.slack_user_id,
            user.team_id,
            encrypt_token(user.slack_token),
            user.lastfm_username,
            json.dumps(user.config, ensure_ascii=False),
            json.dumps(user.state, ensure_ascii=False),
            user.onboarding,
            user.created_at,
            user.updated_at,
        ),
    )
    conn.commit()
    return user


def delete_user(slack_user_id: str, path: str | None = None) -> bool:
    """Delete a user and their data. Returns True if a row was removed."""
    conn = _connect(path)
    cur = conn.execute("DELETE FROM users WHERE slack_user_id = ?", (slack_user_id,))
    conn.commit()
    delete_runtime_state(slack_user_id)
    return cur.rowcount > 0


def all_users(path: str | None = None) -> list[User]:
    """Load every user (for the shared worker loop in Phase 2)."""
    cur = _connect(path).execute("SELECT * FROM users ORDER BY created_at")
    return [User.from_row(row) for row in cur.fetchall()]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "keygen":
        print(Fernet.generate_key().decode())
    else:
        print("usage: python -m db keygen   # print a new FERNET_KEY")
