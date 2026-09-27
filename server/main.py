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

import base64
import binascii
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunsplit

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

# ---------------------------------------------------------------------------
# FFmpeg resolution — muxing must never silently degrade to a 360p combined
# stream. Prefer a real system ffmpeg (the Dockerfile apt-installs one) and fall
# back to the static binary bundled with imageio-ffmpeg so local runs work.
# ---------------------------------------------------------------------------
def _resolve_ffmpeg() -> str:
    explicit = (os.environ.get("YTMAX_FFMPEG") or os.environ.get("FFMPEG_BINARY") or "").strip()
    for cand in (explicit, shutil.which("ffmpeg") or ""):
        if cand and os.path.exists(cand):
            return cand
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _ffmpeg_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15)
        line = (out.stdout or "").splitlines()[0] if out.stdout else ""
        return line.strip() or "unknown"
    except Exception:
        return "unavailable"


def _ytdlp_version() -> str:
    return getattr(yt_dlp.version, "__version__", "unknown")


FFMPEG_EXE = _resolve_ffmpeg()
FFMPEG_VERSION = _ffmpeg_version(FFMPEG_EXE)
FFMPEG_AVAILABLE = FFMPEG_VERSION != "unavailable"

APP_NAME = "STREAM PRESS"
APP_VERSION = "3.1.0"

# Healthy-service limits (override via env if you really need to)
MAX_WORKERS = max(1, int(os.environ.get("YTMAX_WORKERS", "2")))
TASK_CAP = max(10, int(os.environ.get("YTMAX_TASK_CAP", "60")))
RATE_LIMIT_CEIL_MBPS = 20

ALLOWED_AUDIO_FORMATS = {"mp3", "m4a", "wav", "flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TEMP_SUFFIXES = (".part", ".ytdl", ".temp")

# Manifest-first selection: always ask for best video + best audio as SEPARATE
# streams (from the HLS/DASH manifest) and let ffmpeg mux them. Never settle for
# a pre-combined low-quality fallback, which is what caps output at 360p/720p.
DEFAULT_VIDEO_FORMAT = "bestvideo+bestaudio/best"

# A single well-formed browser UA helps CDNs (Instagram, Pinterest, X…) serve us.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Cookie pipeline — the auth-wall fix
# ---------------------------------------------------------------------------
# Authenticated / age-restricted / members-only videos need a signed-in session.
# Locally yt-dlp can borrow your browser profile, but a deployed server has no
# browser — so it reads a Netscape-format cookies.txt instead:
#   YTMAX_COOKIES_FILE=/etc/secrets/cookies.txt   (Render: a Secret File)
#   YTMAX_COOKIES_TXT="<file contents>"           (inline, if you prefer an env var)
COOKIES_FILE_ENV = (os.environ.get("YTMAX_COOKIES_FILE") or "").strip()
COOKIES_TXT_ENV = os.environ.get("YTMAX_COOKIES_TXT") or ""
COOKIES_DEFAULT_PATHS = [
    "/etc/secrets/cookies.txt",              # Render Secret File default location
    os.path.join(BASE_DIR, "cookies.txt"),   # repo root (git-ignored)
]


def _looks_like_netscape_cookies(text: str) -> bool:
    """Accept the standard cookies.txt header or any tab-separated cookie row."""
    if not text:
        return False
    if "# Netscape HTTP Cookie File" in text or "# HTTP Cookie File" in text:
        return True
    for line in text.splitlines():
        if line and not line.startswith("#") and line.count("\t") >= 6:
            return True
    return False


def _resolve_cookies_file() -> Optional[str]:
    for cand in [COOKIES_FILE_ENV] + COOKIES_DEFAULT_PATHS:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(8192)
        except OSError:
            continue
        if _looks_like_netscape_cookies(head):
            return cand
    return None


def _materialize_inline_cookies() -> Optional[str]:
    """Write YTMAX_COOKIES_TXT to a private staging file yt-dlp can read."""
    if not _looks_like_netscape_cookies(COOKIES_TXT_ENV):
        return None
    path = os.path.join(STAGING_ROOT, "cookies.txt")
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(COOKIES_TXT_ENV)
        os.chmod(path, 0o600)
        return path
    except OSError:
        return None


COOKIES_FILE = _resolve_cookies_file() or _materialize_inline_cookies()
COOKIES_CONFIGURED = bool(COOKIES_FILE)

# ---------------------------------------------------------------------------
# Anti-bot / network resilience — a SEPARATE failure mode from the auth wall
# ---------------------------------------------------------------------------
# A datacenter IP range (Render's shared egress) can be rate-limited or blocked
# outright; cookies do not fix that. Mitigations: a realistic User-Agent, a
# randomized delay between fragment requests, and an optional proxy.
PROXY_URL = (os.environ.get("YTMAX_PROXY") or "").strip() or None


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


SLEEP_INTERVAL = _env_float("YTMAX_SLEEP_INTERVAL", 0.0)
MAX_SLEEP_INTERVAL = _env_float("YTMAX_MAX_SLEEP_INTERVAL", 0.0)
SLEEP_INTERVAL_REQUESTS = _env_float("YTMAX_SLEEP_INTERVAL_REQUESTS", 0.0)
# Refresh yt-dlp at boot so YouTube signature-cipher patches are never stale.
YTDLP_AUTO_UPDATE = (os.environ.get("YTMAX_AUTOUPDATE_YTDLP", "0").strip().lower()
                     in ("1", "true", "yes", "on"))


def _build_url_opener() -> urllib.request.OpenerDirector:
    handlers: List[Any] = []
    if PROXY_URL:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
    return urllib.request.build_opener(*handlers)


# urllib paths (page scrapes, direct image fetches) honour the same proxy as yt-dlp.
_URL_OPENER = _build_url_opener()


def _curl_get(url: str, timeout: float) -> Optional[Any]:
    """One curl_cffi GET with the browser persona; None when unavailable/failed.
    Used as the first rung of the raw-fetch ladder — curl_cffi requests are NOT
    routed through YTMAX_PROXY (urllib fallback below is)."""
    if not url.startswith("http"):
        return None
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.get(url, impersonate="chrome", timeout=timeout,
                                 allow_redirects=True)
    except Exception:
        return None


def _network_opts() -> Dict[str, Any]:
    """Shared yt-dlp options for anti-bot resilience (UA + jittered sleeps + proxy)."""
    opts: Dict[str, Any] = {
        "http_headers": {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    if SLEEP_INTERVAL > 0:
        opts["sleep_interval"] = SLEEP_INTERVAL
    if MAX_SLEEP_INTERVAL > 0:
        opts["max_sleep_interval"] = max(MAX_SLEEP_INTERVAL, SLEEP_INTERVAL)
    if SLEEP_INTERVAL_REQUESTS > 0:
        opts["sleep_interval_requests"] = SLEEP_INTERVAL_REQUESTS
    return opts


# ---------------------------------------------------------------------------
# Anti-"source refused the connection" resilience (site-agnostic)
# ---------------------------------------------------------------------------
# Many sources (Cloudflare-fronted or not) drop requests that don't look like a
# real browser: non-browser TLS fingerprints, server/datacenter IPs, and broken
# IPv6 egress all surface to the user as "connection refused / reset / timed
# out". Instead of special-casing any one site, every extraction climbs a
# client-persona retry ladder: hardened base options → browser TLS
# impersonation (curl_cffi, if installed). Raw page fetches (_http_get) use the
# same persona first. That fixes whole classes of sites at once.

def _refused_connection(exc: Exception) -> bool:
    """True for the connection-class failures a client-persona retry can fix:
    TCP/TLS rejection, bot-walls that answer 403, DNS flakes, timeouts."""
    lowered = str(exc).lower()
    return any(k in lowered for k in (
        "connection refused", "connection aborted", "connection reset",
        "reset by peer", "remote end closed connection", "tunnel connection failed",
        "timed out", "timeout", "getaddrinfo failed",
        "temporary failure in name resolution", "name or service not known",
        "errno 10060", "errno 10061", "errno 10054",
        "http error 403", "forbidden", "ssl:", "certificate verify failed",
        "sslv3", "handshake", "unable to download webpage",
    ))


_IMPERSONATE_STATE: Dict[str, Any] = {"tried": False, "target": None}


def _get_impersonate_target() -> Optional[Any]:
    """The curl_cffi Chrome fingerprint for yt-dlp — discovered once; None when
    the impersonate stack is unavailable (later retries then skip that rung)."""
    if not _IMPERSONATE_STATE["tried"]:
        _IMPERSONATE_STATE["tried"] = True
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            target = ImpersonateTarget(client="chrome")
            yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "impersonate": target})
            _IMPERSONATE_STATE["target"] = target
        except Exception:
            _IMPERSONATE_STATE["target"] = None
    return _IMPERSONATE_STATE["target"]


def _resilient_network_opts() -> Dict[str, Any]:
    """_network_opts() + hardening applied to every attempt: force IPv4 egress
    (broken IPv6 reads as 'connection refused') and accept odd legacy TLS."""
    opts = dict(_network_opts())
    opts["source_address"] = "0.0.0.0"
    opts["legacy_server_connect"] = True
    return opts


def _extract_with_resilience(opts: Dict[str, Any], url: str,
                             download: bool = False) -> Optional[Dict[str, Any]]:
    """yt-dlp extraction behind a client-persona retry ladder.

    Rung 1 — hardened base options (IPv4, legacy-TLS tolerance).  Rung 2 — plus
    browser TLS impersonation for servers that reject non-browser clients (and
    for 403 bot-walls). A non-connection error (unsupported URL, DRM, …) stops
    the climb immediately — no persona fixes those. The caller's *opts* always
    win (format selection, outtmpl, hooks…).
    """
    base = _resilient_network_opts()
    rungs: List[Dict[str, Any]] = [dict(base)]
    target = _get_impersonate_target()
    if target is not None:
        rungs.append({**base, "impersonate": target})
    first_error: Optional[Exception] = None
    last_error: Optional[Exception] = None
    for rung in rungs:
        rung.update(opts)
        try:
            with yt_dlp.YoutubeDL(rung) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as e:
            if first_error is None:
                first_error = e
            last_error = e
            if not _refused_connection(e):
                break                      # deterministic failure — stop climbing
            if "impersonate" in rung:
                _IMPERSONATE_STATE["target"] = None   # persona rejected too
    raise last_error or first_error or RuntimeError("Extraction failed.")


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


def _maybe_auto_update_ytdlp() -> None:
    """Refresh yt-dlp at boot so extractor patches are never stale.

    pip-installed yt-dlp refuses ``-U``, so we upgrade the package instead. Set
    ``YTMAX_AUTOUPDATE_YTDLP=1`` (the Dockerfile does) to enable.
    """
    if not YTDLP_AUTO_UPDATE:
        return
    before = _ytdlp_version()
    print(f"[yt-dlp] auto-update enabled - refreshing extractors (installed: {before})")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as exc:
        print(f"[yt-dlp] auto-update could not run (continuing as-is): {exc}")
        return
    after = _ytdlp_version()
    if proc.returncode == 0:
        print(f"[yt-dlp] extractors up to date: {before} -> {after}")
    else:
        print(f"[yt-dlp] auto-update returned {proc.returncode}; staying on {after}")


def _audit_extractors() -> None:
    """Confirm the high-traffic sites resolve to yt-dlp's NATIVE extractors
    (no custom scraping), including the X/Twitter endpoint."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        return
    wanted = ("youtube", "twitter", "instagram", "tiktok", "facebook", "reddit")
    found = set()
    for ie in gen_extractor_classes():
        key = (ie.ie_key() or "").lower()
        for w in wanted:
            if w in key:
                found.add(w)
    absent = sorted(set(wanted) - found)
    print(f"[extractors] native coverage: {sorted(found)}")
    if absent:
        print(f"[extractors] WARNING: no native extractor for {absent} - "
              f"set YTMAX_AUTOUPDATE_YTDLP=1 to refresh yt-dlp")


def _log_startup() -> None:
    print(f"[ffmpeg] {FFMPEG_VERSION}")
    print(f"[ffmpeg] binary: {FFMPEG_EXE} "
          f"(muxing {'available' if FFMPEG_AVAILABLE else 'UNAVAILABLE - quality will be capped!'})")
    print(f"[yt-dlp] version: {_ytdlp_version()}")
    if COOKIES_CONFIGURED:
        print(f"[cookies] configured -> {COOKIES_FILE}")
    else:
        print("[cookies] NOT configured - auth-walled videos will fail. Set YTMAX_COOKIES_FILE.")
    print(f"[network] proxy: {'configured' if PROXY_URL else 'direct'} | "
          f"sleep {SLEEP_INTERVAL}-{MAX_SLEEP_INTERVAL}s")


_load_persisted_state()
_cleanup_interrupted_downloads()
_log_startup()
_maybe_auto_update_ytdlp()
_audit_extractors()


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


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_EMBED_SRC_RE = re.compile(r"<(?:iframe|video|source|embed)[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_ANCHOR_HREF_RE = re.compile(r"<a[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.I)
_DATA_SRC_RE = re.compile(r"\bdata-(?:src|video-url|embed-url)\s*=\s*[\"']([^\"']+)[\"']", re.I)


# Wrapper / unblocker patterns: the real destination hides in a query param
# (`url=`, `target=`, `__cpo=`, `dest=`…) — often percent- or base64-encoded, and
# common to web-based unblockers broadly, not tied to any single service.
_PROXY_PARAM_KEYS = {
    "url", "uri", "u", "target", "dest", "destination", "link", "goto", "go",
    "redirect", "redirect_url", "redirect_uri", "redir", "r", "to", "out",
    "continue", "next", "view", "file", "video", "src", "source", "q", "query",
    "address", "addr", "__cpo", "cpo", "encoded_url", "encodedurl", "real_url",
    "actual_url", "decoded", "decode",
}
# Params that belong to the proxy session itself (tracking, service routing,
# or the encoded-destination slot) — never copied onto the unwrapped URL.
_PROXY_ONLY_PARAMS = _PROXY_PARAM_KEYS | {
    "__cpUrl", "noSheath", "parentUrl", "refererUrl", "viaUrl", "proxyUrl",
    "proxy_url", "proxy", "session", "sid", "sessionid", "session_id",
    "token", "auth", "org", "referrer", "ref",
}
# Ad/tracker params — dropped from the reconstructed URL wherever they appear
# (wrapper leftovers or riding along on the destination itself).
_TRACKING_PARAM_RE = re.compile(
    r"^(?:utm_[a-z0-9_]+|gclid|fbclid|msclkid|dclid|twclid|igshid|"
    r"mc_cid|mc_eid|_hsenc|_hsmi|vero_id|s_kwcid)$", re.I)
_MAX_UNWRAP_DEPTH = 4


def _as_absolute_url(text: str) -> Optional[str]:
    """Normalise a candidate into an absolute http(s) URL, or None."""
    text = html.unescape(text or "").strip().strip("\"'")
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    if "://" not in text:
        first = re.split(r"[/?#]", text, 1)[0]
        if "." not in first or " " in text:
            return None
        text = "https://" + text
    if not text.lower().startswith(("http://", "https://")):
        return None
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return text


def _b64_decode_maybe(value: str) -> Optional[str]:
    """Best-effort base64 (standard or URL-safe) decode into printable text."""
    if not value or len(value) < 8:
        return None
    candidate = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]+", candidate):
        return None
    padded = candidate + "=" * (-len(candidate) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded.strip() and decoded.isprintable():
            return decoded
    return None


def _decode_destination(value: str) -> tuple:
    """Decode a query value into ``(destination_url, was_encoded)``."""
    if not value:
        return None, False
    raw = html.unescape(value).strip().strip("\"'")
    encoded = "%3a" in raw.lower() or "%2f" in raw.lower()
    val = raw
    for _ in range(2):                 # peel double-encoding
        decoded = unquote(val)
        if decoded == val:
            break
        val = decoded
    b64 = _b64_decode_maybe(raw) or _b64_decode_maybe(val)
    if b64:
        encoded = True
    for cand in [val] + ([b64, unquote(b64)] if b64 else []):
        absolute = _as_absolute_url(cand)
        if absolute:
            return absolute, encoded
    return None, False


def _unwrap_proxy_url(url: str) -> Optional[str]:
    """If *url* is an unblocker/wrapper, return the destination URL inside it."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    pairs: List[tuple] = []
    for source in (parsed.query, parsed.fragment):
        if source:
            try:
                pairs.extend(parse_qsl(source, keep_blank_values=True))
            except ValueError:
                pass
    for key, val in pairs:
        candidate, was_encoded = _decode_destination(val)
        if not candidate or candidate == url:
            continue
        # Unwrap when the param is destination-like, or when the value was clearly
        # encoded — so a literal `?ref=https://partner` stays untouched.
        if key.lower() in _PROXY_PARAM_KEYS or was_encoded:
            return _rebuild_destination_url(url, key, candidate)
    # Some services hide the destination in a base64 path segment or fragment.
    for source in (parsed.path, parsed.fragment):
        for part in re.split(r"[/?&=]", source):
            if len(part) >= 12:
                candidate, was_encoded = _decode_destination(part)
                if candidate and candidate != url and was_encoded:
                    return _rebuild_destination_url(url, "", candidate)
    return None


def _rebuild_destination_url(wrapper_url: str, unwrap_key: str,
                             destination: str) -> str:
    """Re-attach the wrapper's *non-proxy* query params to the destination.

    A wrapper like ``https://185.x.x.x/watch?v=ID&__cpo=<encoded-youtube.com>``
    must reconstruct to ``https://youtube.com/watch?v=ID`` — not to a bare
    ``https://youtube.com``. When the decoded destination is a bare domain (no
    path of its own), the wrapper's path carries over too — those wrappers
    mirror the real path on the proxy host. Every original query param survives
    except proxy-control ones: the key that triggered the unwrap itself, other
    destination slots, obvious session/tracking params, and ad-tracker params
    (``utm_*``, ``gclid``…), which are dropped wherever they appear. Exact
    duplicate ``key=value`` pairs are de-duplicated. Values are double-unquoted
    because wrapper operators routinely double-encode them. The wrapper's
    fragment is carried over untouched.
    """
    try:
        wrapper = urlparse(wrapper_url)
        dest = urlparse(destination)
    except ValueError:
        return destination
    if not dest.scheme:
        return destination          # relative destination: nothing to merge onto
    banned = {unwrap_key.lower()} if unwrap_key else set()
    merged = parse_qsl(dest.query, keep_blank_values=True)
    seen = set(merged)
    for k, v in parse_qsl(wrapper.query, keep_blank_values=True):
        if k.lower() in _PROXY_ONLY_PARAMS or k.lower() in banned:
            continue
        # Values may arrive double-encoded (``%2520`` → ``%20``); unquote twice
        # so ``a%20b`` doesn't end up literal in the destination URL.
        v = unquote(unquote(v))
        if (k, v) in seen:          # destination already carries the same pair
            continue
        seen.add((k, v))
        merged.append((k, v))
    host = (dest.hostname or "").lower()
    merged = [(k, v) for k, v in merged
              if not _TRACKING_PARAM_RE.match(k)
              and not (k == "si" and host.endswith(("youtube.com", "youtu.be")))]
    path = dest.path
    if path in ("", "/") and wrapper.path not in ("", "/"):
        path = wrapper.path         # bare-domain destination: adopt wrapper path
    path = quote(path, safe="/%:@&+$,;=~*!'()[]-._")   # re-encode spaces etc.
    dest_query = urlencode(merged)
    if dest_query == dest.query and path == dest.path:
        return destination
    destination = urlunsplit((dest.scheme, dest.netloc, path, dest_query,
                              dest.fragment))
    if wrapper.fragment and not dest.fragment:
        destination = destination + "#" + wrapper.fragment
    return destination


def extract_url_from_input(raw: str, _depth: int = 0) -> Optional[str]:
    """Pull a usable URL out of *anything* a user pastes.

    Handles bare share links, raw page URLs, and embed codes / snippets —
    ``<iframe src="…">``, ``<video><source src="…">``, anchor tags, protocol
    relative ``//cdn…`` sources, and plain prose that merely contains a link.

    Also de-proxies unblocker/wrapper links: when the real destination hides in
    a query param (``url=``, ``target=``, ``__cpo=``, ``dest=``…, often percent-
    or base64-encoded), it decodes that URL and recursively re-extracts on it.
    """
    if not raw:
        return None
    text = html.unescape(raw).strip().strip("\"'")
    if not text:
        return None
    if "<" in text and ">" in text:
        for regex in (_EMBED_SRC_RE, _DATA_SRC_RE, _ANCHOR_HREF_RE):
            m = regex.search(text)
            if m and m.group(1).strip():
                text = m.group(1).strip()
                break
    else:
        m = _URL_IN_TEXT_RE.search(text)
        if m:
            text = m.group(0)
    text = html.unescape(text).strip().rstrip(".,;)\"'")
    absolute = _as_absolute_url(text)
    if not absolute:
        return None
    # De-proxy: peel wrapper/unblocker links down to the real destination URL.
    if _depth < _MAX_UNWRAP_DEPTH:
        unwrapped = _unwrap_proxy_url(absolute)
        if unwrapped and unwrapped != absolute:
            return extract_url_from_input(unwrapped, _depth + 1)
    return absolute


def normalize_media_url(raw: str) -> Optional[str]:
    """Accept any http(s) link, embed snippet or page URL; the engine decides support."""
    return extract_url_from_input(raw)


# ---------------------------------------------------------------------------
# Multi-input routing: embedded-media discovery + playlist/channel detection
# ---------------------------------------------------------------------------
_EMBED_VIDEO_HOSTS = (
    "youtube.com/embed/", "youtube-nocookie.com/embed/", "player.vimeo.com/video/",
    "dailymotion.com/embed/", "facebook.com/plugins/video", "twitch.tv/videos/",
    "streamable.com/e/", "rumble.com/embed/", "bitchute.com/embed/",
    "brighteon.com/embed/", "ok.ru/videoembed/", "vk.com/video_ext.php",
)


def is_playlist_or_channel_url(url: str) -> bool:
    """Batch inputs: YouTube playlists/channels plus generic playlist-ish pages."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    path = p.path
    query = p.query.lower()
    if "youtube.com" in host or "youtu.be" in host:
        if _is_yt_channel_url(url):
            return True
        if path.startswith("/playlist") or "list=" in query:
            return True
    if re.search(r"/(playlists?|sets|channel|c|user|@[^/]+)(/|$)", path, re.I):
        return True
    if "list=" in query or "playlist=" in query:
        return True
    return False


def _embedded_media_urls(page: Optional[str], page_url: str) -> List[str]:
    """Discover embedded video/manifest URLs on a raw web page.

    Covers iframe/player embeds, native <video>/<source> tags, lazy data-src
    attributes, and og:video/twitter:player meta tags — so a page whose primary
    content is an article still yields its embedded video.
    """
    if not page:
        return []
    found: List[str] = []

    def add(cand: str) -> None:
        if not cand:
            return
        cand = html.unescape(cand).strip()
        if not cand or cand.startswith(("data:", "blob:", "javascript:")):
            return
        if cand.startswith("//"):
            cand = "https:" + cand
        if not cand.startswith("http"):
            try:
                cand = urljoin(page_url, cand)
            except ValueError:
                return
        if not cand.startswith("http"):
            return
        lowered = cand.lower()
        if (
            any(h in lowered for h in _EMBED_VIDEO_HOSTS)
            or re.search(r"\.(m3u8|mpd)(\?|#|$)", lowered)
            or re.search(r"\.(mp4|webm|m4v|mov|mkv)(\?|#|$)", lowered)
        ):
            if cand not in found:
                found.append(cand)

    patterns = (
        re.compile(r"<(?:iframe|video|source|embed)[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I),
        re.compile(r"\bdata-(?:src|video-url|embed-url)\s*=\s*[\"']([^\"']+)[\"']", re.I),
        re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::(?:url|secure_url))?|twitter:player)["\'][^>]+content=["\']([^"\']+)', re.I),
        re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:video(?::(?:url|secure_url))?|twitter:player)["\']', re.I),
    )
    for regex in patterns:
        for m in regex.finditer(page):
            add(m.group(1))
    return found


def _clean_error(exc: Exception, cookies_configured: Optional[bool] = None) -> str:
    """Human, actionable errors. Distinguishes the auth-wall class from the
    IP-block/bot-check class, because the two need different fixes."""
    msg = str(exc).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    lowered = msg.lower()
    if cookies_configured is None:
        cookies_configured = COOKIES_CONFIGURED

    if "is not a valid url" in lowered or "unsupported url" in lowered:
        return "Unsupported or invalid link — the press could not read it. Paste a media page URL, a share link, or an embed snippet."

    # ---- IP-block / bot-check class (NOT fixed by cookies) ------------------
    if any(k in lowered for k in (
        "confirm you're not a bot", "confirm you are not a bot", "not a bot", "bot check",
    )):
        return (
            "YouTube asked this server to prove it is not a bot (a datacenter-IP block, "
            "which is separate from the login wall). Fix it one of two ways: (a) attach "
            "cookies.txt via YTMAX_COOKIES_FILE, and/or (b) route traffic through a "
            "residential proxy with YTMAX_PROXY. See docs/RENDER_DEPLOY.md → "
            "“Anti-bot & IP blocks”."
        )

    # ---- Auth-wall class (login / members-only / subscription / age) --------
    auth_markers = (
        "login required", "log in", "sign in", "requires a login", "logged-in",
        "authentication", "members-only", "members only", "join this channel",
        "requires payment", "paid members", "premium", "subscription",
        "age-restricted", "age restricted", "confirm your age", "this video requires",
    )
    if any(k in lowered for k in auth_markers):
        if not cookies_configured:
            return (
                "This video requires a signed-in session (login / members-only / "
                "subscription / age-restricted) and this server has NO cookies configured, "
                "so it is being turned away. Export cookies.txt from a logged-in browser and "
                "set YTMAX_COOKIES_FILE (on Render: add it as a Secret File). Full steps: "
                "docs/RENDER_DEPLOY.md → “Unlock authenticated videos”."
            )
        return (
            "This video requires a signed-in session and the configured cookies were "
            "rejected — they may be expired, from the wrong account, or missing this "
            "site. Re-export cookies.txt and update YTMAX_COOKIES_FILE / the Secret File."
        )

    if "video unavailable" in lowered or "not available" in lowered:
        return "Media unavailable — it may be private, region-locked, or removed."
    if "private video" in lowered or "private post" in lowered:
        return "This media is private and cannot be downloaded."
    if "empty media response" in lowered or "no media found" in lowered or "no media" in lowered:
        return "No downloadable media found here — the post may need a logged-in session (configure cookies, see docs/RENDER_DEPLOY.md) or may have been removed."
    if "no video formats found" in lowered:
        return "This post has no video stream — if it is an image post, the images are offered automatically. Otherwise the source may need cookies (see docs/RENDER_DEPLOY.md)."
    if "no video could be found" in lowered:
        return "This X post has no extractable video stream. If it is a photo post, the images are offered automatically — otherwise videos may need cookies (see docs/RENDER_DEPLOY.md)."
    if "http error 404" in lowered:
        return "That file no longer exists at this address (404) — try a fresh link."
    if "http error 403" in lowered or "forbidden" in lowered:
        return "The source refused the request (403) — this media likely needs a logged-in session (configure cookies, see docs/RENDER_DEPLOY.md)."
    if "http error 429" in lowered or "too many requests" in lowered:
        return "The source is rate-limiting this server's IP — wait a few minutes, or set YTMAX_PROXY to route around a blocked IP range."
    if any(k in lowered for k in ("connection aborted", "connection reset", "timed out", "connection refused")):
        return "The source refused the connection right now — the server's network path to this site was rejected. Try again, or set YTMAX_PROXY to route through a different IP range."
    if any(k in lowered for k in ("drm", "playready", "widevine", "fairplay")):
        return "DRM-protected stream — copy-protected content cannot be saved."
    if "unsupported audio format" in lowered or "unsupported video format" in lowered:
        return "That media format is not supported — for audio pick MP3, M4A, WAV or FLAC."
    if "unsupported" in lowered:
        return "This site or link type is not supported yet. Try a direct media page URL or an embed snippet."
    if len(msg) > 420:
        msg = msg[:420] + "…"
    return msg


def _is_permanent_error(msg: str) -> bool:
    """Errors that retrying cannot fix (retry only wastes time and bandwidth)."""
    lowered = msg.lower()
    return any(k in lowered for k in (
        "unsupported", "drm", "private", "no longer exists at this address",
        "requires a signed-in session", "no cookies configured", "invalid link",
    ))


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
    """Raw page fetch with the same client-persona ladder as yt-dlp: browser
    persona first (curl_cffi), plain urllib fallback (proxy-compatible)."""
    r = _curl_get(url, timeout)
    try:
        if r is not None and r.status_code < 400:
            return r.text[:max_bytes]
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        with _URL_OPENER.open(req, timeout=timeout) as resp:
            return resp.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return None


def _probe_url(url: str, timeout: float = 6.0) -> bool:
    """Cheap reachability check (HEAD, then GET fallback)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": DEFAULT_UA})
        with _URL_OPENER.open(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with _URL_OPENER.open(req, timeout=timeout) as resp:
                return resp.status < 400
        except Exception:
            return False


def _is_placeholder_image(url: str) -> bool:
    """Skip generic logos / default avatars that og:image sometimes points at."""
    lowered = url.lower()
    markers = (
        "abs.twimg.com/rweb/ssr/default",   # X/Twitter logo
        "youtube.com/img/",                  # YouTube logo
        "rsrc.php",                          # Facebook sprite/placeholder
        "static.licdn.com/sc/h/",            # LinkedIn logo
        "instagram.com/static/",             # IG default avatar
        "default_profile",                   # generic default avatar
        "spacer.gif", "data:image", "pixel.gif",
        "placeholder", "avatar-default",
    )
    return any(m in lowered for m in markers)


def _og_image_url(html_text: Optional[str]) -> Optional[str]:
    if not html_text:
        return None
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html_text)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text)
    if not m:
        m = re.search(r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)', html_text)
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


def _cookies_configured(cookies_browser: Optional[str] = None) -> bool:
    """True when *some* authenticated session (browser profile or cookies.txt) is available."""
    return bool(cookies_browser) or COOKIES_CONFIGURED


def _cookies_opts(cookies_browser: Optional[str]) -> Dict[str, Any]:
    """Attach an authenticated session to yt-dlp.

    Priority:
      1. an explicit browser profile (local machines only — production has no browser), then
      2. the Netscape ``cookies.txt`` configured via ``YTMAX_COOKIES_FILE`` / Secret File.

    The cookie file is what makes a deployed instance behave exactly like a local
    one for login-walled, age-restricted and members-only videos.
    """
    opts: Dict[str, Any] = {}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    return opts


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
        "fallback": True,
        "video_formats": [],
        "audio_formats": [],
        "images": images,
        "image_count": len(images),
    }


def _pinterest_result(url: str, pin_id: str, title: str, uploader: str, image: Dict[str, Any]) -> Dict[str, Any]:
    """Synthetic analyze result for a single Pinterest pin image."""
    return {
        "url": url,
        "title": title,
        "duration": 0,
        "duration_str": "Image",
        "thumbnail": image["url"],
        "uploader": uploader,
        "view_count": 0,
        "view_count_str": "N/A",
        "is_live": False,
        "source": "PINTEREST",
        "extractor_key": "Pinterest",
        "highest_res_height": 0,
        "highest_res_label": "Pin image",
        "media_kind": "images",
        "is_profile": False,
        "has_video": False,
        "has_audio": False,
        "fallback": True,
        "video_formats": [],
        "audio_formats": [],
        "images": [image],
        "image_count": 1,
    }


def _generic_page_image_fallback(url: str) -> Optional[Dict[str, Any]]:
    """Universal last resort: when the extractor finds no media, grab the page's
    og:image / twitter:image (embedded on nearly every news & social page) so
    the press still has an image to hand over. Placeholder logos are skipped."""
    if not url.startswith("http"):
        return None
    page = _http_get(url, max_bytes=1_200_000)
    if not page:
        return None
    image_url = _og_image_url(page)
    if not image_url or _is_placeholder_image(image_url):
        # og:image absent or a placeholder logo — try twitter:image before giving up.
        m = re.search(r'<meta[^>]+name=["\']twitter:image(?:src)?["\'][^>]+content=["\']([^"\']+)', page)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?:src)?["\']', page)
        if m:
            image_url = html.unescape(m.group(1)).strip()
    if not image_url or not image_url.startswith("http") or _is_placeholder_image(image_url):
        return None

    host = (urlparse(url).hostname or "").lower().replace("www.", "")
    source_label = host.split(".")[0].upper() if "." in host else "WEB"
    title = _og_title(page) or (host or "Web page")
    image = {
        "id": "page-image",
        "label": "Page image · best available",
        "kind": "post_image",
        "url": image_url,
        "width": None,
        "height": None,
        "ext": _ext_of(image_url),
    }
    return {
        "url": url,
        "title": title,
        "duration": 0,
        "duration_str": "Image",
        "thumbnail": image_url,
        "uploader": host or "Web",
        "view_count": 0,
        "view_count_str": "N/A",
        "is_live": False,
        "source": source_label,
        "extractor_key": "Generic",
        "highest_res_height": 0,
        "highest_res_label": "Page image",
        "media_kind": "images",
        "is_profile": False,
        "has_video": False,
        "has_audio": False,
        "fallback": True,
        "video_formats": [],
        "audio_formats": [],
        "images": [image],
        "image_count": 1,
    }


def _pinterest_image_fallback(url: str) -> Optional[Dict[str, Any]]:
    """Image-only Pinterest pins: yt-dlp raises 'No video formats found!' before
    returning anything, so ask Pinterest's own Pin API for the image (the same
    endpoint the extractor uses — the full-res 'images' variants are in there)."""
    host = (urlparse(url).hostname or "").lower()
    if "pinterest" not in host:
        return None
    m = re.search(r"/pin/(?:[\w-]+--)?(\d+)", url)
    if not m:
        return None
    pin_id = m.group(1)
    options = {"field_set_key": "unauth_react_main_pin", "id": pin_id}
    qs = urlencode({"data": json.dumps({"options": options})})
    api_url = f"https://www.pinterest.com/resource/PinResource/get/?{qs}"
    try:
        req = urllib.request.Request(api_url, headers={
            "User-Agent": DEFAULT_UA,
            "X-Pinterest-PWS-Handler": "www/[username].js",
        })
        with _URL_OPENER.open(req, timeout=20) as resp:
            payload = json.loads(resp.read(1_500_000).decode("utf-8", "replace"))
    except Exception:
        return None
    pin = (payload or {}).get("resource_response", {}).get("data") or {}
    if not isinstance(pin, dict):
        return None

    best_url, best_area, best_w, best_h = None, 0, 0, 0
    for v in (pin.get("images") or {}).values():
        if not isinstance(v, dict):
            continue
        u = (v.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        try:
            w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        area = (w or 0) * (h or 0)
        if area >= best_area:
            best_url, best_area, best_w, best_h = u, area, w, h
    if not best_url:
        return None

    title = pin.get("title") or pin.get("grid_title") or ""
    title = title.strip() if isinstance(title, str) else ""
    title = title or f"Pinterest pin {pin_id}"
    attribution = pin.get("closeup_attribution")
    uploader = attribution.get("full_name") if isinstance(attribution, dict) else None
    dims = f" · {best_w}×{best_h}" if best_w and best_h else ""
    image = {
        "id": "pin-image",
        "label": f"Pin image · full resolution{dims}",
        "kind": "post_image",
        "url": best_url,
        "width": best_w or None,
        "height": best_h or None,
        "ext": _ext_of(best_url),
    }
    return _pinterest_result(url, pin_id, title, uploader or "Pinterest", image)


def _pinterest_page_fallback(url: str) -> Optional[Dict[str, Any]]:
    """Backup for image-only pins when Pinterest's Pin API is rate-limited:
    scrape the pin page for the embedded full-res 'originals' CDN URL.
    The pin page always repeats the pin's own image hash at every size, so the
    most common hash wins and its embedded originals URL is used as-is."""
    host = (urlparse(url).hostname or "").lower()
    if "pinterest" not in host:
        return None
    m = re.search(r"/pin/(?:[\w-]+--)?(\d+)", url)
    if not m:
        return None
    pin_id = m.group(1)
    page = _http_get(url, max_bytes=1_500_000)
    if not page:
        return None

    # Dead / removed pins: Pinterest serves a shell page that may embed a random
    # related pin's image. Real pin pages ALWAYS carry og:title + og:url pointing
    # at the pin — reject anything that lacks them or points elsewhere.
    title_probe = _og_title(page) or ""
    if not title_probe:
        return None
    if re.search(r"not found|doesn't exist|does not exist|\b404\b|page removed|was removed|unavailable", title_probe, re.I):
        return None
    og_url_m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)', page)
    if not og_url_m:
        og_url_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']', page)
    if og_url_m:
        og_url_val = html.unescape(og_url_m.group(1)).strip()
        if og_url_val and pin_id not in og_url_val:
            return None

    # Full-res 'originals' URLs embedded in the page (they carry the true extension).
    originals = sorted(set(re.findall(
        r"i\.pinimg\.com/originals/((?:[a-f0-9]{2}/){3}[a-f0-9]{32})\.(png|jpe?g|webp|gif)", page)))
    # How often each image hash appears — the pin's own image repeats at every size.
    hashes = re.findall(
        r"i\.pinimg\.com/[a-z0-9x_]+/((?:[a-f0-9]{2}/){3}[a-f0-9]{32})\.", page)
    top_hash = Counter(hashes).most_common(1)[0][0] if hashes else None

    # Only trust an originals URL whose hash is also the pin's own image (it always
    # repeats across the sized variants). Never grab a lexicographically-first
    # avatar or related-pin image.
    hash_set = set(hashes)
    best_url = None
    if originals:
        for hsh, ext in originals:
            if hsh in hash_set and (top_hash is None or hsh == top_hash):
                best_url = f"https://i.pinimg.com/originals/{hsh}.{ext}"
                break

    dims = None
    if not best_url:
        # No trusted originals: pick the largest sized variant of the main image.
        sized = sorted(set(re.findall(
            r"i\.pinimg\.com/([a-z0-9x_]+)/((?:[a-f0-9]{2}/){3}[a-f0-9]{32})\.(png|jpe?g|webp)", page)))

        def token_val(t: str) -> int:
            mm = re.match(r"(\d+)x", t)
            return int(mm.group(1)) if mm else 0

        if sized:
            cands = [s for s in sized if s[1] == top_hash] or sized
            size, hsh, ext = max(cands, key=lambda s: token_val(s[0]))
            best_url = f"https://i.pinimg.com/{size}/{hsh}.{ext}"
            mm = re.match(r"(\d+)x(\d+)", size)
            if mm:
                dims = (int(mm.group(1)), int(mm.group(2)))
    if not best_url:
        return None

    title = _og_title(page) or ""
    if "|" in title:
        title = "|".join(title.split("|")[:-1]).strip()  # drop Pinterest's appended blurb
    title = title.strip() or f"Pinterest pin {pin_id}"
    label = "Pin image · full resolution" + (f" · {dims[0]}×{dims[1]}" if dims else "")
    image = {
        "id": "pin-image",
        "label": label,
        "kind": "post_image",
        "url": best_url,
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "ext": _ext_of(best_url),
    }
    return _pinterest_result(url, pin_id, title, "Pinterest", image)


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
    mode: str = "video"  # "video" | "image" — what the user is hunting for


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
    opts.update(_network_opts())
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


def _analyze_playlist(url: str, req: AnalyzeRequest) -> Optional[Dict[str, Any]]:
    """Batch listing for a playlist / channel URL (per-item download happens later)."""
    opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "extract_flat": "in_playlist",   # cheap listing; full extraction happens per item
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    opts.update(_network_opts())
    opts.update(_cookies_opts(req.cookies_browser))
    try:
        info = _extract_with_resilience(opts, url, download=False)
    except Exception:
        return None
    if not info or info.get("_type") != "playlist":
        return None

    entries: List[Dict[str, Any]] = []
    for i, e in enumerate(info.get("entries") or [], start=1):
        if not isinstance(e, dict):
            continue
        eurl = e.get("url") or e.get("webpage_url") or ""
        if not eurl.startswith("http"):
            # Flat extraction often yields a bare video id (YouTube) — rebuild a URL.
            vid = str(e.get("id") or eurl or "")
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                eurl = f"https://www.youtube.com/watch?v={vid}"
            else:
                eurl = e.get("webpage_url") or ""
        if not eurl.startswith("http"):
            continue
        dur = e.get("duration")
        entries.append({
            "index": i,
            "url": eurl,
            "title": e.get("title") or f"Item {i}",
            "thumbnail": e.get("thumbnail") or "",
            "duration": dur or 0,
            "duration_str": format_eta(dur) if dur else "--:--",
            "uploader": e.get("uploader") or e.get("channel") or "",
        })
    if not entries:
        return None

    view_count = info.get("view_count")
    art: List[Dict[str, Any]] = _yt_channel_art(info) if _is_yt_channel_url(url) else []
    return {
        "url": url,
        "title": info.get("title") or "Playlist",
        "duration": 0,
        "duration_str": f"{len(entries)} items",
        "thumbnail": info.get("thumbnail") or entries[0]["thumbnail"],
        "uploader": info.get("uploader") or info.get("channel") or "Playlist",
        "view_count": view_count or 0,
        "view_count_str": f"{view_count:,}" if view_count else "N/A",
        "is_live": False,
        "source": _pretty_source(info.get("extractor_key") or "", info.get("extractor") or "", info),
        "extractor_key": info.get("extractor_key") or "",
        "highest_res_height": 0,
        "highest_res_label": f"{len(entries)} item playlist",
        "media_kind": "playlist",
        "is_profile": False,
        "is_playlist": True,
        "has_video": True,
        "has_audio": True,
        "video_formats": [],
        "audio_formats": [],
        "images": art,
        "image_count": len(art),
        "playlist_count": len(entries),
        "entries": entries,
    }


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

    hunt = req.mode if req.mode in ("video", "image") else "video"

    # Playlists / channels → batch listing (batch extraction).
    if hunt == "video" and is_playlist_or_channel_url(url):
        playlist = _analyze_playlist(url, req)
        if playlist:
            return playlist

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
    ydl_opts.update(_network_opts())
    ydl_opts.update(_cookies_opts(req.cookies_browser))

    def _try_extract(extract_opts: Dict[str, Any], target_url: str):
        """Resilient extraction of one candidate plus the per-site transient
        retries: Reddit-style 429 backoff and the Vimeo embed-endpoint fallback.
        Returns ``(info, first_error)`` — exactly one is usable."""
        try:
            return _extract_with_resilience(extract_opts, target_url, download=False), None
        except Exception as e:
            first_error = e

        retried_info = None

        # Reddit's public JSON endpoint rate-limits per-IP; one short backoff
        # retry clears most transient 429s instead of failing outright.
        if "429" in str(first_error) or "too many requests" in str(first_error).lower():
            time.sleep(4)
            try:
                retried_info = _extract_with_resilience(extract_opts, target_url, download=False)
            except Exception:
                retried_info = None

        # Vimeo: the main vimeo.com/{id} page enforces session/password auth
        # that the player.vimeo.com embed endpoint often does not — retry
        # through the embed URL before giving up on a real video result.
        if retried_info is None:
            host = (urlparse(target_url).hostname or "").lower()
            if "vimeo.com" in host and "player.vimeo.com" not in host:
                vid_match = re.search(r"vimeo\.com/(?:video/)?(\d+)", target_url)
                if vid_match:
                    embed_url = f"https://player.vimeo.com/video/{vid_match.group(1)}"
                    try:
                        retried_info = _extract_with_resilience(extract_opts, embed_url, download=False)
                    except Exception:
                        retried_info = None

        if retried_info is not None:
            return retried_info, None
        return None, first_error

    # Multi-input routing: try the URL itself first; if it is a raw web page or an
    # embed wrapper, fall back to the media embedded inside it (iframe / <video> /
    # og:video / manifest) — so a video that is not the page's primary content still
    # resolves, and a whole embed snippet resolves to its player URL.
    pending: List[str] = [url]
    seen_candidates: set = set()
    info: Optional[Dict[str, Any]] = None
    last_error: Optional[Exception] = None

    while pending and info is None:
        candidate = pending.pop(0)
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        cand_is_profile = _is_yt_channel_url(candidate) or _is_profile_url(candidate)
        cand_opts = dict(ydl_opts)
        if cand_is_profile:
            # Profile pages are big playlists — one entry is enough to characterise them.
            cand_opts["playlist_items"] = "1"
        info, cand_error = _try_extract(cand_opts, candidate)
        if info:
            url = candidate
            is_profile_guess = cand_is_profile
            break
        info = None
        last_error = cand_error or RuntimeError("Media information could not be retrieved.")
        if len(seen_candidates) < 4:
            for embedded in _embedded_media_urls(_http_get(candidate), candidate)[:3]:
                if embedded not in seen_candidates and embedded not in pending:
                    pending.append(embedded)

    if not info:
        e = last_error or RuntimeError("Media information could not be retrieved.")
        if is_profile_guess and profile_art:
            return _profile_only_result(url, profile_art, profile_page)

        fallback = (
            _post_image_fallback(url)
            or _pinterest_image_fallback(url)
            or _pinterest_page_fallback(url)
            or _generic_page_image_fallback(url)
        )
        # Only pass off a silent image swap when the source genuinely has
        # no video (a real image post). Auth walls, bot checks, rate
        # limits and impersonation failures get surfaced as real errors
        # instead of being disguised as a successful image result.
        genuinely_image_only = any(
            k in str(e).lower()
            for k in ("no video formats found", "no video could be found", "no media found")
        )
        if fallback and genuinely_image_only:
            return fallback
        if fallback:
            fallback["fallback_reason"] = _clean_error(e, _cookies_configured(req.cookies_browser))
            return fallback
        raise HTTPException(
            status_code=500,
            detail=_clean_error(e, _cookies_configured(req.cookies_browser)),
        )

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

    # Mode-aware image hunt: in IMAGE MODE keep digging for an image whenever the
    # extraction gave none; in VIDEO MODE also rescue a page with no video at all
    # ("video not found → still bring the image").
    if not images and (hunt == "image" or not sorted_video_formats):
        generic = _generic_page_image_fallback(url)
        if generic:
            images = generic.get("images") or []

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
        "duration_str": format_eta(duration) if duration else ("Live Stream" if is_live else "--:--"),
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


def _set_status(task_id: str, status: str, message: str) -> None:
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = status
            task["status_msg"] = message


def execute_download(task_id: str, req: DownloadRequest) -> None:
    task_dir = os.path.join(STAGING_ROOT, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
        if req.kind in ("direct", "images"):
            if req.kind == "direct":
                _run_direct_download(task_id, req, task_dir)
            else:
                _run_multi_image_download(task_id, req, task_dir)
        elif req.kind == "playlist":
            _run_playlist_download(task_id, req, task_dir)
        else:
            # Retry chain with exponential backoff; permanent failures short-circuit.
            last_exc: Optional[Exception] = None
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    _run_download(task_id, req, task_dir)
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    err = _clean_error(e, _cookies_configured(req.cookies_browser))
                    # Artwork/metadata embedding fails on some odd containers — drop it and retry.
                    if _is_embed_error(err) and (req.embed_thumbnail or req.embed_metadata):
                        _set_status(task_id, "downloading", "Retrying without artwork embedding…")
                        req = req.model_copy(update={"embed_thumbnail": False, "embed_metadata": False})
                        continue
                    if _is_permanent_error(err) or attempt == max_attempts:
                        raise
                    delay = min(8, 2 ** (attempt - 1))
                    _set_status(
                        task_id, "downloading",
                        f"Transient error — retrying in {delay}s (attempt {attempt + 1}/{max_attempts})…",
                    )
                    time.sleep(delay)
            if last_exc is not None:
                raise last_exc
    except Exception as e:
        err = _clean_error(e, _cookies_configured(req.cookies_browser))
        with _lock:
            task = tasks.get(task_id)
            if task:
                task["status"] = "failed"
                task["error"] = err
        return

    _finalize(task_id, task_dir, req)


def _manifest_url_from_info(info: Optional[Dict[str, Any]]) -> Optional[str]:
    """Find the HLS/DASH manifest URL inside a yt-dlp info dict."""
    if not info:
        return None
    mu = info.get("manifest_url")
    if isinstance(mu, str) and mu.startswith("http"):
        return mu
    for f in info.get("formats") or []:
        proto = (f.get("protocol") or "").lower()
        u = f.get("url") or ""
        if not u.startswith("http"):
            continue
        ul = u.lower()
        if "m3u8" in proto or ".m3u8" in ul or "dash" in proto or ".mpd" in ul:
            return u
    return None


def _manifest_url_from_page(url: str) -> Optional[str]:
    """Fall-back discovery: scrape a page for an .m3u8 / .mpd reference."""
    page = _http_get(url, max_bytes=2_000_000)
    if not page:
        return None
    m = re.search(r"https?://[^\s\"'\\]+\.(?:m3u8|mpd)(?:\?[^\s\"'\\]*)?", page)
    if m:
        return m.group(0).replace("\\/", "/")
    m = re.search(r"[^\s\"'\\/]+\.(?:m3u8|mpd)(?:\?[^\s\"'\\]*)?", page)
    return html.unescape(m.group(0)).replace("\\/", "/") if m else None


def _cookie_header_for(url: str) -> str:
    """Best-effort Cookie header from cookies.txt, for ffmpeg's manifest fetch."""
    if not COOKIES_FILE:
        return ""
    host = (urlparse(url).hostname or "").lower()
    pairs: List[str] = []
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, _path, _secure, _expiry, name, value = parts[:7]
                dom = domain.lstrip(".").lower()
                if host == dom or host.endswith("." + dom):
                    pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def _download_via_manifest(task_id: str, req: DownloadRequest, task_dir: str) -> Optional[str]:
    """Secondary extraction path: fetch the HLS/DASH manifest and stitch it with
    ffmpeg (`-c copy`). Used when yt-dlp's own downloader fails outright."""
    if req.audio_only:
        return None
    probe_opts: Dict[str, Any] = {
        "ffmpeg_location": FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    probe_opts.update(_network_opts())
    probe_opts.update(_cookies_opts(req.cookies_browser))
    manifest: Optional[str] = None
    try:
        manifest = _manifest_url_from_info(
            _extract_with_resilience(probe_opts, req.url, download=False))
    except Exception:
        manifest = None
    if not manifest:
        manifest = _manifest_url_from_page(req.url)
    if not manifest:
        return None
    if not FFMPEG_AVAILABLE:
        return None

    _set_status(task_id, "downloading", "Primary extractor failed — stitching the manifest with FFmpeg…")
    out_path = os.path.join(task_dir, "manifest-fallback.mp4")
    cmd = [FFMPEG_EXE, "-hide_banner", "-loglevel", "error", "-y", "-user_agent", DEFAULT_UA]
    cookie_header = _cookie_header_for(req.url)
    if cookie_header:
        cmd += ["-headers", f"Cookie: {cookie_header}\r\n"]
    if req.referer:
        cmd += ["-referer", req.referer]
    cmd += ["-i", manifest, "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None
    return out_path


def _run_playlist_download(task_id: str, req: DownloadRequest, task_dir: str) -> None:
    """Batch download of a playlist / channel: every item, into one staging dir."""
    _set_status(task_id, "downloading", "Fetching every item in the playlist…")
    opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps, req.cookies_browser)
    opts["noplaylist"] = False
    opts["ignoreerrors"] = True          # one dead item must not kill the whole batch
    opts["outtmpl"] = os.path.join(task_dir, "%(playlist_index)s - %(title)s [%(format_id)s].%(ext)s")
    if req.audio_only:
        audio_fmt = (req.audio_format or "mp3").lower()
        if audio_fmt not in ALLOWED_AUDIO_FORMATS:
            raise RuntimeError(f"Unsupported audio format: {audio_fmt}")
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_fmt,
            "preferredquality": "320" if audio_fmt == "mp3" else "0",
        }]
    else:
        opts["format"] = DEFAULT_VIDEO_FORMAT
        opts["merge_output_format"] = "mp4"
    _extract_with_resilience(opts, req.url, download=True)


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
    precheck_opts.update(_network_opts())
    precheck_opts.update(_cookies_opts(req.cookies_browser))
    info = _extract_with_resilience(precheck_opts, req.url, download=False)
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
            # Manifest-first: best video + best audio as separate streams, muxed by ffmpeg.
            opts["format"] = DEFAULT_VIDEO_FORMAT
        opts["merge_output_format"] = "mp4"
        if req.embed_metadata:
            opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]

    try:
        _extract_with_resilience(opts, req.url, download=True)
    except Exception:
        # Primary extraction failed → secondary method: manifest + ffmpeg stitch.
        if _download_via_manifest(task_id, req, task_dir):
            return
        raise


def _run_direct_download(task_id: str, req: DownloadRequest, task_dir: str) -> None:
    """Download a single direct media URL (image / thumbnail / avatar) at full speed.

    Uses a plain HTTP stream instead of yt-dlp — some CDNs (Wikimedia, …) refuse
    yt-dlp's downloader with a 403 even though a normal browser fetch works.
    Honours the optional referer and the speed throttle."""
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
            task["status"] = "downloading"
            task["status_msg"] = "Fetching image at full resolution…"

    if req.cookies_browser:
        # Login-walled direct media needs the browser session — the plain HTTP
        # fetch can't send cookies, so use yt-dlp's cookie-aware downloader.
        opts = _build_common_opts(task_id, req.concurrency, req.rate_limit_mbps, req.cookies_browser)
        opts["outtmpl"] = os.path.join(task_dir, "%(title)s.%(ext)s")
        opts["writethumbnail"] = False
        info = {
            "id": task_id[:8],
            "title": title,
            "url": url,
            "ext": ext,
            "http_headers": headers,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_ie_result(info, download=True)
        return

    rate_bytes = max(0, int((req.rate_limit_mbps or 0) * 1024 * 1024))
    out_path = os.path.join(task_dir, f"{title}.{ext}")
    try:
        http_req = urllib.request.Request(url, headers=headers)
        with _URL_OPENER.open(http_req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with _lock:
                task = tasks.get(task_id)
                if task:
                    task["total_bytes"] = total
                    task["total_str"] = format_bytes(total) if total else "Dynamic"
            downloaded = 0
            with open(out_path, "wb") as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if rate_bytes > 0:
                        time.sleep(len(chunk) / rate_bytes)
                    with _lock:
                        task = tasks.get(task_id)
                        if task:
                            task["downloaded_bytes"] = downloaded
                            task["downloaded_str"] = format_bytes(downloaded)
                            task["status_msg"] = f"Fetching image… {format_bytes(downloaded)}"
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP Error {e.code}: {e.reason}") from e


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
                "filepath": final_path,  # survives restarts so Save/Open-folder keep working
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


def _finalize_playlist(task_id: str, task_dir: str, req: DownloadRequest) -> None:
    """Move every downloaded playlist item into Downloads and log the batch."""
    files = _find_output_files(task_dir)
    if not files:
        _fail_task(task_id, "No playlist items could be downloaded.")
        return
    saved: List[str] = []
    total_bytes = 0
    for path in files:
        try:
            size = os.path.getsize(path)
            dest = _move_to_downloads(path)
        except OSError:
            continue
        saved.append(os.path.basename(dest))
        total_bytes += size
    if not saved:
        _fail_task(task_id, "Playlist items could not be saved to your Downloads folder.")
        return

    quality = ("Audio · " + (req.audio_format or "mp3").upper()) if req.audio_only \
        else f"Playlist · {len(saved)} items"
    with _lock:
        task = tasks.get(task_id)
        if task:
            task["status"] = "completed"
            task["percentage"] = 100.0
            task["filename"] = saved[0]
            task["file_size"] = total_bytes
            task["file_size_str"] = format_bytes(total_bytes)
            task["status_msg"] = f"Playlist complete — {len(saved)} files saved to your PC!"
            task["playlist_saved"] = len(saved)
        stats["downloads"] += len(saved)
        stats["total_bytes"] += total_bytes
        if not req.incognito:
            history.insert(0, {
                "task_id": task_id,
                "title": (task or {}).get("title") or f"Playlist ({len(saved)} items)",
                "filename": saved[0],
                "filepath": os.path.join(DOWNLOADS_DIR, saved[0]),
                "file_size_str": format_bytes(total_bytes),
                "thumbnail": (task or {}).get("thumbnail", ""),
                "kind": "playlist",
                "audio_only": req.audio_only,
                "quality": quality,
                "source": (task or {}).get("source_label", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            history[:] = history[:50]
            _record_download_day()
    _persist_state()
    try:
        os.rmdir(task_dir)
    except OSError:
        pass


def _finalize(task_id: str, task_dir: str, req: DownloadRequest) -> None:
    if req.kind in ("direct", "images"):
        _finalize_images(task_id, task_dir, req)
        return
    if req.kind == "playlist":
        _finalize_playlist(task_id, task_dir, req)
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

    if req.kind not in ("video", "audio", "direct", "images", "playlist"):
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


def _find_task_filepath(task_id: str) -> str:
    """Resolve a completed file's path, surviving server restarts: first the live
    task dict, then the persisted history ledger (which stores the real path)."""
    with _lock:
        task = tasks.get(task_id)
        if task:
            return task.get("filepath") or ""
    with _lock:
        for item in history:
            if item.get("task_id") == task_id:
                fp = item.get("filepath") or ""
                if fp and os.path.exists(fp):
                    return fp
                fname = item.get("filename")
                if fname:
                    candidate = os.path.join(DOWNLOADS_DIR, fname)
                    if os.path.exists(candidate):
                        return candidate
    return ""


@app.get("/api/file/{task_id}")
def get_file(task_id: str):
    filepath = _find_task_filepath(task_id)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=400, detail="File is not ready for download yet.")
    return FileResponse(path=filepath, filename=os.path.basename(filepath), media_type="application/octet-stream")


@app.post("/api/open-folder/{task_id}")
def open_download_folder(task_id: str):
    """Open the containing folder in the OS file explorer (local personal use)."""
    filepath = _find_task_filepath(task_id)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="No saved file for this task.")
    folder = os.path.dirname(filepath)
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
            "ffmpeg_version": FFMPEG_VERSION,
            "ffmpeg_available": FFMPEG_AVAILABLE,
            "ytdlp_version": _ytdlp_version(),
            "cookies_configured": COOKIES_CONFIGURED,
            "proxy_configured": bool(PROXY_URL),
            "sleep_interval": [SLEEP_INTERVAL, MAX_SLEEP_INTERVAL],
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
