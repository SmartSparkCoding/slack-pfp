"""Multi-tenant web app for the Slack PFP updater.

Anyone in the Hack Club workspace signs in with Slack, sets a Last.fm username,
and the worker keeps their Slack photo/status in sync with their now-playing
track. Per-user settings/state live in SQLite (``db.py``); the rendering
pipeline in ``core.py`` is unchanged.
"""
import os
import shutil
import uuid
from functools import wraps
from io import BytesIO

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.utils import secure_filename

import core
import db
import slack_oauth

load_dotenv(os.path.join(core.BASE_DIR, ".env"))

# Optional workspace bot token, used only as a fallback for emoji lookups when
# the signed-in user's token is unavailable.
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
DEFAULT_PFP = os.path.join(core.ASSETS_DIR, "pfp.png")
FRAME_PATH = os.path.join(core.ASSETS_DIR, "frame.png")
ALLOWED_EXT = {".png", ".gif", ".webp", ".jpg", ".jpeg"}

# Per-user overlay quotas (storage decision: Option 1, our disk + quotas).
MAX_OVERLAYS_PER_USER = 20
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB of stored overlays per user

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB per upload
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
)
os.makedirs(core.UPLOADS_DIR, exist_ok=True)
db.init_db()


# --------------------------------------------------------------------------- #
# Auth / session
# --------------------------------------------------------------------------- #

def current_user() -> db.User | None:
    if "user" in g:
        return g.user
    uid = session.get("slack_user_id")
    user = db.load_user(uid) if uid else None
    if user is not None:
        # Overlay the live now-playing state the worker keeps in RAM (tmpfs).
        user.state.update(db.load_runtime_state(uid))
    g.user = user
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            session.clear()
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def save(user: db.User):
    db.save_user(user)
    g.user = user


# --------------------------------------------------------------------------- #
# Per-user uploads + quota
# --------------------------------------------------------------------------- #

def _user_uploads_dir(uid: str) -> str:
    path = os.path.join(core.UPLOADS_DIR, secure_filename(uid))
    os.makedirs(path, exist_ok=True)
    return path


def _user_upload_bytes(uid: str) -> int:
    d = os.path.join(core.UPLOADS_DIR, secure_filename(uid))
    if not os.path.isdir(d):
        return 0
    return sum(os.path.getsize(os.path.join(d, f))
               for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))


def _overlay_count(user: db.User) -> int:
    n = len(user.config.get("custom_overlays", []))
    for h in user.config.get("holidays", []):
        n += len(h.get("items", []))
    return n


def _quota_ok(user: db.User) -> tuple[bool, str]:
    if _overlay_count(user) >= MAX_OVERLAYS_PER_USER:
        return False, f"Overlay limit reached ({MAX_OVERLAYS_PER_USER})."
    if _user_upload_bytes(user.slack_user_id) >= MAX_UPLOAD_BYTES:
        return False, "Upload storage limit reached (10 MB)."
    return True, ""


def _save_upload(user: db.User, stream, filename: str, basename: str):
    """Validate/re-encode an upload into the user's dir. Returns rel path or False."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return False
    try:
        fname = core.save_overlay_upload(stream, _user_uploads_dir(user.slack_user_id), basename)
    except Exception:
        return False
    return f"uploads/{secure_filename(user.slack_user_id)}/{fname}"


def _delete_item_image(uid: str, item: dict):
    img = item.get("image", "")
    prefix = f"uploads/{secure_filename(uid)}/"
    if img.startswith(prefix):
        path = os.path.join(core.BASE_DIR, img)
        if os.path.exists(path):
            os.remove(path)


def _emoji_token(user: db.User) -> str:
    return user.slack_token or SLACK_BOT_TOKEN


# --------------------------------------------------------------------------- #
# Landing + OAuth
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    user = current_user()
    if user:
        return redirect(url_for("dashboard"))
    return render_template("landing.html",
                           configured=slack_oauth.is_configured(),
                           team=slack_oauth.allowed_team_id())


@app.route("/login")
def login():
    if not slack_oauth.is_configured():
        flash("Slack sign-in is not configured on this server.")
        return redirect(url_for("index"))
    state = slack_oauth.new_state()
    session["oauth_state"] = state
    return redirect(slack_oauth.authorize_url(state))


@app.route("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        flash("Slack sign-in was cancelled.")
        return redirect(url_for("index"))
    state = request.args.get("state", "")
    if not state or state != session.pop("oauth_state", None):
        flash("Sign-in expired or was tampered with. Please try again.")
        return redirect(url_for("index"))
    code = request.args.get("code", "")
    try:
        result = slack_oauth.exchange_code(code)
    except slack_oauth.OAuthError as e:
        if str(e) == "wrong_workspace":
            return render_template("wrong_workspace.html",
                                   team=slack_oauth.allowed_team_id()), 403
        flash("Slack sign-in failed. Please try again.")
        return redirect(url_for("index"))

    uid = result["slack_user_id"]
    user = db.load_user(uid)
    if user is None:
        user = db.User(slack_user_id=uid, team_id=result["team_id"],
                       slack_token=result["user_token"], onboarding="slack_connected")
    else:
        # Re-auth: refresh token; reactivate a previously disconnected account.
        user.slack_token = result["user_token"]
        user.team_id = result["team_id"]
        if user.onboarding == "disconnected":
            user.onboarding = "active" if user.lastfm_username else "slack_connected"
    # Capture the user's real Slack avatar once, before we ever overwrite their
    # photo, so their profile is built on their own PFP (not the bundled default).
    if not user.state.get("slack_avatar_url"):
        avatar_url = core.fetch_slack_avatar_url(result["user_token"])
        if avatar_url:
            user.state["slack_avatar_url"] = avatar_url
    db.save_user(user)
    session.clear()
    session["slack_user_id"] = uid
    session.permanent = True

    if not user.lastfm_username:
        return redirect(url_for("onboarding"))
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Onboarding (Last.fm step)
# --------------------------------------------------------------------------- #

def _birthday_mmdd(user: db.User) -> str:
    for h in user.config.get("holidays", []):
        if h.get("id") == "birthday":
            return h.get("start", "")
    return ""


def _set_birthday(user: db.User, mmdd: str) -> bool:
    """Update the birthday holiday's dates from a 'MM-DD' string. Returns True if set."""
    parts = (mmdd or "").split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    mm, dd = int(parts[0]), int(parts[1])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return False
    norm = f"{mm:02d}-{dd:02d}"
    for h in user.config.get("holidays", []):
        if h.get("id") == "birthday":
            h["start"] = h["end"] = norm
            h["enabled"] = True
            return True
    return False


@app.route("/onboarding")
@login_required
def onboarding():
    user = current_user()
    return render_template("onboarding.html", user=user,
                           birthday=_birthday_mmdd(user))


@app.route("/api/lastfm/validate")
@login_required
def lastfm_validate():
    if not LASTFM_API_KEY:
        return jsonify({"status": "error", "track": "", "detail": "no api key"})
    username = request.args.get("username", "")
    return jsonify(core.validate_lastfm_user(LASTFM_API_KEY, username))


@app.route("/onboarding/preview.png")
@login_required
def onboarding_preview():
    """Render the user's real profile photo (their avatar + frame + the album
    art of the typed Last.fm user's recent track) so they see it before going
    live. Falls back to just the base photo when nothing can be fetched."""
    user = current_user()
    username = request.args.get("username", "").strip()
    cfg = user.config
    base_path = core.resolve_base_path(cfg, user.state, user.slack_user_id)
    base = core.build_base_image(cfg, base_path)
    if username and LASTFM_API_KEY and cfg.get("frame_enabled", True):
        info = core.validate_lastfm_user(LASTFM_API_KEY, username)
        art = info.get("album_art")
        if art:
            try:
                album = core.download_image(art)
                base = core.create_profile_image(base, album, FRAME_PATH, cfg)
            except Exception:
                pass
    out = BytesIO()
    base.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/onboarding/lastfm", methods=["POST"])
@login_required
def onboarding_lastfm():
    user = current_user()
    username = request.form.get("lastfm_username", "").strip()
    if not username:
        flash("Enter your Last.fm username.")
        return redirect(url_for("onboarding"))
    user.lastfm_username = username
    # Birthday (from a date input, YYYY-MM-DD) configures the birthday overlay.
    bday = request.form.get("birthday", "").strip()
    if bday:
        _set_birthday(user, bday[5:] if len(bday) >= 10 else bday)
    go_live = request.form.get("go_live") == "on"
    user.onboarding = "active" if go_live else "lastfm_set"
    save(user)
    flash("You're live! Your Slack profile will update shortly." if go_live
          else "Last.fm username saved.")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #
# Dashboard + settings
# --------------------------------------------------------------------------- #

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if not user.lastfm_username:
        return redirect(url_for("onboarding"))
    return render_template("dashboard.html", user=user, cfg=user.config,
                           state=user.state, anchors=core.ANCHORS)


@app.route("/api/settings", methods=["POST"])
@login_required
def update_settings():
    user = current_user()
    cfg = user.config
    cfg["frame_enabled"] = request.form.get("frame_enabled") == "on"
    cfg["show_status"] = request.form.get("show_status") == "on"
    cfg["status_format"] = request.form.get("status_format", "{song} - {artist}").strip() or "{song} - {artist}"
    cfg["status_emoji"] = request.form.get("status_emoji", ":musical_note:").strip()
    cfg["default_status"] = request.form.get("default_status", "").strip()
    cfg["default_status_emoji"] = request.form.get("default_status_emoji", "").strip()
    try:
        cfg["poll_interval"] = max(2, int(request.form.get("poll_interval", 5)))
        cfg["restore_delay"] = max(0, int(request.form.get("restore_delay", 30)))
    except ValueError:
        pass
    save(user)
    flash("Settings saved")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #
# Base profile photo (user-supplied; overrides their Slack avatar)
# --------------------------------------------------------------------------- #

@app.route("/api/photo", methods=["POST"])
@login_required
def update_photo():
    user = current_user()
    upload = request.files.get("photo")
    if not upload or not upload.filename:
        flash("Choose an image to upload.")
        return redirect(url_for("dashboard"))
    saved = _save_upload(user, upload.stream, upload.filename, "base")
    if saved is False:
        flash("Unsupported or invalid image (use PNG/GIF/WebP/JPEG)")
        return redirect(url_for("dashboard"))
    user.config["base_photo"] = saved
    save(user)
    flash("Profile photo updated")
    return redirect(url_for("dashboard"))


@app.route("/api/photo/reset", methods=["POST"])
@login_required
def reset_photo():
    user = current_user()
    bp = user.config.get("base_photo", "")
    if bp:
        _delete_item_image(user.slack_user_id, {"image": bp})
    user.config["base_photo"] = ""
    save(user)
    flash("Profile photo reset to your Slack avatar")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

@app.route("/account")
@login_required
def account():
    user = current_user()
    return render_template("account.html", user=user)


@app.route("/account/pause", methods=["POST"])
@login_required
def account_pause():
    user = current_user()
    if user.onboarding == "active":
        user.onboarding = "paused"
        flash("Updates paused.")
    elif user.onboarding == "paused":
        user.onboarding = "active" if user.lastfm_username else "lastfm_set"
        flash("Updates resumed.")
    save(user)
    return redirect(url_for("account"))


@app.route("/account/disconnect", methods=["POST"])
@login_required
def account_disconnect():
    user = current_user()
    user.slack_token = ""
    user.onboarding = "disconnected"
    save(user)
    db.delete_runtime_state(user.slack_user_id)
    session.clear()
    flash("Disconnected from Slack.")
    return redirect(url_for("index"))


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    user = current_user()
    uid = user.slack_user_id
    d = os.path.join(core.UPLOADS_DIR, secure_filename(uid))
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    db.delete_user(uid)
    session.clear()
    flash("Your data has been deleted.")
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #

@app.route("/api/holiday/<hid>/toggle", methods=["POST"])
@login_required
def toggle_holiday(hid):
    user = current_user()
    for h in user.config.get("holidays", []):
        if h["id"] == hid:
            h["enabled"] = not h.get("enabled")
    save(user)
    return redirect(url_for("dashboard"))


@app.route("/api/holiday/add", methods=["POST"])
@login_required
def add_holiday():
    user = current_user()
    name = request.form.get("name", "").strip() or "New Holiday"
    holiday = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "enabled": True,
        "start": request.form.get("start", "01-01").strip() or "01-01",
        "end": request.form.get("end", "01-01").strip() or "01-01",
        "status_text": request.form.get("status_text", "").strip(),
        "status_emoji": request.form.get("status_emoji", "").strip(),
        "items": [],
    }
    user.config.setdefault("holidays", []).append(holiday)
    save(user)
    flash(f"Holiday '{name}' created")
    return redirect(url_for("editor", scene=holiday["id"]))


@app.route("/api/holiday/<hid>/edit", methods=["POST"])
@login_required
def edit_holiday(hid):
    user = current_user()
    for h in user.config.get("holidays", []):
        if h["id"] == hid:
            h["name"] = request.form.get("name", h["name"]).strip() or h["name"]
            h["start"] = request.form.get("start", h["start"]).strip() or h["start"]
            h["end"] = request.form.get("end", h["end"]).strip() or h["end"]
            h["status_text"] = request.form.get("status_text", "").strip()
            h["status_emoji"] = request.form.get("status_emoji", "").strip()
            break
    save(user)
    flash("Holiday updated")
    return redirect(request.form.get("next") or url_for("dashboard"))


@app.route("/api/holiday/<hid>/delete", methods=["POST"])
@login_required
def delete_holiday(hid):
    user = current_user()
    remaining = []
    for h in user.config.get("holidays", []):
        if h["id"] == hid:
            for item in h.get("items", []):
                _delete_item_image(user.slack_user_id, item)
            continue
        remaining.append(h)
    user.config["holidays"] = remaining
    save(user)
    flash("Holiday deleted")
    return redirect(url_for("dashboard"))


@app.route("/api/holiday/<hid>/item/add", methods=["POST"])
@login_required
def add_holiday_item(hid):
    user = current_user()
    ok, msg = _quota_ok(user)
    if not ok:
        flash(msg)
        return redirect(url_for("editor", scene=hid))
    emoji = request.form.get("emoji", "").strip()
    slack_emoji = request.form.get("slack_emoji", "").strip()
    upload = request.files.get("image")
    item = {"scale": 0.30, "anchor": "top-center", "offset_x": 0.0, "offset_y": 0.02}
    if upload and upload.filename:
        saved = _save_upload(user, upload.stream, upload.filename, f"{hid}_{uuid.uuid4().hex[:6]}")
        if saved is False:
            flash("Unsupported or invalid image (use PNG/GIF/WebP/JPEG)")
            return redirect(url_for("editor", scene=hid))
        item["image"] = saved
    elif slack_emoji:
        try:
            item["image"] = core.fetch_slack_emoji(slack_emoji, _user_uploads_dir(user.slack_user_id), _emoji_token(user))
            item["image"] = _normalize_emoji_path(user, item["image"])
            item["label"] = f":{slack_emoji.strip(':')}:"
        except Exception:
            flash(f"Couldn't fetch Hack Club emoji ':{slack_emoji.strip(':')}:'")
            return redirect(url_for("editor", scene=hid))
    elif emoji:
        item["emoji"] = emoji
    else:
        flash("Provide an emoji or upload an image")
        return redirect(url_for("editor", scene=hid))
    for h in user.config.get("holidays", []):
        if h["id"] == hid:
            h.setdefault("items", []).append(item)
            break
    save(user)
    return redirect(url_for("editor", scene=hid))


@app.route("/api/holiday/<hid>/item/<int:idx>/delete", methods=["POST"])
@login_required
def delete_holiday_item(hid, idx):
    user = current_user()
    for h in user.config.get("holidays", []):
        if h["id"] == hid:
            items = h.get("items", [])
            if 0 <= idx < len(items):
                _delete_item_image(user.slack_user_id, items[idx])
                items.pop(idx)
            break
    save(user)
    return redirect(url_for("editor", scene=hid))


# --------------------------------------------------------------------------- #
# Custom overlays
# --------------------------------------------------------------------------- #

def _normalize_emoji_path(user: db.User, saved: str) -> str:
    """fetch_slack_emoji returns 'uploads/<file>'; rewrite to the user dir form."""
    base = os.path.basename(saved)
    return f"uploads/{secure_filename(user.slack_user_id)}/{base}"


@app.route("/api/overlay/add", methods=["POST"])
@login_required
def add_overlay():
    user = current_user()
    ok, msg = _quota_ok(user)
    if not ok:
        flash(msg)
        return redirect(url_for("dashboard"))
    overlay = {
        "id": uuid.uuid4().hex[:8],
        "enabled": True,
        "scale": _clamp_float(request.form.get("scale", "0.25"), 0.02, 1.0, 0.25),
        "anchor": request.form.get("anchor", "bottom-right"),
        "offset_x": _clamp_float(request.form.get("offset_x", "0.02"), 0.0, 1.0, 0.02),
        "offset_y": _clamp_float(request.form.get("offset_y", "0.02"), 0.0, 1.0, 0.02),
    }
    if overlay["anchor"] not in core.ANCHORS:
        overlay["anchor"] = "bottom-right"

    emoji = request.form.get("emoji", "").strip()
    slack_emoji = request.form.get("slack_emoji", "").strip()
    upload = request.files.get("image")
    if upload and upload.filename:
        saved = _save_upload(user, upload.stream, upload.filename, overlay["id"])
        if saved is False:
            flash("Unsupported or invalid image (use PNG/GIF/WebP/JPEG)")
            return redirect(url_for("dashboard"))
        overlay["image"] = saved
        overlay["label"] = upload.filename
    elif slack_emoji:
        try:
            saved = core.fetch_slack_emoji(slack_emoji, _user_uploads_dir(user.slack_user_id), _emoji_token(user))
            overlay["image"] = _normalize_emoji_path(user, saved)
            overlay["label"] = f":{slack_emoji.strip(':')}:"
        except Exception:
            flash(f"Couldn't fetch Hack Club emoji ':{slack_emoji.strip(':')}:'")
            return redirect(url_for("dashboard"))
    elif emoji:
        overlay["emoji"] = emoji
        overlay["label"] = emoji
    else:
        flash("Provide an emoji or upload an image")
        return redirect(url_for("dashboard"))

    user.config.setdefault("custom_overlays", []).append(overlay)
    save(user)
    flash("Overlay added")
    return redirect(url_for("dashboard"))


@app.route("/api/overlay/<oid>/toggle", methods=["POST"])
@login_required
def toggle_overlay(oid):
    user = current_user()
    for o in user.config.get("custom_overlays", []):
        if o["id"] == oid:
            o["enabled"] = not o.get("enabled")
    save(user)
    return redirect(url_for("dashboard"))


@app.route("/api/overlay/<oid>/delete", methods=["POST"])
@login_required
def delete_overlay(oid):
    user = current_user()
    remaining = []
    for o in user.config.get("custom_overlays", []):
        if o["id"] == oid:
            _delete_item_image(user.slack_user_id, o)
            continue
        remaining.append(o)
    user.config["custom_overlays"] = remaining
    save(user)
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #
# Emoji search (workspace-wide; single Hack Club workspace)
# --------------------------------------------------------------------------- #

@app.route("/api/emojis")
@login_required
def list_emojis():
    user = current_user()
    cache = core.load_emoji_cache()
    emoji = cache.get("emoji", {})
    if not emoji:
        emoji = core.get_workspace_emojis(_emoji_token(user))
        cache = core.load_emoji_cache()
    meta = {"updated_at": cache.get("updated_at", 0), "count": len(emoji)}

    name = request.args.get("name", "").strip().strip(":")
    if name:
        return {"name": name, "url": emoji.get(name, "")}

    q = request.args.get("q", "").strip().lower().strip(":")
    if q:
        matches = sorted(n for n in emoji if q in n)[:40]
        meta["matches"] = [{"name": n, "url": emoji[n]} for n in matches]
    return meta


@app.route("/api/emojis/refresh", methods=["POST"])
@login_required
def refresh_emojis():
    user = current_user()
    emoji = core.get_workspace_emojis(_emoji_token(user), force=True)
    if not emoji:
        flash("Couldn't load emojis — check the token has the emoji:read scope")
    else:
        flash(f"Loaded {len(emoji)} workspace emojis")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #
# Image rendering (preview / editor)
# --------------------------------------------------------------------------- #

@app.route("/preview.png")
@login_required
def preview():
    user = current_user()
    cfg, state = user.config, user.state
    base_path = core.resolve_base_path(cfg, state, user.slack_user_id)
    base = core.build_base_image(cfg, base_path)
    album_url = state.get("album_art")
    if album_url and cfg.get("frame_enabled", True):
        try:
            album = core.download_image(album_url)
            base = core.create_profile_image(base, album, FRAME_PATH, cfg)
        except Exception:
            pass
    out = BytesIO()
    base.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/base.png")
@login_required
def base_png():
    user = current_user()
    base_path = core.resolve_base_path(user.config, user.state, user.slack_user_id)
    img = core.load_local_image(base_path)
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/album-badge.png")
@login_required
def album_badge_png():
    """Framed album-art badge used as the draggable node in the album editor."""
    user = current_user()
    album_url = user.state.get("album_art")
    album = None
    if album_url:
        try:
            album = core.download_image(album_url)
        except Exception:
            album = None
    if album is None:
        album = core.placeholder_album()
    badge = core.album_badge(album, FRAME_PATH, 256)
    out = BytesIO()
    badge.save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/render-overlay")
@login_required
def render_overlay():
    item = {}
    emoji = request.args.get("emoji", "")
    image = request.args.get("image", "")
    if emoji:
        item["emoji"] = emoji
    elif image:
        # Only allow the signed-in user's own uploads or bundled assets.
        uid = secure_filename(current_user().slack_user_id)
        if image.startswith("uploads/") and not image.startswith(f"uploads/{uid}/"):
            abort(403)
        item["image"] = image
    else:
        abort(400)
    try:
        ov = core._item_image(item)
    except Exception:
        abort(404)
    out = BytesIO()
    ov.save(out, format="PNG")
    out.seek(0)
    return Response(out.read(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/editor/<scene>")
@login_required
def editor(scene):
    user = current_user()
    cfg = user.config
    holiday = None
    if scene == "album":
        view_items = [{
            "index": 0,
            "oid": "album",
            "label": "Album cover",
            "emoji": "",
            "image": "",
            "src": url_for("album_badge_png"),
            "scale": float(cfg.get("album_scale", 0.36)),
            "anchor": cfg.get("album_anchor", "bottom-left"),
            "offset_x": float(cfg.get("album_offset_x", 0.02)),
            "offset_y": float(cfg.get("album_offset_y", 0.02)),
        }]
        return render_template("editor.html", scene="album", title="Album cover",
                               items=view_items, holiday_start="", holiday_end="",
                               holiday_status="", holiday_emoji="")
    if scene == "custom":
        items = cfg.get("custom_overlays", [])
        title = "Custom overlays"
    else:
        holiday = next((h for h in cfg.get("holidays", []) if h["id"] == scene), None)
        if not holiday:
            abort(404)
        items = holiday.get("items", [])
        title = holiday["name"]
    view_items = []
    for i, it in enumerate(items):
        view_items.append({
            "index": i,
            "oid": it.get("id", ""),
            "label": it.get("label") or it.get("emoji") or os.path.basename(it.get("image", "")),
            "emoji": it.get("emoji", ""),
            "image": it.get("image", ""),
            "scale": float(it.get("scale", 0.25)),
            "anchor": it.get("anchor", "bottom-right"),
            "offset_x": float(it.get("offset_x", 0.02)),
            "offset_y": float(it.get("offset_y", 0.02)),
        })
    return render_template(
        "editor.html", scene=scene, title=title, items=view_items,
        holiday_start=(holiday or {}).get("start", ""),
        holiday_end=(holiday or {}).get("end", ""),
        holiday_status=(holiday or {}).get("status_text", ""),
        holiday_emoji=(holiday or {}).get("status_emoji", ""),
    )


@app.route("/api/layout/<scene>", methods=["POST"])
@login_required
def save_layout(scene):
    user = current_user()
    data = request.get_json(silent=True) or {}
    cfg = user.config
    if scene == "album":
        items = data.get("items", [])
        if items:
            u = items[0]
            cfg["album_anchor"] = "top-left"
            cfg["album_offset_x"] = _clamp_float(u.get("offset_x"), 0.0, 1.0, cfg.get("album_offset_x", 0.02))
            cfg["album_offset_y"] = _clamp_float(u.get("offset_y"), 0.0, 1.0, cfg.get("album_offset_y", 0.02))
            cfg["album_scale"] = _clamp_float(u.get("scale"), 0.08, 0.9, cfg.get("album_scale", 0.36))
        save(user)
        return {"ok": True}
    updates = {int(u["index"]): u for u in data.get("items", [])}
    if scene == "custom":
        items = cfg.get("custom_overlays", [])
    else:
        holiday = next((h for h in cfg.get("holidays", []) if h["id"] == scene), None)
        if not holiday:
            return {"ok": False, "error": "scene not found"}, 404
        items = holiday.get("items", [])
    for i, it in enumerate(items):
        if i in updates:
            u = updates[i]
            it["anchor"] = "top-left"
            it["offset_x"] = _clamp_float(u.get("offset_x"), 0.0, 1.0, it.get("offset_x", 0.0))
            it["offset_y"] = _clamp_float(u.get("offset_y"), 0.0, 1.0, it.get("offset_y", 0.0))
            it["scale"] = _clamp_float(u.get("scale"), 0.02, 1.0, it.get("scale", 0.25))
    save(user)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Health / ops
# --------------------------------------------------------------------------- #

@app.route("/healthz")
def healthz():
    try:
        users = db.all_users()
        return {
            "ok": True,
            "users": len(users),
            "active": sum(1 for u in users if u.onboarding == "active"),
            "oauth_configured": slack_oauth.is_configured(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def _clamp_float(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="127.0.0.1", port=int(os.getenv("WEB_PORT", "8091")))
