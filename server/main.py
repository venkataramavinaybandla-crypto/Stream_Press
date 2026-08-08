"""STREAM PRESS — Universal Personal-Use Media Downloader (local service backend).

Strictly a private, non-commercial, local-first utility. Binds to 127.0.0.1
only, performs no telemetry, and talks only to the media sources you paste
into it. Reads YouTube, Instagram, X, Facebook, TikTok, Pinterest, Reddit,
Vimeo, Twitch and 1,000+ more sources. Downloads are intended for personal
offline viewing, archiving and educational research under Fair Use
guidelines. Re-distribution of copyrighted material is the sole
responsibility of the end user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
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
APP_VERSION = "2.0.0"

# Healthy-service limits (override via env if you really need to)
MAX_WORKERS = max(1, int(os.environ.get("YTMAX_WORKERS", "2")))
TASK_CAP = max(10, int(os.environ.get("YTMAX_TASK_CAP", "60")))
RATE_LIMIT_CEIL_MBPS = 20

ALLOWED_AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEMP_SUFFIXES = (".part", ".ytdl", ".temp")

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
        return "Unsupported or invalid link — the press could not read it. Paste a direct video page URL."
    if "video unavailable" in lowered or "not available" in lowered:
        return "Media unavailable — it may be private, region-locked, or removed."
    if "private video" in lowered or "private post" in lowered:
        return "This media is private and cannot be downloaded."
    if any(k in lowered for k in ("login required", "log in", "sign in", "authentication", "requires a login")):
        return "This link requires a login — sign-in protected content cannot be saved."
    if any(k in lowered for k in ("drm", "playready", "widevine", "fairplay")):
        return "DRM-protected stream — copy-protected content cannot be saved."
    if "confirm you're not a bot" in lowered or "bot check" in lowered:
        return "The source is running a bot check right now. Wait a few minutes and try again."
    if "unsupported" in lowered:
        return "This site or link type is not supported yet. Try a direct video page URL."
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
    """Largest non-image, non-temp file produced in the task directory."""
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


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None  # None/"highest" = automatic best stream
    audio_only: bool = False
    audio_format: Optional[str] = "mp3"
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


def _build_common_opts(task_id: str, concurrency: int, rate_limit_mbps: Optional[float]) -> Dict[str, Any]:
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
        raise HTTPException(status_code=400, detail="Paste a valid video link (https://…). The press reads YouTube, Instagram, X, Facebook, TikTok, Pinterest, Reddit, Vimeo and 1,000+ more sources.")

    ydl_opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "extractor_retries": 3,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_clean_error(e))

    if not info:
        raise HTTPException(status_code=404, detail="Video information could not be retrieved.")

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

    for f in info.get("formats", []):
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
        "video_formats": sorted_video_formats,
        "audio_formats": preset_audio_formats,
    }


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------
def execute_download(task_id: str, req: DownloadRequest) -> None:
    task_dir = os.path.join(STAGING_ROOT, task_id)
    os.makedirs(task_dir, exist_ok=True)

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

    opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps)
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


def _finalize(task_id: str, task_dir: str, req: DownloadRequest) -> None:
    file_path, file_size = _find_output_file(task_dir)
    if not file_path or file_size <= 0:
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["status"] = "failed"
                task["error"] = "Downloaded file could not be located."
        return

    # Finished files go straight into the PC's Downloads folder.
    try:
        final_path = _move_to_downloads(file_path)
    except OSError as e:
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["status"] = "failed"
                task["error"] = f"Could not save to Downloads folder: {e}"
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
                "audio_only": req.audio_only,
                "quality": ("Audio · " + (req.audio_format or "mp3").upper()) if req.audio_only else "Video + Audio",
                "source": (task or {}).get("source_label", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            history[:] = history[:50]
            _record_download_day()
    _persist_state()


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.post("/api/download")
def start_download(req: DownloadRequest):
    url = normalize_media_url(req.url)
    if not url:
        raise HTTPException(status_code=400, detail="Paste a valid video link (https://…).")

    task_id = str(uuid.uuid4())
    with _lock:
        queue_position = _active_download_count()
        if queue_position >= MAX_WORKERS + 6:
            raise HTTPException(status_code=429, detail="Too many queued downloads — wait for the engine to catch up.")

        tasks[task_id] = {
            "task_id": task_id,
            "url": url,
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
