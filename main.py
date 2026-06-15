"""Multi-user Slack profile updater loop.

A single shared loop iterates every active user, throttled to stay under
Last.fm's ~5 requests/second limit. For each user it polls their now-playing
track (using the shared app key + their public username) and updates their Slack
photo (framed album art) and status via their own per-user token.

Per-user settings/state live in SQLite (``db.py``). Repeated Slack auth failures
disable a user (token revoked) until they re-connect via the dashboard.
"""
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import core
import db

FRAME_PATH = os.path.join(core.ASSETS_DIR, "frame.png")
DEFAULT_PFP = os.path.join(core.ASSETS_DIR, "pfp.png")

# Last.fm rate limiting: stay comfortably under ~5 req/s on the shared key.
LASTFM_MAX_RPS = 4.0
_MIN_LASTFM_INTERVAL = 1.0 / LASTFM_MAX_RPS

# How long (seconds) one full sweep over all users should take, minimum.
CYCLE_SECONDS = 30

# Disable a user after this many consecutive auth failures.
AUTH_FAIL_LIMIT = 3
AUTH_ERRORS = {"invalid_auth", "token_revoked", "account_inactive",
               "not_authed", "no_permission", "missing_scope"}

# In-memory per-user runtime tracking (reset on restart).
_runtime: dict[str, dict] = {}


def _rt(uid: str) -> dict:
    return _runtime.setdefault(uid, {
        "last_status": None, "last_photo_key": None,
        "last_config_sig": None, "not_playing_since": None, "auth_fails": 0,
    })


def status_for(cfg, playing, song, artist, album, default_status):
    holidays = core.active_holidays(cfg)
    for h in holidays:
        if h.get("status_text"):
            return h["status_text"], h.get("status_emoji", "")
    if playing and cfg.get("show_status", True):
        fmt = cfg.get("status_format", "{song} - {artist}")
        return core.format_status(fmt, song, artist, album), cfg.get("status_emoji", ":musical_note:")
    text = cfg.get("default_status") or default_status or ""
    return text, cfg.get("default_status_emoji", "")


class AuthFailure(Exception):
    """Raised when a Slack call fails because the user's token is bad."""


def _slack_call(fn):
    """Run a Slack API call, distinguishing auth failures from transient ones."""
    try:
        fn()
        return True
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err in AUTH_ERRORS:
            raise AuthFailure(err)
        print(f"Slack error: {err}")
        return False
    except Exception as e:
        print(f"Slack call error: {e}")
        return False


def set_status(client, text, emoji):
    return _slack_call(lambda: client.users_profile_set(
        profile={"status_text": text, "status_emoji": emoji}))


def set_photo(client, user, album_url):
    cfg = user.config
    base_path = core.resolve_base_path(cfg, user.state, user.slack_user_id)
    base_img = core.build_base_image(cfg, base_path)
    if album_url and cfg.get("frame_enabled", True):
        album_img = core.download_image(album_url)
        final_img = core.create_profile_image(base_img, album_img, FRAME_PATH, cfg)
    else:
        final_img = base_img
    return _slack_call(lambda: client.users_setPhoto(
        image=core.prepare_image_for_slack(final_img)))


def write_state(user, playing, song, artist, album, album_art, lastfm_status=""):
    user.state.update({
        "playing": playing,
        "song": song,
        "artist": artist,
        "album": album,
        "album_art": album_art,
        "lastfm_status": lastfm_status,
        "active_holidays": [h["id"] for h in core.active_holidays(user.config)],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    # Volatile now-playing state goes to RAM (tmpfs), not the SD card.
    db.save_runtime_state(user.slack_user_id, user.state)


def process_user(user: db.User, api_key: str):
    """Run one polling step for a single user. Returns False if disabled."""
    rt = _rt(user.slack_user_id)
    cfg = user.config
    client = WebClient(token=user.slack_token)

    # Force a re-render if the user changed their config.
    config_sig = repr(cfg)
    if config_sig != rt["last_config_sig"]:
        rt["last_status"] = None
        rt["last_photo_key"] = None
        rt["last_config_sig"] = config_sig

    track_id, song, artist, album, album_art = core.get_current_track(
        api_key, user.lastfm_username)
    playing = bool(track_id and song and artist)

    try:
        if playing:
            rt["not_playing_since"] = None
            write_state(user, True, song, artist, album, album_art, "ok")
            text, emoji = status_for(cfg, True, song, artist, album, "")
            if (text, emoji) != rt["last_status"] and set_status(client, text, emoji):
                rt["last_status"] = (text, emoji)
            photo_key = ("track", track_id)
            if photo_key != rt["last_photo_key"]:
                print(f"[{user.slack_user_id}] Now playing: {song} by {artist}")
                if set_photo(client, user, album_art):
                    rt["last_photo_key"] = photo_key
        else:
            write_state(user, False, None, None, None, None, "ok")
            if rt["not_playing_since"] is None:
                rt["not_playing_since"] = time.time()
            elif time.time() - rt["not_playing_since"] >= cfg.get("restore_delay", 30):
                text, emoji = status_for(cfg, False, None, None, None, "")
                if (text, emoji) != rt["last_status"] and set_status(client, text, emoji):
                    rt["last_status"] = (text, emoji)
                if rt["last_photo_key"] != ("default",):
                    if set_photo(client, user, None):
                        rt["last_photo_key"] = ("default",)
        rt["auth_fails"] = 0
        return True
    except AuthFailure as e:
        rt["auth_fails"] += 1
        print(f"[{user.slack_user_id}] Slack auth failure ({e}) "
              f"{rt['auth_fails']}/{AUTH_FAIL_LIMIT}")
        if rt["auth_fails"] >= AUTH_FAIL_LIMIT:
            user.onboarding = "disconnected"
            user.state["lastfm_status"] = "slack_disconnected"
            db.save_user(user)
            db.delete_runtime_state(user.slack_user_id)  # drop stale now-playing
            print(f"[{user.slack_user_id}] disabled (token revoked)")
        return False


def main():
    load_dotenv(os.path.join(core.BASE_DIR, ".env"))
    db.init_db()
    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        raise SystemExit("LASTFM_API_KEY must be set in .env")

    print("Starting multi-user Slack profile updater...")
    last_lastfm_call = 0.0

    while True:
        cycle_start = time.time()
        users = [u for u in db.all_users()
                 if u.onboarding == "active" and u.slack_token and u.lastfm_username]
        if not users:
            time.sleep(min(CYCLE_SECONDS, 10))
            continue

        for user in users:
            # Throttle Last.fm requests to stay under the per-key rate limit.
            wait = _MIN_LASTFM_INTERVAL - (time.time() - last_lastfm_call)
            if wait > 0:
                time.sleep(wait)
            last_lastfm_call = time.time()
            try:
                process_user(user, api_key)
            except Exception as e:
                print(f"[{user.slack_user_id}] processing error: {e}")

        elapsed = time.time() - cycle_start
        headroom = LASTFM_MAX_RPS * elapsed - len(users)
        print(f"Cycle: {len(users)} users in {elapsed:.1f}s "
              f"(Last.fm headroom ~{headroom:.0f} req)")
        if elapsed < CYCLE_SECONDS:
            time.sleep(CYCLE_SECONDS - elapsed)


if __name__ == "__main__":
    main()
