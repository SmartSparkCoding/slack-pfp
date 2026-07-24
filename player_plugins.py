"""Now-playing provider registry for Slack PFP.

Built-in providers keep Last.fm working and allow a linked CLI/self-hosted
player to push now-playing data. Third-party modules can be loaded with the
SLACK_PFP_PLAYER_PLUGINS environment variable.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import core


@dataclass(frozen=True)
class Track:
    """Normalized now-playing data returned by every provider."""

    track_id: str
    song: str
    artist: str
    album: str = ""
    album_art: str = ""


@dataclass(frozen=True)
class PollResult:
    """A provider response.

    track=None means the provider authoritatively reports nothing playing.
    Returning None from poll() means the provider is not configured, allowing
    auto mode to try the next provider.
    """

    source: str
    track: Track | None
    status: str = "ok"


class PlayerPlugin(Protocol):
    id: str
    priority: int

    def poll(self, user: Any, context: dict[str, Any]) -> PollResult | None:
        """Return a result, or None when this plugin does not apply."""


class Registry:
    def __init__(self) -> None:
        self._plugins: dict[str, PlayerPlugin] = {}

    def register(self, plugin: PlayerPlugin) -> None:
        plugin_id = str(getattr(plugin, "id", "")).strip().lower()
        if not plugin_id:
            raise ValueError("player plugin must define a non-empty id")
        if plugin_id == "auto":
            raise ValueError("'auto' is reserved by the player registry")
        self._plugins[plugin_id] = plugin

    def get(self, plugin_id: str) -> PlayerPlugin | None:
        return self._plugins.get((plugin_id or "").strip().lower())

    def plugins(self) -> list[PlayerPlugin]:
        return sorted(
            self._plugins.values(),
            key=lambda plugin: int(getattr(plugin, "priority", 100)),
        )

    def poll(self, user: Any, context: dict[str, Any] | None = None) -> PollResult | None:
        context = dict(context or {})
        selected = str((getattr(user, "config", {}) or {}).get("player_plugin", "auto"))
        selected = selected.strip().lower() or "auto"

        if selected != "auto":
            plugin = self.get(selected)
            if plugin is None:
                return PollResult(selected, None, "plugin_not_found")
            result = plugin.poll(user, context)
            return result or PollResult(selected, None, "not_configured")

        for plugin in self.plugins():
            result = plugin.poll(user, context)
            if result is not None:
                return result
        return None


class LinkedPlayerPlugin:
    """Read fresh payloads written by linked_player.py."""

    id = "linked"
    priority = 10

    def poll(self, user: Any, context: dict[str, Any]) -> PollResult | None:
        payload = (getattr(user, "state", {}) or {}).get("linked_player")
        if not isinstance(payload, dict):
            return None
        try:
            expires_at = float(payload.get("expires_at", 0))
        except (TypeError, ValueError):
            return None
        if expires_at <= time.time():
            return None
        if not payload.get("playing", True):
            return PollResult(self.id, None)

        song = str(payload.get("song") or "").strip()
        artist = str(payload.get("artist") or "").strip()
        if not song or not artist:
            return PollResult(self.id, None, "invalid_payload")
        album = str(payload.get("album") or "").strip()
        album_art = str(payload.get("album_art") or "").strip()
        track_id = str(payload.get("track_id") or "").strip()
        if not track_id:
            raw = "\0".join((artist.lower(), song.lower(), album.lower()))
            track_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
        return PollResult(self.id, Track(track_id, song, artist, album, album_art))


class LastFMPlugin:
    """Compatibility provider for the existing Last.fm implementation."""

    id = "lastfm"
    priority = 100

    def poll(self, user: Any, context: dict[str, Any]) -> PollResult | None:
        username = str(getattr(user, "lastfm_username", "") or "").strip()
        api_key = str(context.get("lastfm_api_key") or "").strip()
        if not username or not api_key:
            return None
        track_id, song, artist, album, album_art = core.get_current_track(api_key, username)
        if not track_id or not song or not artist:
            return PollResult(self.id, None)
        return PollResult(
            self.id,
            Track(str(track_id), str(song), str(artist), str(album or ""), str(album_art or "")),
        )


def _load_external_plugins(registry: Registry) -> None:
    modules = [
        module.strip()
        for module in os.getenv("SLACK_PFP_PLAYER_PLUGINS", "").split(",")
        if module.strip()
    ]
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
            register = getattr(module, "register", None)
            if callable(register):
                register(registry)
                continue
            plugin = getattr(module, "PLUGIN", None)
            if plugin is None:
                raise RuntimeError("module exports neither register(registry) nor PLUGIN")
            registry.register(plugin)
        except Exception as exc:
            print(f"Player plugin load failed ({module_name}): {exc}")


REGISTRY = Registry()
REGISTRY.register(LinkedPlayerPlugin())
REGISTRY.register(LastFMPlugin())
_load_external_plugins(REGISTRY)


def poll_user(user: Any, lastfm_api_key: str = "") -> PollResult | None:
    return REGISTRY.poll(user, {"lastfm_api_key": lastfm_api_key})


def user_is_ready(user: Any, lastfm_api_key: str = "") -> bool:
    """Return whether an active user has at least one possible source."""
    selected = str((getattr(user, "config", {}) or {}).get("player_plugin", "auto"))
    selected = selected.strip().lower() or "auto"
    if selected in {"auto", "linked"}:
        return True
    if selected == "lastfm":
        return bool(getattr(user, "lastfm_username", "") and lastfm_api_key)
    return REGISTRY.get(selected) is not None
