"""Token-authenticated bridge for CLI and self-hosted music players."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core
import db

MAX_BODY_BYTES = 64 * 1024
DEFAULT_TTL = 90
MAX_TTL = 3600
LINKS_PATH = os.getenv("PLAYER_LINKS_PATH", os.path.join(core.BASE_DIR, "player_links.json"))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_links(path: str | None = None) -> dict:
    try:
        with open(path or LINKS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_links(links: dict, path: str | None = None) -> None:
    dest = path or LINKS_PATH
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = f"{dest}.tmp"
    with open(tmp, "w") as f:
        json.dump(links, f, indent=2, sort_keys=True)
    os.replace(tmp, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def create_link(slack_user_id: str, label: str = "", path: str | None = None) -> str:
    uid = (slack_user_id or "").strip()
    if not uid:
        raise ValueError("Slack user id is required")
    token = secrets.token_urlsafe(32)
    links = load_links(path)
    links[uid] = {
        "token_hash": _token_hash(token),
        "label": (label or "").strip(),
        "created_at": int(time.time()),
    }
    save_links(links, path)
    return token


def revoke_link(slack_user_id: str, path: str | None = None) -> bool:
    links = load_links(path)
    removed = links.pop((slack_user_id or "").strip(), None) is not None
    if removed:
        save_links(links, path)
    return removed


def verify_token(slack_user_id: str, token: str, path: str | None = None) -> bool:
    expected = str(load_links(path).get((slack_user_id or "").strip(), {}).get("token_hash") or "")
    actual = _token_hash(token or "")
    return bool(expected) and hmac.compare_digest(expected, actual)


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    playing = bool(data.get("playing", True))
    try:
        ttl = max(5, min(MAX_TTL, int(data.get("ttl", DEFAULT_TTL))))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    now = time.time()
    payload = {
        "playing": playing,
        "song": _clean_text(data.get("song"), 300),
        "artist": _clean_text(data.get("artist"), 300),
        "album": _clean_text(data.get("album"), 300),
        "album_art": _clean_text(data.get("album_art"), 2048),
        "track_id": _clean_text(data.get("track_id"), 300),
        "client": _clean_text(data.get("client"), 100),
        "received_at": now,
        "expires_at": now + ttl,
    }
    if playing and (not payload["song"] or not payload["artist"]):
        raise ValueError("song and artist are required while playing")
    return payload


def publish(slack_user_id: str, data: dict) -> dict:
    uid = (slack_user_id or "").strip()
    if db.load_user(uid) is None:
        raise LookupError("unknown Slack user")
    payload = normalize_payload(data)
    state = db.load_runtime_state(uid)
    state["linked_player"] = payload
    db.save_runtime_state(uid, state)
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "SlackPFPPlayerLink/1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[player-link] {self.address_string()} {fmt % args}")

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._json(200, {"ok": True}) if self.path == "/healthz" else self._json(404, {"ok": False})

    def do_POST(self) -> None:
        prefix = "/v1/now-playing/"
        if not self.path.startswith(prefix):
            self._json(404, {"ok": False, "error": "not_found"})
            return
        uid = self.path[len(prefix):].split("?", 1)[0].strip()
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not verify_token(uid, token):
            self._json(401, {"ok": False, "error": "invalid_token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "invalid_body_size"})
            return
        try:
            payload = publish(uid, json.loads(self.rfile.read(length)))
        except LookupError as exc:
            self._json(404, {"ok": False, "error": str(exc)})
            return
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            print(f"[player-link] publish failed: {exc}")
            self._json(500, {"ok": False, "error": "internal_error"})
            return
        self._json(200, {"ok": True, "playing": payload["playing"], "expires_at": payload["expires_at"]})


def start_server() -> ThreadingHTTPServer | None:
    if os.getenv("LINK_PLAYER_ENABLED", "1") != "1":
        return None
    host = os.getenv("LINK_PLAYER_HOST", "127.0.0.1")
    port = int(os.getenv("LINK_PLAYER_PORT", "8092"))
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="player-link", daemon=True).start()
    print(f"Linked-player bridge listening on http://{host}:{port}")
    return server
