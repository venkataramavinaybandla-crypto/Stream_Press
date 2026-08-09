"""STREAM PRESS — Universal Personal-Use Media Downloader (local service backend).

Strictly a private, non-commercial, local-first utility. Binds to 127.0.0.1
only, performs no telemetry, and talks only to the media sources you paste
into it. Reads YouTube, Instagram, X, Facebook, TikTok, LinkedIn, Pinterest,
Reddit, Vimeo, Twitch and 1,000+ more sources — videos, audio, image posts,
thumbnails, profile pictures and channel cover art. Downloads are intended
for personal offline viewing, archiving and educational research under Fair
Use guidelines. Re-distribution of copyrighted material is the sole
responsibility of the end user.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_DIR = os.path.join(BASE_DIR, "client")
HISTORY_FILE = os.path.join(BASE_DIR, "server", "history.json")
STREAK_FILE = os.path.join(BASE_DIR, "server", "state.json")


def _default_downloads_dir() -> str:
    """The PC's real Downloads folder; falls back to the repo only if none exists."""
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, "Downloads"), home):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(BASE_DIR, "downloads")


# Finished files land straight in the user's Downloads folder (never in the repo).
DOWNLOADS_DIR = os.environ.get("YTMAX_DOWNLOAD_DIR") or _default_downloads_dir()

# In-progress work is staged in the OS temp dir, so the repo stays clean.
STAGING_ROOT = os.path.join(tempfile.gettempdir(), "stream-press-staging")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(STAGING_ROOT, exist_ok=True)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

APP_NAME = "STREAM PRESS"
APP_VERSION = "3.0.0"

# Healthy-service limits (override via env if you really need to)
MAX_WORKERS = max(1, int(os.environ.get("YTMAX_WORKERS", "2")))
TASK_CAP = max(10, int(os.environ.get("YTMAX_TASK_CAP", "60")))
RATE_LIMIT_CEIL_MBPS = 20

ALLOWED_AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEMP_SUFFIXES = (".part", ".ytdl", ".temp")

# A single well-formed browser UA helps CDNs (Instagram, Pinterest, X…) serve us.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# YouTube thumbnail ladder (img.youtube.com), highest first.
YT_THUMB_SIZES = [
    ("maxresdefault", "Max res (1280×720)", 1280, 720),
    ("hq720", "HD 720p (1280×720)", 1280, 720),
    ("sddefault", "SD (640×480)", 640, 480),
    ("hqdefault", "HQ (480×360)", 480, 360),
    ("mqdefault", "MQ (320×180)", 320, 180),
    ("default", "Default (120×90)", 120, 90),
]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="STREAM PRESS — Universal Personal-Use Media Downloader API",
    description="Local, non-commercial private utility for personal offline viewing and educational research under Fair Use guidelines.",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# State (guarded by _lock; workers write via helper functions)
# ---------------------------------------------------------------------------
_lock = threading.RLock()
tasks: Dict[str, Dict[str, Any]] = {}
history: List[Dict[str, Any]] = []
stats: Dict[str, Any] = {"downloads": 0, "total_bytes": 0, "started_at": time.time()}

# Dedicated download pool: request threads are never blocked by heavy work.
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ytdl-worker")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _load_persisted_state() -> None:
    global history
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        history = data.get("history", []) or []
        saved = data.get("stats", {}) or {}
        stats["downloads"] = int(saved.get("downloads", 0))
        stats["total_bytes"] = int(saved.get("total_bytes", 0))
    except Exception:
        history = []


def _persist_state() -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"history": history[:100], "stats": stats}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Streak tracking (consecutive days with at least one completed download)
# ---------------------------------------------------------------------------
def _load_streak_state() -> Dict[str, Any]:
    try:
        with open(STREAK_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "streak": int(data.get("streak", 0)),
            "best": int(data.get("best", 0)),
            "last_date": str(data.get("last_date", "") or ""),
        }
    except Exception:
        return {"streak": 0, "best": 0, "last_date": ""}


streak_state: Dict[str, Any] = _load_streak_state()


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def _record_download_day() -> Dict[str, Any]:
    """Count a completed non-incognito download toward the daily streak."""
    global streak_state
    today = _today_str()
    with _lock:
        if streak_state.get("last_date") == today:
            pass  # already counted today
        elif streak_state.get("last_date") == _yesterday_str():
            streak_state["streak"] = int(streak_state.get("streak", 0)) + 1
        else:
            streak_state["streak"] = 1
        streak_state["best"] = max(
            int(streak_state.get("best", 0)),
            int(streak_state.get("streak", 0)),
        )
        streak_state["last_date"] = today
        try:
            os.makedirs(os.path.dirname(STREAK_FILE), exist_ok=True)
            tmp = STREAK_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(streak_state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, STREAK_FILE)
        except Exception:
            pass
        return dict(streak_state)


def _cleanup_interrupted_downloads() -> None:
    """Remove leftover staging temp files from previous sessions (never touches the user's Downloads folder)."""
    try:
        for entry in os.listdir(STAGING_ROOT):
            task_dir = os.path.join(STAGING_ROOT, entry)
            if not os.path.isdir(task_dir):
                continue
            for name in os.listdir(task_dir):
                if name.endswith(TEMP_SUFFIXES):
                    try:
                        os.remove(os.path.join(task_dir, name))
                    except OSError:
                        pass
            try:
                if not os.listdir(task_dir):
                    os.rmdir(task_dir)
            except OSError:
                pass
    except OSError:
        pass


_load_persisted_state()
_cleanup_interrupted_downloads()


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def format_speed(bytes_per_sec: Optional[float]) -> str:
    if not bytes_per_sec:
        return "0 KB/s"
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{int(bytes_per_sec)} B/s"


def format_eta(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(b: Optional[float]) -> str:
    if not b:
        return "0 MB"
    if b >= 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{int(b)} B"


def normalize_media_url(raw: str) -> Optional[str]:
    """Accept any http(s) link; the extractor engine decides if it's supported."""
    url = raw.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return url


def _clean_error(exc: Exception) -> str:
    msg = str(exc).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    lowered = msg.lower()
    if "is not a valid url" in lowered or "unsupported url" in lowered:
        return "Unsupported or invalid link — the press could not read it. Paste a direct media page URL."
    if "video unavailable" in lowered or "not available" in lowered:
        return "Media unavailable — it may be private, region-locked, or removed."
    if "private video" in lowered or "private post" in lowered:
        return "This media is private and cannot be downloaded."
    if "empty media response" in lowered or "no media found" in lowered or "no media" in lowered:
        return "No downloadable media found here — the post may need a logged-in browser session (see Machine Settings → Browser cookies) or may have been removed."
    if "no video could be found" in lowered:
        return "This X post has no extractable video stream. If it is a photo post, the images are offered automatically — otherwise videos may need a logged-in browser session (Machine Settings → Browser cookies)."
    if "http error 404" in lowered:
        return "That file no longer exists at this address (404) — try a fresh link."
    if "http error 403" in lowered or "forbidden" in lowered:
        return "The source refused the request (403) — this media likely needs a logged-in browser session (Machine Settings → Browser cookies)."
    if "http error 429" in lowered or "too many requests" in lowered:
        return "The source is rate-limiting right now — wait a few minutes and try again."
    if "connection aborted" in lowered or "connection reset" in lowered or "timed out" in lowered or "connection refused" in lowered:
        return "The source refused the connection right now — try again in a moment."
    if any(k in lowered for k in ("login required", "log in", "sign in", "authentication", "requires a login", "logged-in")):
        return "This link requires a login — enable the Browser cookies session in Machine Settings to use your logged-in account."
    if any(k in lowered for k in ("drm", "playready", "widevine", "fairplay")):
        return "DRM-protected stream — copy-protected content cannot be saved."
    if "confirm you're not a bot" in lowered or "bot check" in lowered:
        return "The source is running a bot check right now. Wait a few minutes and try again."
    if "unsupported" in lowered:
        return "This site or link type is not supported yet. Try a direct media page URL."
    if len(msg) > 420:
        msg = msg[:420] + "…"
    return msg


def _is_embed_error(msg: str) -> bool:
    lowered = msg.lower()
    return any(h in lowered for h in ("thumbnail", "cover art", "artwork", "postprocess", " embed "))


def _active_download_count() -> int:
    with _lock:
        return sum(
            1 for t in tasks.values()
            if t.get("status") in ("initializing", "queued", "downloading", "processing")
        )


def _prune_tasks() -> None:
    """Keep only the most recent TASK_CAP task entries (memory hygiene)."""
    with _lock:
        if len(tasks) <= TASK_CAP:
            return
        active = {tid for tid, t in tasks.items() if t.get("status") not in ("completed", "failed")}
        to_prune = [tid for tid in tasks if tid not in active]
        # drop oldest finished entries first
        for tid in to_prune[: (len(tasks) - TASK_CAP)]:
            tasks.pop(tid, None)


def _find_output_file(task_dir: str):
    """Largest non-image, non-temp file produced in the task directory (video/audio tasks)."""
    best_path, best_size = None, -1
    try:
        names = os.listdir(task_dir)
    except OSError:
        return None, -1
    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTENSIONS or name.endswith(TEMP_SUFFIXES):
            continue
        path = os.path.join(task_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > best_size:
            best_path, best_size = path, size
    return best_path, best_size


def _find_output_files(task_dir: str) -> List[str]:
    """All finished (non-temp) files produced in the task directory, biggest first."""
    out: List[str] = []
    try:
        names = os.listdir(task_dir)
    except OSError:
        return out
    for name in names:
        if name.endswith(TEMP_SUFFIXES):
            continue
        path = os.path.join(task_dir, name)
        try:
            if os.path.getsize(path) > 0:
                out.append(path)
        except OSError:
            continue
    out.sort(key=os.path.getsize, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Universal media helpers — images, thumbnails, avatars, cover art
# ---------------------------------------------------------------------------
def _origin_of(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except ValueError:
        return ""


def _sanitize_component(name: Optional[str], fallback: str = "download") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return name[:80] or fallback


def _ext_of(url: str) -> str:
    base = re.sub(r"[?#].*$", "", url).lower()
    m = re.search(r"\.(jpe?g|png|webp|gif|avif|bmp|ico|heic|mp4|webm|mov|mkv)$", base)
    return m.group(1) if m else "jpg"


def _http_get(url: str, timeout: float = 12.0, max_bytes: int = 900_000) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return None


def _probe_url(url: str, timeout: float = 6.0) -> bool:
    """Cheap reachability check (HEAD, then GET fallback)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status < 400
        except Exception:
            return False


def _og_image_url(html_text: Optional[str]) -> Optional[str]:
    if not html_text:
        return None
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html_text)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text)
    if not m:
        return None
    url = html.unescape(m.group(1)).strip()
    return url if url.startswith("http") else None


def _og_title(html_text: Optional[str]) -> Optional[str]:
    if not html_text:
        return None
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html_text)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html_text)
    if not m:
        return None
    return html.unescape(m.group(1)).strip()


def _upgrade_avatar_url(url: str) -> str:
    """Ask profile-art CDNs for the largest available size (best-effort)."""
    host = (urlparse(url).hostname or "").lower()
    if "pbs.twimg.com" in host:
        url = re.sub(r"_normal(\.\w+)$", r"_400x400\1", url)
        url = re.sub(r"_(\d+)x(\d+)(\.\w+)$", r"_400x400\3", url)
        url = re.sub(r"name=(\d+)x(\d+)", "name=1500x500", url)
    elif "media.licdn.com" in host:
        url = re.sub(r"shrink_(\d+)_(\d+)", "shrink_800_800", url)
        url = re.sub(r"w=(\d+)", "w=800", url)
    elif "yt3." in host or "yt3.googleusercontent.com" in host:
        url = re.sub(r"=s\d+", "=s0", url)
    elif "cdninstagram.com" in host or "fbcdn" in host:
        url = re.sub(r"stp=dst-jpg_s\d+x\d+", "stp=dst-jpg_s1080x1080", url)
        url = re.sub(r"/s\d+x\d+/", "/s1080x1080/", url)
    return url


def _cookies_opts(cookies_browser: Optional[str]) -> Dict[str, Any]:
    """Optional logged-in browser session for login-walled posts (IG, X, FB, LinkedIn…)."""
    if not cookies_browser:
        return {}
    return {"cookiesfrombrowser": (cookies_browser,)}


def _is_yt_channel_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return False
    path = urlparse(url).path
    return bool(
        re.match(r"^/@", path)
        or path.startswith("/channel/")
        or path.startswith("/user/")
        or path.startswith("/c/")
    )


def _is_yt_video_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return False
    path = urlparse(url).path
    return bool(path.startswith("/watch") or "/shorts/" in path or "/embed/" in path or host == "youtu.be")


def _is_profile_url(url: str, extractor_key: str = "") -> bool:
    """User/channel profile pages (their art is offered as downloads)."""
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    if extractor_key in ("YoutubeTab", "YoutubeChannel"):
        return True
    if "youtube.com" in host or "youtu.be" in host:
        return _is_yt_channel_url(url)
    if "instagram.com" in host:
        return bool(re.match(r"^/[^/]+/?$", path))
    if host in ("x.com", "twitter.com"):
        return bool(re.match(r"^/[^/]+/?$", path))
    if "linkedin.com" in host:
        return path.startswith("/in/") or path.startswith("/company/") or path.startswith("/school/")
    if "facebook.com" in host:
        return bool(re.match(r"^/[^/]+/?$", path))
    if "pinterest" in host:
        parts = [p for p in path.split("/") if p]
        return bool(parts and len(parts) <= 1 and parts[0] not in ("pin", "ideas", "search"))
    return False


def _post_image_fallback(url: str) -> Optional[Dict[str, Any]]:
    """X/Twitter image-only posts: yt-dlp only extracts videos, so scrape the
    photo URLs from the post page and offer those instead (best-effort)."""
    host = (urlparse(url).hostname or "").lower()
    if host not in ("x.com", "twitter.com"):
        return None
    page = _http_get(url)
    if not page:
        return None
    media_ids = sorted(set(re.findall(r"pbs\.twimg\.com/media/([A-Za-z0-9_\-]+)", page)))
    if not media_ids:
        return None
    media_ids = media_ids[:8]
    images: List[Dict[str, Any]] = []
    for i, media_id in enumerate(media_ids, start=1):
        images.append({
            "id": f"x-img-{i}",
            "label": f"Post image {i}" + (f" · {len(media_ids)} total" if len(media_ids) > 1 else ""),
            "kind": "post_image",
            "url": f"https://pbs.twimg.com/media/{media_id}?format=jpg&name=orig",
            "width": None,
            "height": None,
            "ext": "jpg",
        })
    return {
        "url": url,
        "title": "Post on X",
        "duration": 0,
        "duration_str": "Images",
        "thumbnail": images[0]["url"],
        "uploader": "X / Twitter",
        "view_count": 0,
        "view_count_str": "N/A",
        "is_live": False,
        "source": "X",
        "extractor_key": "Twitter",
        "highest_res_height": 0,
        "highest_res_label": "Post images",
        "media_kind": "images",
        "is_profile": False,
        "has_video": False,
        "has_audio": False,
        "video_formats": [],
        "audio_formats": [],
        "images": images,
        "image_count": len(images),
    }


def _profile_only_result(url: str, art: List[Dict[str, Any]], page: Optional[str]) -> Dict[str, Any]:
    """Minimal result for profile pages yt-dlp cannot extract (X, LinkedIn, IG…): just the avatar art."""
    title = _og_title(page) or "Profile page"
    return {
        "url": url,
        "title": title,
        "duration": 0,
        "duration_str": "Profile",
        "thumbnail": art[0]["url"] if art else "",
        "uploader": "Profile page",
        "view_count": 0,
        "view_count_str": "N/A",
        "is_live": False,
        "source": "PROFILE",
        "extractor_key": "Profile",
        "highest_res_height": 0,
        "highest_res_label": "Profile art",
        "media_kind": "profile",
        "is_profile": True,
        "has_video": False,
        "has_audio": False,
        "video_formats": [],
        "audio_formats": [],
        "images": art,
        "image_count": len(art),
    }


def _yt_video_id(url: str) -> Optional[str]:
    m = re.search(r"[?&]v=([\w-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(?:shorts|embed|live|v)/([\w-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([\w-]{11})", url)
    if m:
        return m.group(1)
    return None


def _yt_video_thumbs(video_id: str) -> List[Dict[str, Any]]:
    """Standard YouTube thumbnail ladder via img.youtube.com (no extractor needed)."""
    thumbs: List[Dict[str, Any]] = []
    for name, label, w, h in YT_THUMB_SIZES:
        u = f"https://img.youtube.com/vi/{video_id}/{name}.jpg"
        if not _probe_url(u, timeout=4):
            continue
        thumbs.append({
            "id": f"thumb-{name}",
            "label": f"Thumbnail · {label}",
            "kind": "thumbnail",
            "url": u,
            "width": w,
            "height": h,
            "ext": "jpg",
        })
    return thumbs


def _yt_channel_art(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Channel avatar + cover banner from the thumbnail list yt-dlp provides."""
    art: List[Dict[str, Any]] = []
    avatar_url, avatar_w = None, 0
    banner_url, banner_w = None, 0
    for t in info.get("thumbnails") or []:
        u = t.get("url") or ""
        w = t.get("width") or 0
        if not u.startswith("http"):
            continue
        if "-fcrop" in u:
            if banner_url is None or w >= banner_w:
                banner_url, banner_w = u, w
        elif "=s" in u:
            if avatar_url is None or "=s0" in u or w >= avatar_w:
                avatar_url, avatar_w = u, w
    if avatar_url:
        art.append({
            "id": "avatar",
            "label": "Profile picture · original" if "=s0" in avatar_url else (f"Profile picture · {avatar_w}×{avatar_w}" if avatar_w else "Profile picture"),
            "kind": "avatar",
            "url": _upgrade_avatar_url(avatar_url),
            "width": avatar_w or None,
            "height": avatar_w or None,
            "ext": _ext_of(avatar_url),
        })
    if banner_url:
        art.append({
            "id": "banner",
            "label": f"Channel cover banner · {banner_w}px wide" if banner_w else "Channel cover banner",
            "kind": "banner",
            "url": banner_url,
            "width": banner_w or None,
            "height": None,
            "ext": _ext_of(banner_url),
        })
    return art


def _profile_art_from_page(page: Optional[str]) -> List[Dict[str, Any]]:
    """Best-effort profile picture from the site's og:image tag (X, LinkedIn, IG, Pinterest, FB…)."""
    og = _og_image_url(page)
    if not og:
        return []
    return [{
        "id": "avatar",
        "label": "Profile picture · best available",
        "kind": "avatar",
        "url": _upgrade_avatar_url(og),
        "width": None,
        "height": None,
        "ext": _ext_of(og),
    }]


def _fetch_profile_art(url: str) -> List[Dict[str, Any]]:
    return _profile_art_from_page(_http_get(url))


def _image_family(url: str) -> str:
    """Stable identity for one image across CDN size variants (e.g. Pinterest 564x/736x/originals)."""
    host = (urlparse(url).hostname or "").lower()
    if "pinimg.com" in host:
        m = re.search(r"/(?:originals|[0-9]+x[0-9]*)/([^/]+/.+\.\w+)$", re.sub(r"[?#].*$", "", url))
        if m:
            return "pinimg:" + m.group(1)
    return re.sub(r"[?#].*$", "", url)


def _collect_post_images(info: Dict[str, Any], max_items: int = 12) -> List[Dict[str, Any]]:
    """Flatten thumbnails from info + playlist entries (carousels) into unique image candidates.

    Candidates are sorted by area (largest first) and deduped by base URL so
    multi-resolution thumbnail ladders collapse to their best version.
    """
    cands: List[Dict[str, Any]] = []
    entries = (info.get("entries") or []) if info.get("_type") == "playlist" else []
    sources = [info] + [e for e in entries if isinstance(e, dict)]
    seen = set()
    for src in sources:
        for t in src.get("thumbnails") or []:
            u = (t.get("url") or "").strip()
            if not u.startswith("http"):
                continue
            key = re.sub(r"[?#].*$", "", u)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"url": u, "width": t.get("width"), "height": t.get("height")})
        u = (src.get("thumbnail") or "").strip()
        if u.startswith("http"):
            key = re.sub(r"[?#].*$", "", u)
            if key not in seen:
                seen.add(key)
                cands.append({"url": u, "width": None, "height": None})

    def area(c: Dict[str, Any]) -> int:
        w, h = c.get("width") or 0, c.get("height") or 0
        return w * h

    cands.sort(key=area, reverse=True)

    keep: List[Dict[str, Any]] = []
    seen_family = set()
    for c in cands:
        host = (urlparse(c["url"]).hostname or "").lower()
        if "static.licdn.com" in host and "/sc/h" in c["url"]:
            continue  # generic LinkedIn placeholder logo
        fam = _image_family(c["url"])
        if fam in seen_family:
            continue
        seen_family.add(fam)
        keep.append(c)
    return keep[:max_items]


def _simple_post_images(info: Dict[str, Any], max_items: int = 12) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(_collect_post_images(info, max_items)):
        w, h = c.get("width"), c.get("height")
        dims = f" · {w}×{h}" if w and h else ""
        out.append({
            "id": f"post-{i + 1}",
            "label": f"Post image {i + 1}{dims}",
            "kind": "post_image",
            "url": c["url"],
            "width": w,
            "height": h,
            "ext": _ext_of(c["url"]),
        })
    return out


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    url: str
    cookies_browser: Optional[str] = None


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None  # None/"highest" = automatic best stream
    audio_only: bool = False
    audio_format: Optional[str] = "mp3"
    kind: str = "video"                       # video | audio | direct | images
    direct_url: Optional[str] = None          # kind=direct: single image URL
    image_urls: Optional[List[str]] = None    # kind=images: post images to pack
    image_ext: Optional[str] = None           # optional extension hint
    download_title: Optional[str] = None      # nice filename hint
    referer: Optional[str] = None             # optional HTTP referer for CDNs
    cookies_browser: Optional[str] = None     # logged-in browser session
    concurrency: int = Field(default=4, ge=1, le=8)
    rate_limit_mbps: Optional[float] = Field(default=None, ge=0, le=RATE_LIMIT_CEIL_MBPS)
    embed_metadata: bool = True
    embed_thumbnail: bool = True
    incognito: bool = False


def _embed_thumb_pp_key() -> str:
    """Pick the cover-art postprocessor key for the installed yt-dlp version."""
    try:
        from yt_dlp.postprocessor import get_postprocessor
        for key in ("EmbedThumbnail", "FFmpegEmbedThumbnail"):
            try:
                get_postprocessor(key)
                return key
            except (KeyError, AttributeError):
                continue
    except Exception:
        pass
    return "EmbedThumbnail"


EMBED_THUMB_KEY = _embed_thumb_pp_key()


def _build_common_opts(
    task_id: str,
    concurrency: int,
    rate_limit_mbps: Optional[float],
    cookies_browser: Optional[str] = None,
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,                 # never pull whole playlists
        "overwrites": True,                 # safe: each task has its own dir
        "continuedl": True,                 # resume partials
        "concurrent_fragment_downloads": max(1, min(8, concurrency)),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "buffersize": 1024 * 1024,
        "progress_hooks": [make_yt_hook(task_id)],
    }
    opts.update(_cookies_opts(cookies_browser))
    if rate_limit_mbps and rate_limit_mbps > 0:
        opts["ratelimit"] = int(rate_limit_mbps * 1024 * 1024)
    return opts


def make_yt_hook(task_id: str):
    def hook(d: Dict[str, Any]) -> None:
        with _lock:
            task = tasks.get(task_id)
            if task is None:
                return
            status = d.get("status")
            if status == "downloading":
                task["status"] = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                task["downloaded_bytes"] = downloaded
                task["total_bytes"] = total
                if total > 0:
                    task["percentage"] = min(98.0, round((downloaded / total) * 100, 1))
                task["speed"] = d.get("speed") or 0
                task["speed_str"] = format_speed(task["speed"])
                task["downloaded_str"] = format_bytes(downloaded)
                task["total_str"] = format_bytes(total) if total else "Dynamic"
                task["eta"] = d.get("eta")
                task["eta_str"] = format_eta(d.get("eta"))
                if d.get("filename"):
                    task["filename"] = os.path.basename(d["filename"])
                frag_idx = d.get("fragment_index")
                frag_tot = d.get("fragment_count")
                if frag_idx and frag_tot:
                    task["status_msg"] = f"Streaming fragments {frag_idx}/{frag_tot}…"
                elif task["total_bytes"] == 0:
                    task["status_msg"] = "Fetching media at full speed…"
                else:
                    task["status_msg"] = "Downloading video stream in high resolution…"
            elif status == "finished":
                task["status"] = "processing"
                task["percentage"] = 99.0
                task["status_msg"] = "Merging & processing with FFmpeg…"
    return hook


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------
def _pretty_source(extractor_key: str, extractor: str, info: Dict[str, Any]) -> str:
    """Human label for the detected source site."""
    key = (extractor_key or "").lower()
    name = (extractor or "").lower()
    if key and key != "generic" and name != "generic":
        return extractor_key.upper()
    host = (urlparse(info.get("webpage_url") or "").hostname or "").lower()
    if host:
        return host.replace("www.", "").split(".")[0].upper()
    return "DIRECT LINK"


@app.post("/api/analyze")
def analyze_video(req: AnalyzeRequest):
    url = normalize_media_url(req.url)
    if not url:
        raise HTTPException(
            status_code=400,
            detail="Paste a valid media link (https://…). The press reads YouTube, Instagram, X, Facebook, TikTok, LinkedIn, Pinterest, Reddit, Vimeo and 1,000+ more sources — videos, images, posts & profile art.",
        )

    # Direct image links — no extractor needed, instant result.
    path_l = urlparse(url).path.lower()
    if re.search(r"\.(jpe?g|png|webp|gif|avif|bmp|ico|heic)$", path_l):
        ext = _ext_of(url)
        base = _sanitize_component(os.path.basename(re.sub(r"[?#].*$", "", url)) or "image", "image")
        return {
            "url": url,
            "title": base,
            "duration": 0,
            "duration_str": "Image",
            "thumbnail": url,
            "uploader": "Direct link",
            "view_count": 0,
            "view_count_str": "N/A",
            "is_live": False,
            "source": "DIRECT IMAGE",
            "extractor_key": "Direct",
            "highest_res_height": 0,
            "highest_res_label": "Direct image",
            "media_kind": "images",
            "is_profile": False,
            "has_video": False,
            "has_audio": False,
            "video_formats": [],
            "audio_formats": [],
            "images": [{
                "id": "direct",
                "label": f"Image · {ext.upper()}",
                "kind": "post_image",
                "url": url,
                "width": None,
                "height": None,
                "ext": ext,
            }],
            "image_count": 1,
        }

    is_profile_guess = _is_yt_channel_url(url) or _is_profile_url(url)
    profile_page = _http_get(url) if is_profile_guess else None
    profile_art = _profile_art_from_page(profile_page)

    ydl_opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "extractor_retries": 3,
    }
    ydl_opts.update(_cookies_opts(req.cookies_browser))
    if is_profile_guess:
        # Profile pages are big playlists — one entry is enough to characterise them.
        ydl_opts["playlist_items"] = "1"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        if is_profile_guess and profile_art:
            return _profile_only_result(url, profile_art, profile_page)
        fallback = _post_image_fallback(url)
        if fallback:
            return fallback
        raise HTTPException(status_code=500, detail=_clean_error(e))

    if not info:
        raise HTTPException(status_code=404, detail="Media information could not be retrieved.")

    title = info.get("title", "Unknown Title")
    duration = info.get("duration", 0)
    thumbnails = info.get("thumbnails") or []
    thumbnail = info.get("thumbnail") or (thumbnails[-1]["url"] if thumbnails else "")
    uploader = info.get("uploader") or info.get("channel") or "Unknown Uploader"
    view_count = info.get("view_count", 0)
    is_live = bool(info.get("is_live", False))
    extractor_key = (info.get("extractor_key") or "").strip()
    extractor = (info.get("extractor") or "").strip()
    source_label = _pretty_source(extractor_key, extractor, info)

    # Flatten formats across playlist entries (e.g. Instagram carousels with video slides).
    all_formats: List[Dict[str, Any]] = list(info.get("formats") or [])
    if info.get("_type") == "playlist":
        for entry in info.get("entries") or []:
            if isinstance(entry, dict):
                all_formats.extend(entry.get("formats") or [])

    def codec_family(vcodec: str) -> str:
        v = (vcodec or "").lower()
        if v.startswith("av01"):
            return "AV1"
        if v.startswith("vp09") or v.startswith("vp9"):
            return "VP9"
        if v.startswith("avc1"):
            return "H.264"
        return "Other"

    video_formats_map: Dict[int, Dict[str, Any]] = {}
    highest_res_height, highest_fps = 0, 0

    for f in all_formats:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height") or 0
        fps = f.get("fps") or 0
        filesize = f.get("filesize") or f.get("filesize_estimate") or 0
        dynamic_range = (f.get("dynamic_range") or "").lower()

        if height > highest_res_height:
            highest_res_height = height
        if fps > highest_fps:
            highest_fps = fps

        if vcodec != "none":
            if height > 0:
                quality_label = f"{height}p" + (f"{int(fps)}" if fps and fps >= 50 else "")
                if height >= 4320:
                    res_name = f"{height}p (8K Ultra HD)"
                elif height >= 2160:
                    res_name = f"{height}p (4K Ultra HD)"
                elif height >= 1440:
                    res_name = f"{height}p (2K QHD)"
                elif height >= 1080:
                    res_name = f"{height}p (Full HD)"
                elif height >= 720:
                    res_name = f"{height}p (HD)"
                else:
                    res_name = f"{height}p"
            else:
                quality_label = "Video"
                res_name = "Source quality"

            existing = video_formats_map.get(height)
            if existing is None or (filesize > existing["filesize"]) or (fps > existing["fps"]):
                video_formats_map[height] = {
                    "format_id": f.get("format_id"),
                    "height": height,
                    "width": f.get("width") or 0,
                    "fps": fps,
                    "ext": f.get("ext", ""),
                    "quality_label": quality_label,
                    "res_name": res_name,
                    "filesize": filesize,
                    "filesize_str": format_bytes(filesize) if filesize else "Dynamic",
                    "vcodec": vcodec,
                    "codec_family": codec_family(vcodec),
                    "hdr": dynamic_range not in ("", "sdr", "unknown", "none"),
                    "has_audio": acodec != "none",
                }

    sorted_video_formats = sorted(
        video_formats_map.values(), key=lambda x: (x["height"], x["fps"]), reverse=True
    )

    preset_audio_formats = [
        {"id": "mp3-320", "format": "mp3", "label": "MP3 Audio (320 kbps, max quality)", "bitrate": "320k"},
        {"id": "m4a-best", "format": "m4a", "label": "M4A AAC (original audio track)", "bitrate": "Best"},
        {"id": "wav-lossless", "format": "wav", "label": "WAV Audio (lossless PCM)", "bitrate": "Lossless"},
        {"id": "flac-lossless", "format": "flac", "label": "FLAC Audio (free lossless)", "bitrate": "Lossless"},
    ]

    # ---- Universal extras: thumbnails, post images, profile art -----------------
    is_yt_channel = _is_yt_channel_url(url)
    is_profile = is_yt_channel or _is_profile_url(url, extractor_key)

    images: List[Dict[str, Any]] = []
    if is_yt_channel:
        images = _yt_channel_art(info) or profile_art
        if not images:
            images = _simple_post_images(info)
    elif is_profile:
        images = profile_art or _fetch_profile_art(url)
    else:
        video_id = _yt_video_id(url)
        if _is_yt_video_url(url) and video_id:
            images = _yt_video_thumbs(video_id)
        else:
            images = _simple_post_images(info)

    if is_profile and images:
        media_kind = "profile"
    elif sorted_video_formats:
        media_kind = "video"
    elif images:
        media_kind = "images"
    else:
        media_kind = "unknown"

    has_audio = any((f.get("acodec") or "none") != "none" for f in all_formats)

    return {
        "url": url,
        "title": title,
        "duration": duration,
        "duration_str": format_eta(duration) if duration else "Live Stream",
        "thumbnail": thumbnail,
        "uploader": uploader,
        "view_count": view_count,
        "view_count_str": f"{view_count:,}" if view_count else "N/A",
        "is_live": is_live,
        "source": source_label,
        "extractor_key": extractor_key,
        "highest_res_height": highest_res_height,
        "highest_res_label": (
            "Best Available"
            if highest_res_height <= 0
            else (
                f"{highest_res_height}p" + (f"{int(highest_fps)}" if highest_fps >= 50 else "")
                + (" (8K Ultra HD)" if highest_res_height >= 4320
                   else " (4K Ultra HD)" if highest_res_height >= 2160
                   else " (Full HD)" if highest_res_height >= 1080 else "")
            )
        ),
        "media_kind": media_kind,
        "is_profile": is_profile,
        "has_video": bool(sorted_video_formats),
        "has_audio": has_audio,
        "video_formats": sorted_video_formats,
        "audio_formats": preset_audio_formats,
        "images": images,
        "image_count": len(images),
    }


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------
def _fail_task(task_id: str, message: str) -> None:
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = "failed"
            task["error"] = message


def execute_download(task_id: str, req: DownloadRequest) -> None:
    task_dir = os.path.join(STAGING_ROOT, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
        if req.kind in ("direct", "images"):
            if req.kind == "direct":
                _run_direct_download(task_id, req, task_dir)
            else:
                _run_multi_image_download(task_id, req, task_dir)
        else:
            for attempt in (1, 2):
                try:
                    _run_download(task_id, req, task_dir)
                    break
                except Exception as e:
                    err = _clean_error(e)
                    if attempt == 1 and _is_embed_error(err):
                        with _lock:
                            task = tasks.get(task_id)
                            if task:
                                task["status"] = "downloading"
                                task["status_msg"] = "Retrying without artwork embedding…"
                        req = req.model_copy(update={"embed_thumbnail": False, "embed_metadata": False})
                        continue
                    raise
    except Exception as e:
        err = _clean_error(e)
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["status"] = "failed"
                task["error"] = err
        return

    _finalize(task_id, task_dir, req)


def _run_download(task_id: str, req: DownloadRequest, task_dir: str) -> None:
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = "downloading"
            task["status_msg"] = "Fetching video streams…"

    # Fast live-stream guard (avoids hanging on an endless stream)
    precheck_opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    precheck_opts.update(_cookies_opts(req.cookies_browser))
    with yt_dlp.YoutubeDL(precheck_opts) as ydl:
        info = ydl.extract_info(req.url, download=False)
    if info and info.get("is_live"):
        raise RuntimeError("Live streams cannot be downloaded — wait until the stream ends, then retry.")

    if info:
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["title"] = info.get("title") or task.get("title") or "Untitled Media"
                task["thumbnail"] = info.get("thumbnail") or ""
                task["source_label"] = _pretty_source(
                    info.get("extractor_key") or "", info.get("extractor") or "", info
                )

    opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps, req.cookies_browser)
    output_template = os.path.join(task_dir, "%(title)s [%(format_id)s].%(ext)s" if not req.audio_only else "%(title)s.%(ext)s")
    opts["outtmpl"] = output_template

    if req.audio_only:
        audio_fmt = (req.audio_format or "mp3").lower()
        if audio_fmt not in ALLOWED_AUDIO_FORMATS:
            raise RuntimeError(f"Unsupported audio format: {audio_fmt}")
        opts["format"] = "bestaudio/best"
        postprocessors: List[Dict[str, Any]] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt,
            "preferredquality": "320" if audio_fmt == "mp3" else "0",
        }]
        if req.embed_thumbnail:
            opts["writethumbnail"] = True
            postprocessors.append({"key": EMBED_THUMB_KEY})
        if req.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        opts["postprocessors"] = postprocessors
    else:
        if req.format_id and req.format_id != "highest":
            opts["format"] = f"{req.format_id}+bestaudio/bestvideo+bestaudio/best"
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        if req.embed_metadata:
            opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(req.url, download=True)


def _run_direct_download(task_id: str, req: DownloadRequest, task_dir: str) -> None:
    """Download a single direct media URL (image / thumbnail / avatar) at full speed."""
    url = req.direct_url or ""
    if not url.startswith("http"):
        raise RuntimeError("No valid image URL to download.")

    title = _sanitize_component(req.download_title or "image", "image")
    ext = (req.image_ext or _ext_of(url)).lstrip(".")
    headers = {"User-Agent": DEFAULT_UA}
    if req.referer:
        headers["Referer"] = req.referer

    with _lock:
        task = tasks.get(task_id)
        if task:
            task["title"] = title
            task["thumbnail"] = url

    opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps)
    opts["outtmpl"] = os.path.join(task_dir, "%(title)s.%(ext)s")
    opts["writethumbnail"] = False
    info = {
        "id": task_id[:8],
        "title": title,
        "url": url,
        "ext": ext,
        "http_headers": headers,
    }
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = "downloading"
            task["status_msg"] = "Fetching image at full resolution…"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_ie_result(info, download=True)


def _run_multi_image_download(task_id: str, req: DownloadRequest, task_dir: str) -> None:
    """Download every image of a post; multiple files are packed into one ZIP later."""
    urls = [u for u in (req.image_urls or []) if u and u.startswith("http")]
    if not urls:
        raise RuntimeError("No images found to download.")
    headers = {"User-Agent": DEFAULT_UA}
    if req.referer:
        headers["Referer"] = req.referer

    base_title = _sanitize_component(req.download_title or "images", "images")
    total = len(urls)
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["title"] = base_title
            task["thumbnail"] = urls[0]

    for i, u in enumerate(urls, start=1):
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["status"] = "downloading"
                task["status_msg"] = f"Fetching image {i} of {total}…"
        opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps)
        opts["outtmpl"] = os.path.join(task_dir, f"{base_title} {i:02d}.%(ext)s")
        opts["writethumbnail"] = False
        info = {
            "id": f"{task_id[:8]}-{i}",
            "title": f"{base_title} {i:02d}",
            "url": u,
            "ext": _ext_of(u),
            "http_headers": headers,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_ie_result(info, download=True)


def _move_to_downloads(staging_path: str) -> str:
    """Move a finished file into the OS Downloads folder, avoiding name collisions.

    The collision check + move run under the global lock so two tasks finishing
    with the same title can never overwrite each other.
    """
    filename = os.path.basename(staging_path)
    base, ext = os.path.splitext(filename)
    with _lock:
        dest = os.path.join(DOWNLOADS_DIR, filename)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(DOWNLOADS_DIR, f"{base} ({counter}){ext}")
            counter += 1
        shutil.move(staging_path, dest)
    return dest


def _save_output(task_id: str, task_dir: str, req: DownloadRequest, file_path: str, file_size: int, quality: str) -> None:
    """Move a finished file into Downloads and record it (history/streak/stats)."""
    try:
        final_path = _move_to_downloads(file_path)
    except OSError as e:
        _fail_task(task_id, f"Could not save to Downloads folder: {e}")
        shutil.rmtree(task_dir, ignore_errors=True)
        return
    finally:
        try:
            os.rmdir(task_dir)
        except OSError:
            pass

    filename = os.path.basename(final_path)
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = "completed"
            task["percentage"] = 100.0
            task["filename"] = filename
            task["filepath"] = final_path
            task["file_size"] = file_size
            task["file_size_str"] = format_bytes(file_size)
            task["status_msg"] = "Download complete — saved to your PC!"
        # Stats are anonymous aggregate counters (files really exist on disk),
        # so they always tick. Incognito skips only the history ledger + streak.
        stats["downloads"] += 1
        stats["total_bytes"] += file_size
        if not req.incognito:
            history.insert(0, {
                "task_id": task_id,
                "title": (task or {}).get("title") or "Untitled Media",
                "filename": filename,
                "file_size_str": format_bytes(file_size),
                "thumbnail": (task or {}).get("thumbnail", ""),
                "kind": "image" if req.kind in ("direct", "images") else ("audio" if req.audio_only else "video"),
                "audio_only": req.audio_only,
                "quality": quality,
                "source": (task or {}).get("source_label", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            history[:] = history[:50]
            _record_download_day()
    _persist_state()


def _finalize_images(task_id: str, task_dir: str, req: DownloadRequest) -> None:
    files = _find_output_files(task_dir)
    if not files:
        _fail_task(task_id, "Downloaded file could not be located.")
        return
    if len(files) == 1:
        final_path = files[0]
        size = os.path.getsize(final_path)
        quality = "Image · " + os.path.splitext(final_path)[1].lstrip(".").upper()
    else:
        zip_name = f"{_sanitize_component(req.download_title or 'images', 'images')} ({len(files)} images).zip"
        zip_path = os.path.join(task_dir, zip_name)
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, os.path.basename(f))
        except Exception:
            _fail_task(task_id, "Could not pack the images into an archive.")
            return
        final_path = zip_path
        size = os.path.getsize(final_path)
        quality = f"Images · {len(files)} files"
    _save_output(task_id, task_dir, req, final_path, size, quality)


def _finalize(task_id: str, task_dir: str, req: DownloadRequest) -> None:
    if req.kind in ("direct", "images"):
        _finalize_images(task_id, task_dir, req)
        return

    file_path, file_size = _find_output_file(task_dir)
    if not file_path or file_size <= 0:
        _fail_task(task_id, "Downloaded file could not be located.")
        return

    quality = ("Audio · " + (req.audio_format or "mp3").upper()) if req.audio_only else "Video + Audio"
    _save_output(task_id, task_dir, req, file_path, file_size, quality)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.post("/api/download")
def start_download(req: DownloadRequest):
    url = normalize_media_url(req.url)
    if not url:
        raise HTTPException(status_code=400, detail="Paste a valid media link (https://…).")

    if req.kind not in ("video", "audio", "direct", "images"):
        raise HTTPException(status_code=400, detail="Unknown download kind.")
    if req.kind == "direct" and not (req.direct_url or "").startswith("http"):
        raise HTTPException(status_code=400, detail="No image URL supplied for this download.")

    task_id = str(uuid.uuid4())
    with _lock:
        queue_position = _active_download_count()
        if queue_position >= MAX_WORKERS + 6:
            raise HTTPException(status_code=429, detail="Too many queued downloads — wait for the engine to catch up.")

        tasks[task_id] = {
            "task_id": task_id,
            "url": url,
            "kind": req.kind,
            "status": "queued" if queue_position >= MAX_WORKERS else "initializing",
            "queue_position": queue_position + 1,
            "percentage": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed": 0,
            "speed_str": "0 KB/s",
            "downloaded_str": "0 MB",
            "total_str": "Dynamic",
            "eta": 0,
            "eta_str": "--:--",
            "filename": "",
            "filepath": "",
            "title": "Untitled Media",
            "thumbnail": "",
            "status_msg": "Queued — waiting for a free engine slot…" if queue_position >= MAX_WORKERS else "Initializing download worker…",
            "error": None,
        }
        _prune_tasks()

    executor.submit(execute_download, task_id, req.model_copy(update={"url": url}))
    return {"task_id": task_id}


@app.get("/api/progress/{task_id}")
def get_progress(task_id: str):
    with _lock:
        task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


@app.get("/api/file/{task_id}")
def get_file(task_id: str):
    with _lock:
        task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    filepath = task.get("filepath") or ""
    if task.get("status") != "completed" or not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=400, detail="File is not ready for download yet.")
    return FileResponse(path=filepath, filename=task.get("filename") or os.path.basename(filepath), media_type="application/octet-stream")


@app.post("/api/open-folder/{task_id}")
def open_download_folder(task_id: str):
    """Open the containing folder in the OS file explorer (local personal use)."""
    with _lock:
        task = tasks.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("filepath"):
        raise HTTPException(status_code=404, detail="No saved file for this task.")
    folder = os.path.dirname(task["filepath"])
    try:
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open folder: {e}")
    return {"ok": True, "folder": folder}


@app.get("/api/history")
def get_history():
    with _lock:
        return history[:50]


@app.delete("/api/history")
def clear_history():
    global history
    with _lock:
        history = []
    _persist_state()
    return {"ok": True, "cleared": True}


@app.get("/api/stats")
def get_stats():
    with _lock:
        return {
            "downloads": stats["downloads"],
            "total_bytes": stats["total_bytes"],
            "total_bytes_str": format_bytes(stats["total_bytes"]),
            "history_count": len(history),
            "active_downloads": _active_download_count(),
            "streak": int(streak_state.get("streak", 0)),
            "started_at": stats["started_at"],
        }


@app.get("/api/streak")
def get_streak():
    with _lock:
        return {
            "streak": int(streak_state.get("streak", 0)),
            "best": int(streak_state.get("best", 0)),
            "last_date": streak_state.get("last_date", ""),
            "downloaded_today": streak_state.get("last_date") == _today_str(),
        }


@app.get("/api/health")
def health():
    with _lock:
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "ffmpeg": FFMPEG_EXE,
            "workers": MAX_WORKERS,
            "active_downloads": _active_download_count(),
            "history_count": len(history),
            "total_downloads": stats["downloads"],
            "total_bytes": stats["total_bytes"],
            "uptime_seconds": int(time.time() - stats["started_at"]),
        }


# Mount client app (must come last so API routes take precedence)
if os.path.exists(CLIENT_DIR):
    app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")
