"""STREAM PRESS — Universal Personal-Use Media Downloader (launcher).

Starts the local service on 127.0.0.1 and opens the web interface.
Strictly personal, non-commercial use. See the in-app Legal & Fair Use terms.
"""

import argparse
import os
import subprocess
import sys
import threading
import webbrowser

# Windows consoles often use cp1252 which cannot encode emoji/unicode glyphs.
# Force UTF-8 so the launcher never crashes while printing status lines.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def banner() -> None:
    print("=" * 64)
    print("  STREAM PRESS  ·  Universal Personal-Use Media Downloader")
    print("  Local service · no telemetry · binds to 127.0.0.1 only")
    print("  YouTube, Instagram, X, Facebook, TikTok, Pinterest & 1,000+ more")
    print("  For personal offline viewing / educational research (Fair Use)")
    print("=" * 64)


def ensure_dependencies() -> None:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import yt_dlp   # noqa: F401
        import imageio_ffmpeg  # noqa: F401
    except ImportError:
        print("[install] Installing required Python dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "server/requirements.txt"])


def open_browser_later(url: str, delay: float = 1.6) -> None:
    timer = threading.Timer(delay, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="STREAM PRESS launcher")
    parser.add_argument("--port", type=int, default=8000, help="port to bind (default 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="host to bind (default 127.0.0.1)")
    parser.add_argument("--reload", action="store_true", help="dev mode: auto-reload on code changes")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args()

    banner()
    ensure_dependencies()

    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[ok] FFmpeg verified: {ffmpeg_exe}")

    from server.main import DOWNLOADS_DIR
    print(f"[dirs] Downloads folder: {DOWNLOADS_DIR}")

    url = f"http://{args.host}:{args.port}"
    print(f"\n[web] Web interface: {url}\n")
    print("   Press Ctrl+C to stop the service.\n")

    if not args.no_browser:
        open_browser_later(url)

    import uvicorn
    uvicorn.run("server.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
