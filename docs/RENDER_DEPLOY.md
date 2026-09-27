# Deploying STREAM PRESS on Render

This is the production runbook. It exists because a download succeeds or fails on
the server for reasons that have nothing to do with the code — and those reasons
fall into **five distinct classes**. Each is fixed by a different knob.

| Failure class | Symptom | Fix | Env / file |
|---|---|---|---|
| **Auth wall** | "This video requires login / is members-only / subscription" | Supply a signed-in session | `YTMAX_COOKIES_FILE` |
| **IP block / bot check** | "Sign in to confirm you're not a bot", 429s | Cookies **and/or** a proxy | `YTMAX_PROXY`, `YTMAX_COOKIES_FILE` |
| **Quality cap** | Output is 360p/720p even though 4K exists | ffmpeg must be installed | Dockerfile apt step |
| **Stale extractor** | Random videos fail right after YouTube changes | Auto-update yt-dlp | `YTMAX_AUTOUPDATE_YTDLP=1` |
| **Missing muxer** | Video downloads but has no audio (or low res) | ffmpeg present + `bestvideo+bestaudio` | Dockerfile + `DEFAULT_VIDEO_FORMAT` |

> The app logs its own diagnosis at boot. Read the first lines of the Render log
> before changing anything.

---

## 1. Create the service

1. Render → **New → Web Service** → connect this repo.
2. **Runtime: Docker** (the repo ships a `Dockerfile`). Do **not** use the native
   Python runtime unless you add an apt build step for ffmpeg yourself.
3. Add a **Persistent Disk** mounted at `/data` (products like videos need durable
   storage; Render's default filesystem is ephemeral).
4. Set the env vars below.

The Dockerfile already:

- `apt-get install ffmpeg` (fixes the quality cap **and** the missing muxer),
- `pip install -U yt-dlp` at build time,
- sets `YTMAX_AUTOUPDATE_YTDLP=1` so extractors refresh on every boot,
- binds `0.0.0.0:$PORT`.

---

## 2. Unlock authenticated videos (the cookie pipeline)

Locally this "just worked" because yt-dlp borrowed your browser profile. A server
has no browser, so you must hand it a **Netscape-format `cookies.txt`**.

### Export cookies

1. Install a cookies export extension in the browser where you are **logged in to
   the site you need** (e.g. *Get cookies.txt LOCALLY* for Chrome/Edge/Firefox).
2. Open the site (YouTube, Instagram, X, …), stay logged in.
3. Click the extension → **Export** → save as `cookies.txt`.
4. Use **Netscape/text** format (not JSON). It starts with a line like
   `# Netscape HTTP Cookie File`.

> Security: a `cookies.txt` is a live session token. Treat it like a password —
> never commit it (it is already in `.gitignore`), and rotate it if leaked.

### Load it as a Render Secret File

1. Render Dashboard → your service → **Environment** → **Secret Files**.
2. Add a secret file named `cookies.txt` (Render mounts it at
   `/etc/secrets/cookies.txt` — the app auto-detects that path).
3. **Redeploy.**

Prefer an env var instead? Set `YTMAX_COOKIES_FILE=/path/to/cookies.txt`, or paste
the file contents into `YTMAX_COOKIES_TXT`.

### Verify

Boot log should show:

```
[cookies] configured → /etc/secrets/cookies.txt
```

If a video still fails, the app now tells you which case you are in: either *"no
cookies configured"* (nothing is set) or *"cookies were rejected"* (expired /
wrong account). Re-export and redeploy.

---

## 3. Anti-bot & IP blocks (a different failure mode)

Cookies prove **who you are**; they do not fix **where you are calling from**.
Render's shared datacenter IP can be rate-limited or blocked outright.

```
# Render → Environment
YTMAX_PROXY=http://user:pass@host:port      # residential/rotating proxy
YTMAX_SLEEP_INTERVAL=1                       # randomized delay between fragments
YTMAX_MAX_SLEEP_INTERVAL=3
```

- **Bot check / "confirm you're not a bot"** → try a residential proxy first;
  premium cookies can also help.
- **HTTP 429 / throttling** → raise the sleep intervals. Slower, but it finishes.
- All outbound paths honour `YTMAX_PROXY`, including direct image fetches.

---

## 4. Confirm muxing in the Render logs

yt-dlp downloads video and audio as **separate streams** and muxes them with
ffmpeg. If ffmpeg is missing it silently keeps the one combined stream it can get
(usually 360p/720p). This image installs ffmpeg, and the app states its status at
boot:

```
[ffmpeg] ffmpeg version 7.x ...
[ffmpeg] binary: /usr/bin/ffmpeg (muxing available)
```

If that line says **`muxing UNAVAILABLE`**, the build did not install ffmpeg —
check the Docker build, not the app. During a download the UI shows
*"Merging & processing with FFmpeg…"*; if it never appears, muxing was skipped.

`GET /api/health` reports `ffmpeg_version`, `ffmpeg_available`, `ytdlp_version`,
`cookies_configured` and `proxy_configured` — use it as a quick triage.

---

## 5. Keep extractors fresh

YouTube changes its signature ciphers often; a stale yt-dlp breaks *random*
videos. Two layers of protection:

1. **Build time** — the Dockerfile runs `pip install -U yt-dlp`.
2. **Boot time** — `YTMAX_AUTOUPDATE_YTDLP=1` makes the app upgrade yt-dlp at
   startup, so the newest extractors land on every deploy without a rebuild.

Boot log:

```
[yt-dlp] version: 2026.08.19
[extractors] native coverage: ['facebook', 'instagram', 'reddit', 'tiktok', 'twitter', 'youtube']
```

All six high-traffic sites resolve to yt-dlp's **native** extractors — there is no
custom scraping for videos (the project only scrapes images/og:image as a
last-resort fallback). The X/Twitter extractor requires the cookies from step 2.

---

## 6. Input types the server now routes

| You paste… | The server does… |
|---|---|
| Share link (`youtu.be/…`, `x.com/…/status/…`) | Direct extraction |
| Raw page URL (article with an embedded player) | Scrapes `<iframe>`, `<video>`, `<source>`, og:video and manifest, then extracts the embedded media |
| Embed code (`<iframe src="…">`, `<video><source src="…">`) | Parses the `src` out of the markup and routes it |
| Playlist / channel URL | Batch listing, then "Download whole playlist" |
| Direct image URL | Saved as-is |
| Unblocker / wrapper URL (`?url=`, `?target=`, `?__cpo=`, `?dest=`, base64 or percent-encoded) | Decodes the hidden destination and re-runs extraction on the real URL (nested wrappers supported) |

---

## Deployment checklist

- [ ] Service runtime set to **Docker** (uses the repo `Dockerfile`).
- [ ] `cookies.txt` added as a **Secret File** → `/etc/secrets/cookies.txt`.
- [ ] `YTMAX_COOKIES_FILE` set **or** the secret auto-detected.
- [ ] `YTMAX_PROXY` set if this IP range is blocked (optional but recommended).
- [ ] `YTMAX_SLEEP_INTERVAL` / `YTMAX_MAX_SLEEP_INTERVAL` set if throttled.
- [ ] Persistent disk mounted at `/data`, with `YTMAX_DOWNLOAD_DIR=/data/downloads`.
- [ ] Boot log shows `muxing available` and `[cookies] configured`.
- [ ] Boot log lists all six native extractors.
- [ ] `GET /api/health` returns `cookies_configured: true`, `ffmpeg_available: true`.
- [ ] `cookies.txt` is git-ignored and never committed.

---

## Quick triage

| Message you see | Meaning | Do this |
|---|---|---|
| "no cookies configured" | Anonymous request hit an auth wall | Add `cookies.txt` secret |
| "cookies were rejected" | Session expired / wrong account | Re-export cookies |
| "prove it is not a bot" | IP block | Add proxy, then cookies |
| "rate-limiting this server's IP" | 429 | Raise sleep intervals / proxy |
| "muxing UNAVAILABLE" | No ffmpeg | Fix the Docker build |
| "no native extractor for …" | Stale yt-dlp | Set `YTMAX_AUTOUPDATE_YTDLP=1` |
