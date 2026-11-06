<p align="center">
  <img src="docs/assets/snipmint-logo.svg" alt="Snipmint logo" width="88">
</p>

<h1 align="center">Snipmint</h1>

<h3 align="center">Paste a YouTube link. Get vertical shorts with captions people watch to the end.</h3>

<p align="center">
  Snipmint finds the best moments in a long video, cuts them, crops them to 9:16 and adds
  <b>word-by-word animated captions</b>. It runs on your computer, needs <b>no API key</b> by default,
  and gets smarter if you add a <b>Gemini key</b>.
</p>

<p align="center">
  <img alt="Licence: MIT" src="https://img.shields.io/badge/licence-MIT-faff69?labelColor=0a0a0a">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-faff69?labelColor=0a0a0a">
  <img alt="Docker" src="https://img.shields.io/badge/docker-ready-faff69?labelColor=0a0a0a">
</p>

> **Status: early development.** See [PRD.md](PRD.md) for what we are building, [PLAN.md](PLAN.md)
> for the architecture and [IMPLEMENTATION.md](IMPLEMENTATION.md) for the build plan and progress.

---

## What it does

1. **You paste a YouTube link** (or upload a video file from your computer).
2. Snipmint **downloads** the video and **transcribes** every word with its timing (locally, with Whisper).
3. It **finds the best moments** (30-60 s by default) and shows them as cards with a score and a reason.
4. **You pick** the clips you like.
5. Each clip is **cropped to vertical** (9:16): it follows the speaker, stacks two people
   during two-shots, or keeps the whole picture for screen recordings. Black bars are removed.
6. You choose one of **10 caption styles** with a live preview.
7. Optional: an AI voice reads each clip's **hook** over its first seconds.
8. You **download** MP4s (levelled to -14 LUFS), SRT subtitles and suggested titles, and can
   **save the project** to open it again later.
9. Optional: **publish to YouTube** as Shorts, with titles, descriptions and hashtags written
   from what is said in each clip, now or on a schedule. See [Publish to YouTube](#publish-to-youtube-optional).

## Free vs Advanced

| | **Free (default)** | **Advanced (your Gemini key)** |
|---|---|---|
| Picks moments with | YouTube "most replayed" data, strong opening lines, loudness, keywords, full sentences | Gemini reads the transcript like an editor: stories, punchlines, payoffs |
| Titles and hooks | Taken from the transcript or chapters | Written for each platform |
| Captions | Whisper's words | Misheard names and terms corrected |
| Videos with little speech | Replay data or evenly spread | Gemini watches the video (up to 45 min) |
| Your data | Stays on your computer | Transcript text (and a few frames) go to Google |
| Cost | Free | A few cents per hour of video |

If Gemini fails (bad key, quota), Snipmint falls back to the free analysis and tells you.

## Quick start (Docker)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (on Windows with WSL2).

```bash
git clone https://github.com/SanTiwari07/Snipmint.git
cd Snipmint
cp .env.example .env        # optional: add GEMINI_API_KEY for Advanced mode
docker compose up --build
```

Open the link the container prints (`http://localhost:8000/?token=...`). Your videos and clips land in `./data`.
If you started it in the background (`docker compose up -d`), get the link with
`docker compose logs snipmint`; it changes on every restart unless you set `SNIPMINT_TOKEN` in `.env`.
The first video also downloads the speech model (about 460 MB for `small`) into `./data/models`.

- **NVIDIA GPU:** `docker compose --profile gpu up --build snipmint-gpu`
- **Local AI (Ollama):** `docker compose --profile ollama up`, then set `LLM_BASE_URL=http://ollama:11434/v1`

## Development

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,server,transcribe]"
snipmint serve --dev                             # API on :8000

cd frontend && npm install && npm run dev        # UI on :5173
```

Tests: `pytest -m "not network and not models"` and `cd frontend && npm test`.

## Publish to YouTube (optional)

Snipmint can upload the clips it made to your YouTube channel as Shorts. It is **off** until you
add Google OAuth credentials; everything else works the same without them.

**What you get:** connect/disconnect a channel, per-clip metadata (title with 3 ideas and why one
was picked, description, 3-5 hashtags, tags, hook, pinned comment, category, SEO keywords) in
English, Hindi, Hinglish, Marathi and more, an editor (regenerate, edit or copy any field), batch
publishing with a confirmation step and a live dashboard, and scheduled publishing.

### 1. Set up Google Cloud (once, about 10 minutes)

1. Open the [Google Cloud console](https://console.cloud.google.com/) and **create a project**
   (for example "Snipmint").
2. **APIs & Services → Library**: search for **YouTube Data API v3** and click **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding/Audience):
   - User type **External**, app name "Snipmint", your e-mail as support and developer contact.
   - **Data access / scopes**: add `.../auth/youtube.upload` and `.../auth/youtube.readonly`.
   - **Audience → Test users**: add the Google account that owns your channel.
   - **Testing mode warning:** while an *External* app's publishing status is *Testing*, only test
     users can sign in and Google expires the refresh token after **7 days**. Snipmint then asks you to
     connect again. Publish the app (Audience → *Publish app*) to avoid this; Google may ask for
     verification of the YouTube scopes before you can.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Web application**.
   Use **two clients**, one per environment, so development never touches production settings:

   | Client | Authorized redirect URI |
   |---|---|
   | *Snipmint (development)* | `http://localhost:8000/api/youtube/oauth/callback` |
   | *Snipmint (production)* | `https://<your-host>/api/youtube/oauth/callback`, or for a Docker install used only on this computer `http://localhost:8000/api/youtube/oauth/callback` |

   Google only accepts plain `http` for `localhost` / loopback addresses; anything public must be
   `https`. Snipmint checks this at start-up and shows the problem on the Publish screen.
   Google compares the URI exactly, so also add `http://127.0.0.1:8000/...` if you open Snipmint that way.

### 2. Local development (no deployment needed)

```bash
cp .env.development.example .env.development   # fill in the development client
snipmint serve --dev                           # API on :8000, reads .env.development
cd frontend && npm run dev                     # UI on :5173
```

`.env.development` is read only by `snipmint serve --dev` (or `--env-file <file>`) and by
`docker-compose.dev.yml`. In `--dev` mode an empty `GOOGLE_REDIRECT_URI` means
`http://localhost:<SNIPMINT_PORT>/api/youtube/oauth/callback`, and after signing in you land back on
the Vite UI (`SNIPMINT_UI_URL`, default `http://localhost:5173` in dev). Open the UI at
`http://localhost`, not `127.0.0.1`, so the sign-in cookie matches the callback host (the Publish
screen warns you when they differ).

### 3. Production / Docker

Put the **production** client in `.env` (copied from `.env.example`); `docker compose up --build`
reads only that file:

```env
GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_REDIRECT_URI=https://clips.example.com/api/youtube/oauth/callback
SNIPMINT_SECRET_KEY=<a long random string>   # encrypts the stored sign-in
SNIPMINT_TOKEN=<a long random string>        # required when Snipmint is reachable from the network
```

For a Docker install used on the same computer, `GOOGLE_REDIRECT_URI=http://localhost:8000/api/youtube/oauth/callback`
is fine; open Snipmint at `http://localhost:8000`. The encrypted sign-in lives in
`./data/integrations/` (never in project files, job folders or exports). Set `SNIPMINT_SECRET_KEY` so
the key isn't stored next to the data. Restart Snipmint, make some clips, then click
**Publish to YouTube** on the *Make clips* screen.

### 4. Limits you should know

- **Visibility:** pick *Public*, *Unlisted* or *Private*; Snipmint sends exactly that as
  `status.privacyStatus`. Since the YouTube Data API revision of 2 November 2026, uploads from
  unverified API projects are no longer forced to private. If YouTube still sets a different
  visibility (for example a channel default or a policy review), the dashboard says so instead of
  claiming the one you asked for.
- **Quota:** `videos.insert` has its own *Video Uploads* quota bucket. The default is
  **100 upload calls a day** per Google Cloud project, but quotas are project-specific: check yours
  under *Google Cloud console → APIs & Services → YouTube Data API v3 → Quotas* and request more
  there. Hitting the limit shows a clear error; retry after midnight Pacific time.
- **Scheduling** uploads the clip right away as *private* with a publish time (`status.publishAt`,
  which YouTube only accepts on private videos); YouTube makes it public at that time even if
  Snipmint is off. Scheduled items can be cancelled in Snipmint until their upload finishes; after
  that, change or cancel the schedule in YouTube Studio (Snipmint only has upload permission, not
  permission to edit videos).
- Uploads are resumable: a network hiccup is retried with exponential backoff, and uploads a restart
  interrupted continue on the next start. The same clip file can't be sent to the same channel twice
  unless you tick *Upload again anyway*.
- Metadata is written only from the clip's transcript. Without a Gemini key a free builder uses the
  speaker's own sentences (it can't translate); with a key, Gemini writes it in the language you pick.
- Follow the [YouTube API Services Terms](https://developers.google.com/youtube/terms/api-services-terms-of-service)
  and Google's OAuth policies: only publish videos you have the rights to.

### Security

The sign-in uses the OAuth authorization-code flow on the server with PKCE and a single-use `state`
bound to your browser. Tokens are encrypted at rest (Fernet), never sent to the web app, never written
to logs, projects or exports, and **Disconnect** revokes them at Google. Snipmint asks only for
`youtube.upload` and `youtube.readonly`.

## Caption styles

Ten presets rendered from the same word timings: bold-pop, highlighter, karaoke, neon, one-word,
keyword-emphasis, boxed, typewriter, minimal, subtitle-classic. See [docs/captions.md](docs/captions.md).

<p align="center">
  <img src="docs/assets/presets.png" alt="Caption presets" width="720">
</p>

## How it is built

Snipmint combines two MIT-licensed projects:

- **[NarraReel](https://github.com/SanTiwari07/NarraReel)**: the web app, the caption engine and presets,
  the ffmpeg renderer, the voices and the job runner.
- **[OpenShorts](https://github.com/mutonby/openshorts)**: download, transcription, clip-window selection,
  reframing and the Gemini prompts (core only; nothing from its commercial `cloud/` folder).

See [NOTICE](NOTICE).

## Responsible use

Only clip videos you own or have the rights to use. Many platforms require labelling AI-generated
content; check the rules of the platform you post on.

## Licence

MIT. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
