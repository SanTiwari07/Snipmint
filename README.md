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

1. **You paste a YouTube link.**
2. Snipmint **downloads** the video and **transcribes** every word with its timing (locally, with Whisper).
3. It **finds the best moments** (30-60 s by default) and shows them as cards with a score and a reason.
4. **You pick** the clips you like.
5. Each clip is **cropped to vertical** (9:16).
6. You choose one of **10 caption styles** with a live preview.
7. You **download** MP4s, SRT subtitles and suggested titles.

## Free vs Advanced

| | **Free (default)** | **Advanced (your Gemini key)** |
|---|---|---|
| Picks moments with | YouTube "most replayed" data, strong opening lines, loudness, keywords, full sentences | Gemini reads the transcript like an editor: stories, punchlines, payoffs |
| Titles and hooks | Taken from the transcript or chapters | Written for each platform |
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
