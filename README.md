# STREAM PRESS

**A local, personal-use, universal media downloader** — paste a link, pick a format, save the file. Runs entirely on your machine: no accounts, no telemetry, no cloud.

Reads **1,000+ sources** (via yt-dlp): YouTube, Instagram, X/Twitter, Facebook, TikTok, Snapchat Spotlight, Pinterest, Reddit, Vimeo, Twitch, Dailymotion, and countless others. DRM-protected streams (Netflix, Prime Video) and login-walled content cannot be saved.

> Strictly a **personal, non-commercial** utility for private offline viewing, research and archival under Fair Use. Re-distribution of copyrighted material is the responsibility of the end user.

---

## ✨ Features

- **Universal input** — paste a share link, a raw page URL, an embed snippet (`<iframe src="…">` / `<video>`), a playlist/channel link, or an unblocker/wrapper link; the source is auto-detected and shown in the results. When a video isn't the page's primary content, the embedded player is discovered and used. Wrapper URLs are de-proxied — if the destination hides in a query param (`url=`, `target=`, `__cpo=`, `dest=`…, often percent- or base64-encoded), the real URL is decoded and used instead.
- **Two press modes** — **VIDEO MODE** (videos, streams & audio take priority) and **IMAGE MODE** (photos, pins, posts & profile art take priority). The mode is sent with every request, so the engine hunts accordingly. Flip with one toggle — the mode is remembered between sessions.
- **Never stops at “no video”** — if a link has no video (or the video is unreachable), the engine keeps digging: post images, Pinterest API/page, then a universal og:image scrape of the page itself. A video link that fails still hands you its thumbnail/cover instead of an error.
- **Videos & audio** — per-resolution video rows plus audio-only extracts (MP3 / M4A / WAV / FLAC).
- **Images & posts** — download image posts straight from Instagram, X, Pinterest, Reddit, LinkedIn and more; multi-image posts pack into a single **ZIP**.
- **Thumbnails** — every YouTube video exposes its full thumbnail ladder (Max res 1280×720 down to Default) as one-tap downloads.
- **Profile art** — channel links offer the **profile picture** and **cover banner** at full resolution (YouTube, X, LinkedIn, Instagram, Pinterest…).
- **Direct image links** — paste any `https://…/photo.jpg` and it saves as-is.
- **Browser session** — opt-in cookies-from-browser unlocks login-walled posts (private Instagram/X/Facebook/LinkedIn) using your own logged-in account, locally.
- **Straight to your Downloads** — finished files land in your PC's real **Downloads folder** (never inside the repo), with an **Open Folder** button on completion.
- **Dark mode** — warm paper by default, "night ink" at the tap of a button.
- **Incognito mode** — downloads leave no history and don't touch your streak.
- **Daily streak** — consecutive-day download counter with a lifetime best.
- **Local-first privacy** — binds to `127.0.0.1` only, zero telemetry, settings stored on-device.

## 🚀 Quick start

Requires **Python 3.9+**.

```bash
# 1. Install dependencies
pip install -r server/requirements.txt

# 2. Run it
python run.py
```

The launcher verifies FFmpeg, prints where files will be saved, and opens the web UI at `http://127.0.0.1:8000`.

Optional env vars (full reference: [`.env.example`](.env.example)):

| Var | Effect |
|---|---|
| `YTMAX_DOWNLOAD_DIR` | Custom folder for finished downloads (default: your OS Downloads folder) |
| `YTMAX_WORKERS` | Parallel download workers (default `2`) |
| `YTMAX_TASK_CAP` | Max remembered tasks (default `60`) |
| `YTMAX_COOKIES_FILE` | Path to a Netscape `cookies.txt` for login-walled / members-only / age-restricted videos |
| `YTMAX_COOKIES_TXT` | Same as above, pasted inline as an env var |
| `YTMAX_PROXY` | Proxy for outbound requests (fixes datacenter-IP blocks / bot checks) |
| `YTMAX_SLEEP_INTERVAL` / `YTMAX_MAX_SLEEP_INTERVAL` | Randomized delay between fragment requests (anti-throttling) |
| `YTMAX_AUTOUPDATE_YTDLP` | `1` = refresh yt-dlp extractors at startup (recommended on servers) |
| `YTMAX_FFMPEG` | Override the ffmpeg binary (defaults to system ffmpeg, then imageio-ffmpeg) |

## 🖼️ What you can pull

| Paste a link to… | You get |
|---|---|
| YouTube video | MP4 video, audio, **thumbnail ladder** |
| YouTube channel (`/@handle`, `/channel/…`) | **Profile pic + cover banner** (full-res) |
| Instagram / X / Pinterest / Reddit post | Video or **images** (carousels → ZIP) |
| Instagram story / reel | Story or reel media (login often needed) |
| LinkedIn post | Post video / images |
| X / LinkedIn / IG profile | **Profile picture** at best available size |
| Direct `https://…/photo.jpg` | The image itself |

> Sites that show a login wall (private posts, some stories) need the **Browser session** option in *Machine Settings* — it reads cookies from your own browser on this machine, so you only ever download what your account can already see.

> **Not supported:** DRM streams, and YouTube **community posts** (they render only in the app's JavaScript, so there is no stable link to scrape).

## 🧠 How it works

- **Frontend** — plain HTML/CSS/JS (no build step). A print-shop "press" UI: paste a link on the Order Slip → the Press Column renders a release sheet. In **VIDEO MODE** the format ledger leads with thumbnails in the extras panel; in **IMAGE MODE** the image grid becomes the primary content, and a one-tap hint flips you back to video when a link also carries video/audio.
- **Backend** — FastAPI + **yt-dlp** (1,700+ extractors) + FFmpeg for merging and embedding. Downloads are staged in a temp directory, then **moved into your Downloads folder** when complete. History, streak and settings are stored in `server/` JSON files that are git-ignored.
- **Save flow** — the file is already in your Downloads; the UI can open the folder for you, or "Save a copy" to re-download it through the browser.

## ☁️ Deployment notes (important)

This app **cannot run on Vercel** (or any serverless platform). Downloading media needs a long-running process with **persistent disk, FFmpeg, and outbound network** — serverless functions have none of these. Options:

- **Keep it local** (recommended) — `python run.py` on your own machine. This is the intended use.
- **A small VPS / VDS or a persistent PaaS** (Railway, Render, Fly.io, a $5 VPS) if you want to reach it from other devices — expose it with auth in front, because it can fetch arbitrary URLs and write files.

> **Deploying to Render?** Read **[docs/RENDER_DEPLOY.md](docs/RENDER_DEPLOY.md)**. A server succeeds or fails for reasons a laptop never hits — auth walls, datacenter-IP blocks, missing ffmpeg (quality caps), stale extractors. That runbook walks through the `cookies.txt` secret, the proxy, ffmpeg verification in the logs, and the yt-dlp auto-update, with a deployment checklist.
- The repo itself is a normal Python project and hosts cleanly on GitHub as-is.

## 🧱 Project layout

```
client/          # frontend (index.html, style.css, app.js) — no build step
server/          # FastAPI backend + yt-dlp download engine
run.py           # launcher (deps check, FFmpeg verify, browser open)
```
