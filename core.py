"""Shared helpers for the Slack PFP updater and its web dashboard.

Handles config/state persistence, Last.fm lookups, Slack updates, and the
overlay/holiday rendering system used to build the profile image.
"""
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")  # bundled default art (pfp, frame, overlays)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
EMOJI_CACHE_PATH = os.path.join(BASE_DIR, "emoji_cache.json")

LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

# Keyless cover-art fallback providers (Last.fm is missing art for many albums).
DEEZER_ALBUM_SEARCH = "https://api.deezer.com/search/album"
DEEZER_TRACK_SEARCH = "https://api.deezer.com/search/track"
ITUNES_SEARCH = "https://itunes.apple.com/search"
# Last.fm serves this md5 "star" image when an album has no cover — treat as missing.
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"

# Cachet: a cache/proxy for Hack Club Slack profile pictures and custom emojis.
CACHET_BASE = "https://cachet.dunkirk.sh"

# Candidate locations for a color emoji font (used to render emoji overlays).
EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
]


# --------------------------------------------------------------------------- #
# Config / state persistence
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "poll_interval": 5,
    "restore_delay": 30,
    # Optional per-user base profile photo (relative path under uploads/). When
    # empty we fall back to the user's captured Slack avatar / bundled default.
    "base_photo": "",
    "frame_enabled": True,
    # Placement of the framed album-art badge on the profile photo. Editable
    # from the dashboard's "Edit / move" album editor. ``album_scale`` is the
    # badge width as a fraction of the photo width (matches overlay ``scale``);
    # anchor + offsets use the same system as overlay items.
    "album_scale": 0.36,
    "album_anchor": "bottom-left",
    "album_offset_x": 0.02,
    "album_offset_y": 0.02,
    "show_status": True,
    "status_format": "{song} - {artist} │ by slackpfp.christianwell.xyz",
    "status_emoji": ":musical_note:",
    "default_status": "",
    "default_status_emoji": "",
    "holidays": [
        {
            "id": "birthday", "name": "Birthday", "enabled": True,
            "start": "03-02", "end": "03-02",
            "status_text": "🎂 It's my birthday!", "status_emoji": ":birthday:",
            "items": [
                {"image": "assets/hat.png", "scale": 0.30, "anchor": "top-center",
                 "offset_x": 0.0, "offset_y": 0.02},
                {"image": "assets/cake.png", "scale": 0.30, "anchor": "bottom-right",
                 "offset_x": 0.02, "offset_y": 0.02},
            ],
        },
        {
            "id": "halloween", "name": "Halloween", "enabled": True,
            "start": "10-25", "end": "10-31",
            "status_text": "🎃 Spooky season", "status_emoji": ":jack_o_lantern:",
            "items": [
                {"image": "assets/halloween_hat.png", "scale": 0.38, "anchor": "top-center",
                 "offset_x": 0.0, "offset_y": -0.02},
                {"image": "assets/halloween_pumpkin.png", "scale": 0.30, "anchor": "bottom-right",
                 "offset_x": 0.02, "offset_y": 0.02},
            ],
        },
        {
            "id": "christmas", "name": "Christmas", "enabled": True,
            "start": "12-20", "end": "12-26",
            "status_text": "🎄 Merry Christmas!", "status_emoji": ":christmas_tree:",
            "items": [
                {"emoji": "🎅", "scale": 0.32, "anchor": "top-center",
                 "offset_x": 0.0, "offset_y": 0.0},
                {"emoji": "🎄", "scale": 0.26, "anchor": "bottom-right",
                 "offset_x": 0.02, "offset_y": 0.02},
            ],
        },
        {
            "id": "newyear", "name": "New Year", "enabled": True,
            "start": "12-31", "end": "01-01",
            "status_text": "🎆 Happy New Year!", "status_emoji": ":fireworks:",
            "items": [
                {"emoji": "🎉", "scale": 0.26, "anchor": "bottom-left",
                 "offset_x": 0.02, "offset_y": 0.02},
                {"emoji": "🎆", "scale": 0.26, "anchor": "top-right",
                 "offset_x": 0.02, "offset_y": 0.02},
            ],
        },
    ],
    "custom_overlays": [],
    "school": {
        "enabled": False,
        "days": [0, 1, 2, 3, 4],  # 0=Mon … 6=Sun
        "start": "08:30",         # HH:MM, server-local time
        "end": "15:30",
        "status_text": "",        # OOO status message while in school
        "status_emoji": "",
        "auto_reply": "",         # fallback OOO message when status_text is blank; not an auto-reply
        "music_behavior": "music_over",
        "holidays": [],           # school holidays: {start, end} full calendar days
    },
}

ANCHORS = [
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right",
]


def _atomic_write(path: str, data: str):
    """Write a file atomically so readers never see a partial file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_config() -> dict:
    """Load config.json, falling back to (and seeding) defaults."""
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # Backfill any missing top-level keys from defaults.
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, json.loads(json.dumps(val)))
    return cfg


def save_config(cfg: dict):
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))


STATUS_MAX = 100  # Slack profile status_text hard limit


def format_status(fmt: str, song: str, artist: str, album: str = "") -> str:
    """Render a status from the user format string, truncated to Slack's limit.

    Unknown placeholders fall back to a sane default so a bad format string
    never stops the status from updating.
    """
    try:
        text = fmt.format(song=song or "", artist=artist or "", album=album or "")
    except (KeyError, IndexError, ValueError):
        text = f"{song} - {artist}"
    if len(text) > STATUS_MAX:
        text = text[:STATUS_MAX - 1].rstrip() + "…"
    return text


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict):
    _atomic_write(STATE_PATH, json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #

def download_image(url: str) -> Image.Image:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content)).convert("RGBA")


def load_local_image(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")


DEFAULT_PFP_PATH = os.path.join(ASSETS_DIR, "pfp.png")


def fetch_slack_avatar_url(token: str) -> str:
    """Return the signed-in user's current Slack profile image URL.

    Prefers the highest-resolution custom upload. Returns "" on any failure
    (no token, network error, default gravatar with no custom image, etc.).
    """
    if not token:
        return ""
    try:
        resp = requests.get(
            "https://slack.com/api/users.profile.get",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        print(f"users.profile.get failed: {e}")
        return ""
    if not data.get("ok"):
        return ""
    p = data.get("profile", {})
    return (p.get("image_original") or p.get("image_512")
            or p.get("image_192") or "")


def ensure_base_pfp(avatar_url: str, uid: str = "") -> str:
    """Resolve a user's base profile photo to a local file path.

    Downloads and caches their own Slack avatar (keyed by URL so a new avatar
    re-downloads). Falls back to the bundled ``pfp.png`` when no avatar URL is
    known or the download fails.
    """
    if not avatar_url:
        return DEFAULT_PFP_PATH
    h = hashlib.sha1(avatar_url.encode()).hexdigest()[:12]
    cache = os.path.join(UPLOADS_DIR, f"base_{h}.png")
    if os.path.exists(cache):
        return cache
    try:
        img = download_image(avatar_url)
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        img.save(cache, format="PNG")
        return cache
    except Exception as e:
        print(f"base pfp download failed ({uid}): {e}")
        return DEFAULT_PFP_PATH


def resolve_base_path(cfg: dict, state: dict, uid: str = "") -> str:
    """Resolve the base profile photo to a local file path.

    Prefers a user-uploaded base photo (``cfg['base_photo']``) when present,
    otherwise falls back to their captured Slack avatar / bundled default.
    """
    bp = (cfg or {}).get("base_photo", "")
    if bp:
        path = os.path.join(BASE_DIR, bp)
        if os.path.exists(path):
            return path
    return ensure_base_pfp((state or {}).get("slack_avatar_url", ""), uid)


def fetch_slack_emoji(name: str, dest_dir: str, token: str = "") -> str:
    """Download a Slack custom emoji and save it as an overlay image.

    Resolves the image URL from the token's own workspace first (via
    emoji.list, requires emoji:read) and falls back to the Cachet proxy.
    Returns the saved overlay path ("uploads/<file>"). Raises on failure.
    """
    name = name.strip().strip(":")
    if not name:
        raise ValueError("empty emoji name")
    url = ""
    if token:
        url = get_workspace_emojis(token).get(name, "")
    if not url:
        url = f"{CACHET_BASE}/emojis/{name}/r"
    resp = requests.get(url, timeout=10, allow_redirects=True)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()
    ext = ".gif" if "gif" in ctype else ".webp" if "webp" in ctype else ".png"
    fname = f"slack_{name}{ext}"
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, fname), "wb") as f:
        f.write(resp.content)
    return f"uploads/{fname}"


# --------------------------------------------------------------------------- #
# Workspace custom emojis (Slack emoji.list, needs the emoji:read scope)
# --------------------------------------------------------------------------- #

def fetch_workspace_emojis(token: str) -> dict:
    """Return {name: image_url} of the token workspace's custom emojis.

    Requires the Slack token to have the ``emoji:read`` scope. Aliases are
    resolved to their target image URL.
    """
    resp = requests.get(
        "https://slack.com/api/emoji.list",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "emoji.list failed"))
    raw = data.get("emoji", {})
    resolved = {}
    for name, url in raw.items():
        if isinstance(url, str) and url.startswith("alias:"):
            resolved[name] = raw.get(url.split(":", 1)[1], "")
        else:
            resolved[name] = url
    return {k: v for k, v in resolved.items() if v}


def load_emoji_cache() -> dict:
    try:
        with open(EMOJI_CACHE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"updated_at": 0, "emoji": {}}


def save_emoji_cache(emoji: dict):
    _atomic_write(EMOJI_CACHE_PATH,
                  json.dumps({"updated_at": time.time(), "emoji": emoji}, ensure_ascii=False))


def get_workspace_emojis(token: str, max_age: int = 86400, force: bool = False) -> dict:
    """Cached accessor for workspace emojis; refetches when stale or forced.

    Falls back to the last cached copy if the API call fails (e.g. missing scope).
    """
    cache = load_emoji_cache()
    fresh = cache.get("emoji") and (time.time() - cache.get("updated_at", 0)) < max_age
    if fresh and not force:
        return cache["emoji"]
    if not token:
        return cache.get("emoji", {})
    try:
        emoji = fetch_workspace_emojis(token)
        save_emoji_cache(emoji)
        return emoji
    except Exception as e:
        print(f"emoji.list fetch failed: {e}")
        return cache.get("emoji", {})


def save_overlay_upload(stream, dest_dir: str, basename: str, max_size: int = 512) -> str:
    """Validate + re-encode an uploaded overlay to a small optimized PNG.

    Opening with Pillow validates that the bytes are a real image (rejects
    junk/abuse). Overlays are only ever composited at ≤512px, so we never store
    anything larger — keeping per-user disk use tiny. Returns the saved filename.
    """
    img = Image.open(stream)
    img.load()
    img = img.convert("RGBA")
    if img.width > max_size or img.height > max_size:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    os.makedirs(dest_dir, exist_ok=True)
    fname = f"{basename}.png"
    img.save(os.path.join(dest_dir, fname), format="PNG", optimize=True)
    return fname


SLACK_PHOTO_MAX_BYTES = 512 * 1024  # Slack users.setPhoto hard limit


def prepare_image_for_slack(img: Image.Image, max_size: int = 512) -> BytesIO:
    """Resize/encode the profile photo to fit under Slack's 512KB limit.

    PNG is tried first (crisp for simple/flat images), but a 512px photographic
    image easily blows past 512KB as a lossless PNG. Profile photos render
    opaque, so we fall back to JPEG (flattened on white) at decreasing quality,
    then shrink dimensions, until the result fits — this always returns an
    uploadable image instead of failing.
    """
    if img.width > max_size or img.height > max_size:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

    # 1) PNG (RGBA then RGB) — best for flat/simple graphics.
    for mode in ("RGBA", "RGB"):
        out = BytesIO()
        img.convert(mode).save(out, format="PNG", optimize=True)
        if out.getbuffer().nbytes <= SLACK_PHOTO_MAX_BYTES:
            out.seek(0)
            return out

    # 2) JPEG fallback for photographic images (no transparency needed).
    flat = Image.new("RGB", img.size, (255, 255, 255))
    rgba = img.convert("RGBA")
    flat.paste(rgba, (0, 0), rgba)
    work = flat
    while True:
        for quality in (90, 80, 70, 60, 50):
            out = BytesIO()
            work.save(out, format="JPEG", quality=quality, optimize=True)
            if out.getbuffer().nbytes <= SLACK_PHOTO_MAX_BYTES:
                out.seek(0)
                return out
        # Still too big even at low quality: halve the dimensions and retry.
        if min(work.size) <= 64:
            out.seek(0)
            return out  # give Slack our smallest attempt rather than failing
        work = work.resize((work.width // 2, work.height // 2), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- #
# Overlay system
# --------------------------------------------------------------------------- #

_emoji_font_cache: dict = {}


def _find_emoji_font() -> str | None:
    for path in EMOJI_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def render_emoji(emoji: str, px: int = 109) -> Image.Image:
    """Render a color emoji to a tightly-cropped RGBA image."""
    font_path = _find_emoji_font()
    if not font_path:
        raise RuntimeError("No color emoji font installed (fonts-noto-color-emoji)")
    font = _emoji_font_cache.get(px)
    if font is None:
        font = ImageFont.truetype(font_path, px)
        _emoji_font_cache[px] = font
    canvas = Image.new("RGBA", (px * 2, px * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((px // 2, px // 2), emoji, font=font, embedded_color=True)
    bbox = canvas.getbbox()
    return canvas.crop(bbox) if bbox else canvas


def _item_image(item: dict) -> Image.Image:
    """Resolve an overlay item to an RGBA image (emoji or file)."""
    emoji = item.get("emoji")
    if emoji:
        return render_emoji(emoji)
    name = item.get("image", "")
    candidates = [
        os.path.join(BASE_DIR, name),
        # Bundled art lives in assets/; also resolves legacy configs that stored
        # bare filenames (e.g. "hat.png") before assets/ existed.
        os.path.join(ASSETS_DIR, os.path.basename(name)),
        os.path.join(UPLOADS_DIR, os.path.basename(name)),
    ]
    for path in candidates:
        if os.path.exists(path):
            return load_local_image(path)
    raise FileNotFoundError(f"Overlay image not found: {name}")


def _anchor_pos(base_size, ov_size, anchor: str, ox: float, oy: float):
    bw, bh = base_size
    ow, oh = ov_size
    dx, dy = int(bw * ox), int(bh * oy)
    if "left" in anchor:
        x = dx
    elif "right" in anchor:
        x = bw - ow - dx
    else:
        x = (bw - ow) // 2 + dx
    if "top" in anchor:
        y = dy
    elif "bottom" in anchor:
        y = bh - oh - dy
    else:
        y = (bh - oh) // 2 + dy
    return x, y


def apply_overlay_item(img: Image.Image, item: dict) -> Image.Image:
    """Paste a single overlay item onto img according to its placement."""
    ov = _item_image(item)
    scale = float(item.get("scale", 0.25))
    w = max(1, int(img.width * scale))
    h = max(1, int(ov.height * (w / ov.width)))
    ov = ov.resize((w, h), Image.Resampling.LANCZOS)
    pos = _anchor_pos(img.size, (w, h), item.get("anchor", "bottom-right"),
                      float(item.get("offset_x", 0.02)), float(item.get("offset_y", 0.02)))
    out = img.copy()
    out.paste(ov, pos, ov)
    return out


def _date_in_range(today: datetime, start: str, end: str) -> bool:
    s = tuple(int(p) for p in start.split("-"))
    e = tuple(int(p) for p in end.split("-"))
    t = (today.month, today.day)
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e  # wraps over the new year


def active_holidays(cfg: dict, today: datetime | None = None) -> list:
    today = today or datetime.now()
    return [h for h in cfg.get("holidays", [])
            if h.get("enabled") and _date_in_range(today, h["start"], h["end"])]


# --------------------------------------------------------------------------- #
# School mode (OOO while you're in class)
# --------------------------------------------------------------------------- #

SCHOOL_BEHAVIORS = ("music_over", "music_school", "school_music", "listening")


def _hhmm_minutes(value) -> int | None:
    """'HH:MM' -> minutes since midnight, or None when unparseable."""
    try:
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def active_school_holidays(cfg: dict, now: datetime | None = None) -> list:
    """School holidays covering ``now`` (full calendar days, start→end inclusive)."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    return [h for h in (cfg.get("school") or {}).get("holidays", [])
            if h.get("start") and h.get("end") and h.get("start") <= today <= h.get("end")]


def is_school_time(cfg: dict, now: datetime | None = None) -> bool:
    """True while school OOO applies: enabled, a school day, inside the time
    window, and not on a school holiday. Server-local time, like holidays."""
    now = now or datetime.now()
    school = cfg.get("school") or {}
    if not school.get("enabled"):
        return False
    if active_school_holidays(cfg, now):
        return False
    start = _hhmm_minutes(school.get("start"))
    end = _hhmm_minutes(school.get("end"))
    if start is None or end is None:
        return False
    cur = now.hour * 60 + now.minute
    wrap = start > end
    # For an overnight window, the after-midnight leg belongs to the day the
    # schedule started on (e.g. Mon-only 22:00–02:00 covers Mon 22:00 +
    # Tue 00:00–02:00, so Tue 01:00 matches the Monday schedule).
    if wrap and cur <= end:
        day = (now.weekday() - 1) % 7
    else:
        day = now.weekday()
    if day not in (school.get("days") or []):
        return False
    if wrap:
        return cur >= start or cur <= end
    return start <= cur <= end


def _limit_status(text: str) -> str:
    if len(text) > STATUS_MAX:
        return text[:STATUS_MAX - 1].rstrip() + "…"
    return text


def school_status(cfg: dict, playing: bool, song: str = "", artist: str = "",
                  album: str = "", now: datetime | None = None) -> tuple[str | None, str | None]:
    """Status + emoji while school OOO is active, else (None, None).

    ``music_behavior`` decides how a playing track mixes with the school message:
      - music_over:    music status wins while playing, school message otherwise
      - music_school:  "<music> · <school message>"
      - school_music:  "<school message> · <music>"
      - listening:     "<school message> - Listening to Music"
    """
    school = cfg.get("school") or {}
    if not is_school_time(cfg, now):
        return None, None
    text = (school.get("status_text") or school.get("auto_reply") or "").strip()
    emoji = school.get("status_emoji") or ""
    if not playing:
        return _limit_status(text), emoji
    fmt = cfg.get("status_format", "{song} - {artist}")
    music_emoji = cfg.get("status_emoji", ":musical_note:")
    music = format_status(fmt, song, artist, album)
    behavior = school.get("music_behavior", "music_over")
    if behavior == "music_over":
        return music, music_emoji or ""
    if behavior == "music_school":
        return _limit_status(" · ".join(p for p in (music, text) if p)), music_emoji or ""
    if behavior == "school_music":
        return _limit_status(" · ".join(p for p in (text, music) if p)), emoji
    if behavior == "listening":
        return _limit_status(f"{text} - Listening to Music" if text else "Listening to Music"), emoji
    return _limit_status(text), emoji


def build_base_image(cfg: dict, default_pfp_path: str, today: datetime | None = None) -> Image.Image:
    """Profile photo with all active holiday + custom overlays applied."""
    img = load_local_image(default_pfp_path)
    for holiday in active_holidays(cfg, today):
        for item in holiday.get("items", []):
            try:
                img = apply_overlay_item(img, item)
            except Exception as e:
                print(f"Overlay error ({holiday.get('id')}): {e}")
    for overlay in cfg.get("custom_overlays", []):
        if overlay.get("enabled"):
            try:
                img = apply_overlay_item(img, overlay)
            except Exception as e:
                print(f"Custom overlay error ({overlay.get('id')}): {e}")
    return img


def album_badge(album_img: Image.Image, frame_path: str, width: int) -> Image.Image:
    """Composite album art inside the frame as a square ``width``px RGBA badge."""
    width = max(8, int(width))
    border = max(2, round(width * 8 / 184))  # ~4.3% border, matches the old look
    inner = max(2, width - 2 * border)
    album_img = album_img.resize((inner, inner), Image.Resampling.LANCZOS)
    frame_img = Image.open(frame_path).convert("RGBA").resize(
        (width, width), Image.Resampling.LANCZOS)
    combined = Image.new("RGBA", (width, width), (255, 255, 255, 0))
    combined.paste(album_img, (border, border), album_img)
    combined.paste(frame_img, (0, 0), frame_img)
    return combined


def placeholder_album(size: int = 300) -> Image.Image:
    """A neutral vinyl-style stand-in album cover for the editor preview."""
    img = Image.new("RGBA", (size, size), (38, 42, 56, 255))
    draw = ImageDraw.Draw(img)
    m = size // 6
    draw.ellipse([m, m, size - m, size - m], fill=(90, 100, 122, 255))
    r = size // 12
    c = size // 2
    draw.ellipse([c - r, c - r, c + r, c + r], fill=(38, 42, 56, 255))
    return img


def album_placement(cfg: dict) -> tuple[float, str, float, float]:
    """Return (scale, anchor, offset_x, offset_y) for the album badge."""
    anchor = cfg.get("album_anchor", "bottom-left")
    if anchor not in ANCHORS:
        anchor = "bottom-left"
    return (
        float(cfg.get("album_scale", 0.36)),
        anchor,
        float(cfg.get("album_offset_x", 0.02)),
        float(cfg.get("album_offset_y", 0.02)),
    )


def create_profile_image(base_img: Image.Image, album_img: Image.Image,
                         frame_path: str, cfg: dict | None = None) -> Image.Image:
    """Overlay the framed album-art badge onto ``base_img`` per ``cfg`` placement."""
    scale, anchor, ox, oy = album_placement(cfg or {})
    width = max(8, int(base_img.width * scale))
    badge = album_badge(album_img, frame_path, width)
    pos = _anchor_pos(base_img.size, badge.size, anchor, ox, oy)
    result = base_img.copy()
    result.paste(badge, pos, badge)
    return result


# --------------------------------------------------------------------------- #
# Last.fm
# --------------------------------------------------------------------------- #

# In-memory cover-art cache (incl. negative results) keyed by artist/album/song.
_album_art_cache: dict[tuple, str] = {}


def best_lastfm_image(images: list) -> str:
    """Pick the largest usable Last.fm cover URL, skipping the star placeholder."""
    url = ""
    for image in images or []:
        text = image.get("#text") or ""
        if text and LASTFM_PLACEHOLDER not in text:
            url = text  # images are ordered small→large, keep the last good one
    return url


def _deezer_art(artist: str, album: str, song: str) -> str:
    try:
        if album:
            resp = requests.get(DEEZER_ALBUM_SEARCH,
                                params={"q": f'artist:"{artist}" album:"{album}"'}, timeout=8)
            data = resp.json().get("data", [])
            if data:
                return data[0].get("cover_xl") or data[0].get("cover_big") or ""
        # No album (or no album hit) — a track search still carries the album cover.
        q = f'artist:"{artist}" track:"{song}"' if song else f'artist:"{artist}"'
        resp = requests.get(DEEZER_TRACK_SEARCH, params={"q": q}, timeout=8)
        data = resp.json().get("data", [])
        if data:
            alb = data[0].get("album", {})
            return alb.get("cover_xl") or alb.get("cover_big") or ""
    except Exception as e:
        print(f"Deezer art lookup failed: {e}")
    return ""


def _itunes_art(artist: str, album: str, song: str) -> str:
    try:
        term = f"{artist} {album}".strip() if album else f"{artist} {song}".strip()
        entity = "album" if album else "song"
        resp = requests.get(ITUNES_SEARCH,
                            params={"term": term, "entity": entity, "limit": 1}, timeout=8)
        results = resp.json().get("results", [])
        if results:
            art = results[0].get("artworkUrl100", "")
            if art:  # bump the requested size; no-op if the token isn't present
                return art.replace("100x100bb", "600x600bb")
    except Exception as e:
        print(f"iTunes art lookup failed: {e}")
    return ""


def fetch_album_art(artist: str, album: str = "", song: str = "") -> str:
    """Find cover art from keyless providers (Deezer, then iTunes).

    Fallback for when Last.fm has no usable image. Results — including misses —
    are cached in memory by artist/album/song so we never look up the same
    track twice.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    song = (song or "").strip()
    if not artist or (not album and not song):
        return ""
    key = (artist.lower(), album.lower(), "" if album else song.lower())
    if key in _album_art_cache:
        return _album_art_cache[key]
    url = _deezer_art(artist, album, song) or _itunes_art(artist, album, song)
    _album_art_cache[key] = url
    return url


def validate_lastfm_user(api_key: str, username: str) -> dict:
    """Check a Last.fm username for onboarding.

    Returns {"status": ..., "track": <str or "">} where status is one of:
      - "ok"       username exists and recent scrobbles are public
      - "empty"    username exists but has no scrobbles yet
      - "private"  username exists but recent listening is hidden
      - "notfound" no such username
      - "error"    network/API failure
    """
    username = (username or "").strip()
    if not username:
        return {"status": "notfound", "track": ""}
    try:
        resp = requests.get(
            LASTFM_API_URL,
            params={
                "method": "user.getrecenttracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 1,
            },
            timeout=10,
        )
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            # 6 = invalid user; 17 = user has hidden their recent listening.
            code = data.get("error")
            if code == 6:
                return {"status": "notfound", "track": ""}
            if code == 17:
                return {"status": "private", "track": ""}
            return {"status": "error", "track": ""}
        tracks = data.get("recenttracks", {}).get("track", [])
        if not tracks:
            return {"status": "empty", "track": ""}
        t = tracks[0]
        artist = t.get("artist", {}).get("#text", "")
        song = t.get("name", "")
        album = t.get("album", {}).get("#text", "")
        album_art = best_lastfm_image(t.get("image", []))
        if not album_art:
            album_art = fetch_album_art(artist, album, song)
        nowplaying = t.get("@attr", {}).get("nowplaying") == "true"
        return {
            "status": "ok",
            "track": f"{song} — {artist}".strip(" —"),
            "song": song, "artist": artist, "album": album,
            "album_art": album_art, "nowplaying": nowplaying,
        }
    except Exception as e:
        print(f"Last.fm validate error: {e}")
        return {"status": "error", "track": ""}


def get_current_track(api_key: str, username: str) -> tuple:
    """Return (track_id, song, artist, album, album_art_url) for the now-playing track."""
    try:
        resp = requests.get(
            LASTFM_API_URL,
            params={
                "method": "user.getrecenttracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        tracks = resp.json().get("recenttracks", {}).get("track", [])
        if not tracks:
            return None, None, None, None, None
        track = tracks[0]
        if track.get("@attr", {}).get("nowplaying") != "true":
            return None, None, None, None, None
        song = track.get("name")
        artist = track.get("artist", {}).get("#text")
        album = track.get("album", {}).get("#text", "")
        album_art = best_lastfm_image(track.get("image", []))
        if not album_art:
            album_art = fetch_album_art(artist, album, song)
        return f"{artist} - {song}", song, artist, album, album_art or None
    except Exception as e:
        print(f"Last.fm API error: {e}")
    return None, None, None, None, None
