# Snipmint: Implementation guide

| | |
|---|---|
| **Date** | 2026-11-05 |
| **Related** | [PRD.md](PRD.md) (requirements, IDs like `AN-1`) · [PLAN.md](PLAN.md) (architecture) |

## Progress

| Phase | Status |
|---|---|
| 0. Scaffold + Docker | Done. CI green: tests on Windows and Linux, frontend, Docker build + health smoke test |
| 1. Free MVP | Done. Link → download → Whisper → free moment scoring → center crop → captions → MP4/SRT/titles.json/zip. Web flow: Start, Video, Moments, Captions, Make clips. Tested end to end on a real YouTube video |
| 2. Smart framing | Done. Black-bar removal, YuNet face tracking with a calm camera (cuts at scene changes), two-people split that switches to the speaker during close-ups (per scene), whole-picture "fit" for screens, Auto choice per clip, framing picker in the UI. Tested on real videos (speech, screen recording, two-shot interview). Single-clip caption re-render not started |
| 3. Advanced AI | Done. Gemini two-pass provider, key check, redaction, fallback, caption proofreading (single-word fixes), silent-video picking with a length guard, model choice; Free/Advanced switch and key field in the UI. Gemini layout picker not built: the face-based Auto framing covers it |
| 5. YouTube publishing | Done (optional, off without Google credentials): OAuth connect/disconnect with PKCE, encrypted tokens, resumable uploads with retry/cancel/duplicate protection, per-clip metadata (Gemini + free fallback) with an editor, batch publishing dashboard, scheduled publishing. Tested against a mocked Google API only; not yet tried with a real channel |
| 4. Extras | Done: file upload, local AI provider, -14 LUFS levelling, hook voice-over (Edge/Kokoro/cloned voice), save/open project files, captions on/off, size and length limits. Not built: cookies upload in the UI (use YOUTUBE_COOKIES_FILE), single-clip caption re-render |

**How to use this doc:**
- Work the phases in order. Each task lists the files it touches, what it ports (if anything), and how to check it.
- A phase is done when its **acceptance check** passes in Docker.

**Source checkouts** used as references while porting (read-only, not dependencies):
- `openshorts/` = https://github.com/mutonby/openshorts (MIT; **skip `cloud/`**)
- `narrareel/` = https://github.com/SanTiwari07/NarraReel (MIT)

---

## Phase 0: Scaffold

### 0.1 Create the repo from NarraReel
1. Copy NarraReel into `Snipmint/`, without `.git`. Then run `git init`.
2. Rename the package to `snipmint`:
   - `narrareel/` → `snipmint/`
   - fix imports and `pyproject.toml` (`name`, `[project.scripts] snipmint = "snipmint.cli:app"`)
   - env vars: `NARRAREEL_HOME` → `SNIPMINT_HOME`, `NARRAREEL_PORT` → `SNIPMINT_PORT` (default **8000**), `NARRAREEL_TOKEN` → `SNIPMINT_TOKEN`
   - frontend `package.json` name, logo and title
3. Delete what we replace:
   - frontend: `steps/ScriptStep.tsx`, `steps/LengthStep.tsx`
   - backend: `script/duration.py`, `/api/plan`, `/api/duration/parse`
   - **Keep** `script/analyze.py`, because the caption adapter uses its models.
4. Add `NOTICE` with both MIT copyright lines (OpenShorts 2024, NarraReel contributors).
5. Add `.env.example` (see 0.3), `.gitignore` (`data/`, `.env`, `node_modules/`, `dist/`).

### 0.2 Python dependencies (`pyproject.toml`)
```toml
dependencies = [
  "pydantic>=2.6", "typer>=0.12", "numpy>=1.24", "pillow>=10", "fonttools>=4.50",
  "yt-dlp>=2026.1", "faster-whisper>=1.1", "scenedetect>=0.6.4", "opencv-python-headless>=4.9",
  "httpx>=0.27",
]
[project.optional-dependencies]
server  = ["fastapi>=0.110", "uvicorn[standard]>=0.29", "sse-starlette>=2", "python-multipart>=0.0.9"]
reframe = ["opencv-python-headless>=4.9"]
gemini  = ["google-genai>=1.0"]
voice   = ["edge-tts>=7.2", "kokoro-onnx>=0.6"]
clone   = ["chatterbox-tts>=0.1.7", "setuptools<81"]
gpu     = ["torch", "ultralytics"]
dev     = ["pytest>=8", "pytest-cov", "ruff", "httpx>=0.27", "jsonschema>=4"]
```
Pin exact versions once the first Docker build passes.

### 0.3 Docker

**`docker/Dockerfile`** (CPU):
```dockerfile
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg espeak-ng libsndfile1 libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY snipmint ./snipmint
COPY --from=web /web/dist ./snipmint/server/static
RUN pip install --no-cache-dir ".[server,reframe,gemini,voice]"
ENV SNIPMINT_HOME=/data SNIPMINT_PORT=8000 HF_HOME=/models XDG_CACHE_HOME=/models PYTHONUNBUFFERED=1
VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK CMD curl -fs http://localhost:8000/api/health || exit 1
CMD ["sh", "-c", "export SNIPMINT_TOKEN=${SNIPMINT_TOKEN:-$(python -c 'import secrets;print(secrets.token_urlsafe(12))')}; echo \"Snipmint: open http://localhost:${SNIPMINT_PORT}/?token=${SNIPMINT_TOKEN}\"; exec snipmint serve --host 0.0.0.0 --port ${SNIPMINT_PORT}"]
```

**`docker/Dockerfile.gpu`:**
- Same stages, on base `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04` with python3.12 installed.
- Installs `".[server,reframe,gemini,voice,gpu]"`.
- Sets `WHISPER_DEVICE=cuda WHISPER_COMPUTE=float16`.

**`docker-compose.yml`:**
```yaml
services:
  snipmint:
    build: { context: ., dockerfile: docker/Dockerfile }
    ports: ["8000:8000"]
    env_file: .env
    volumes: ["./data:/data", "models:/models"]
    restart: unless-stopped
  snipmint-gpu:
    profiles: ["gpu"]
    build: { context: ., dockerfile: docker/Dockerfile.gpu }
    ports: ["8000:8000"]
    env_file: .env
    volumes: ["./data:/data", "models:/models"]
    deploy: { resources: { reservations: { devices: [{ driver: nvidia, count: all, capabilities: [gpu] }] } } }
  ollama:
    profiles: ["ollama"]
    image: ollama/ollama:0.12.0
    volumes: ["ollama:/root/.ollama"]
    environment: ["OLLAMA_CONTEXT_LENGTH=16384"]
volumes: { models: {}, ollama: {} }
```
- Run either `snipmint` or `snipmint-gpu`, not both: they share port 8000.
- `docker compose --profile gpu up snipmint-gpu` runs the GPU image.

**`docker-compose.dev.yml`** (override):
- Mounts `./snipmint:/app/snipmint`.
- Command: `uvicorn snipmint.server.app:create_app --factory --host 0.0.0.0 --port 8000 --reload`.
- Sets `SNIPMINT_DEV=1`, which disables the token for localhost.

**`.env.example`:**
```env
# Advanced AI (optional). Leave empty for free mode.
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-lite
# Local AI (optional): http://ollama:11434/v1 when using the ollama profile
LLM_BASE_URL=
LLM_MODEL=qwen2.5:14b
# Transcription
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8
# Downloads
YOUTUBE_COOKIES_FILE=
MAX_SOURCE_MINUTES=180
# Server
SNIPMINT_TOKEN=
```

### 0.4 CI (`.github/workflows/ci.yml`)
- Keep NarraReel's jobs: ruff, pytest, `npm ci && npm run build && npm test`, on Linux and Windows.
- Add a `docker build -f docker/Dockerfile .` job on Linux.

**Acceptance check:** `docker compose up --build` opens the NarraReel-style start screen at `http://localhost:8000`, and `/api/health` returns 200.

---

## Phase 1: Free MVP (PRD milestone M1)

### 1.1 Data layout on disk
```text
/data/
  sources/{source_id}/            source_id = sha1(normalized URL)[:12]
    meta.json                     title, duration, width, height, chapters, heatmap, thumbnail
    source.mp4
    audio.wav                     16 kHz mono (for whisper + loudness)
    transcript.json               {language, segments:[{start,end,text,words:[{word,start,end}]}]}
    signals.json                  loudness curve, scene cuts
    moments.{provider}.json       analysis results per provider
  jobs/{job_id}/                  NarraReel job folder (events.jsonl, outputs)
    clip_01.mp4  clip_01.srt  clip_01.ass  titles.json
```
Reuse is automatic: when a source is submitted again, the cached steps are skipped (TR-4).

### 1.2 Ingest: `snipmint/ingest/`
| File | Do | Port from |
|---|---|---|
| `guard.py` | `validate_url(url)`: allow only youtube.com, youtu.be, m.youtube.com, `/shorts/`; reject playlists; resolve DNS and refuse private IPs | `openshorts/ssrf_guard.py`, `security_utils.py` (simplify) |
| `youtube.py` | `probe(url) -> SourceMeta` (yt-dlp `extract_info(download=False)`: title, duration, thumbnail, width/height, `chapters`, `heatmap`); `download(url, dir, progress)` with `format="bv*[height<=1080]+ba/b[height<=1080]"`, `merge_output_format=mp4`, `noplaylist=True`, optional `cookiefile` | `openshorts/main.py` `_download_youtube_video`, `yt_clients.py` (client list only). **Skip** the proxy chain, paid ledger and statics |
| `audio.py` | `extract_audio(mp4) -> audio.wav` via ffmpeg (`-ac 1 -ar 16000`) | new |

**Errors:** map yt-dlp failures to friendly `IngestError` messages:
- "Sign-in / bot check: add a cookies file"
- "Private video"
- "Age-restricted"
- "Live stream not supported"

### 1.3 Transcribe: `snipmint/transcribe/whisper.py`
- `transcribe(audio_wav, progress) -> Transcript` uses faster-whisper `WhisperModel(WHISPER_MODEL, device, compute_type)` with `word_timestamps=True` and `vad_filter=True`.
- Report progress as `segment.end / duration`.
- Port `merge_continuation_words` from `openshorts/subtitles.py`, which fixes split tokens like "don" + "'t".
- After the run, free the model (`del model; gc.collect()`), the same rule as OpenShorts' `release_models()`.
- Pydantic models in `snipmint/analyze/schemas.py`: `Word{text,start,end,prob}`, `Segment`, `Transcript{language, segments, words}`.

### 1.4 Signals: `snipmint/analyze/signals.py`
| Function | How |
|---|---|
| `loudness_curve(audio_wav, hop=0.5)` | NumPy RMS per 0.5 s → dB → z-score |
| `scene_cuts(mp4)` | PySceneDetect `ContentDetector(threshold=27)` on a 360p downscale; port the downscale idea from `openshorts/scene_detection.py` |
| `heatmap_curve(meta)` | yt-dlp `heatmap` = `[{start_time,end_time,value}]` → value per second, or `None` |
| `speech_rate(words)` | words per second per window |

### 1.5 Windows: `snipmint/analyze/windows.py`
- Port from `openshorts/clip_selection.py`: `clip_duration_bounds`, `shortlist_target`, `clip_count_targets`.
- Port from `main.py`: `build_transcript_windows`.
- Add `candidate_spans(transcript, min_s, max_s)`:
  - **Starts** are sentence starts (punctuation or a gap ≥ 0.6 s).
  - **Ends** are sentence ends where `min_s ≤ end - start ≤ max_s`.
  - Use a stride of 1 sentence and cap at about 2,000 candidates.

### 1.6 Moment schema: `snipmint/analyze/schemas.py`
```python
class Moment(BaseModel):
    id: str
    start: float
    end: float
    score: int                     # 0-100
    why: str
    title: str
    hook_text: str
    descriptions: dict[str, str] = {}   # tiktok, instagram
    keywords: list[str] = []
    provider: Literal["heuristic", "local_llm", "gemini"]
    fallback: bool = False         # True if a better provider failed and this one stepped in
    layout: Literal["track", "split", "screencast", "center"] | None = None
```

### 1.7 Heuristic provider: `snipmint/analyze/providers/heuristic.py` (AN-1)
**Score each candidate span. Every feature is normalised to 0..1:**

| Feature | Weight | Computation |
|---|---|---|
| `heatmap` | 0.30 | mean heatmap value over the span ÷ the video's max (0 if there's no heatmap; weights renormalise) |
| `hook` | 0.20 | first sentence: +question mark, +second person ("you/your"), +number, +contrast or curiosity words (`but, never, nobody, secret, mistake, actually, truth, why, how`), +short (≤ 12 words); −filler start (`um, uh, so, and, because, like`) |
| `energy` | 0.15 | mean loudness z-score over the span, plus peak count |
| `density` | 0.15 | TF-IDF of span words vs the whole transcript (pure Python, stopword list for en/hi) |
| `complete` | 0.10 | starts at a sentence start and ends on `.?!`; no dangling start word |
| `pace` | 0.05 | speech rate close to the video median (penalise long silences) |
| `cuts` | 0.05 | penalise > 1 scene cut per 3 s (montage/B-roll) |

Then:
1. `score = round(100 * Σ w·f)`.
2. **Select:** sort, apply non-max suppression (drop spans overlapping a kept one by > 30%), and take `clip_count_targets(...)` (or the user's count).
3. Snap the edges to word boundaries with a 0.15 s pad.
4. **Text fields:**
   - `hook_text` = the best-scoring sentence in the first 40% of the span, trimmed to ≤ 9 words.
   - `title` = the chapter title containing the span, else `hook_text`.
   - `why` = template from the top 2 features, e.g. "Most replayed part · opens with a question".
   - `keywords` = top 5 TF-IDF terms.
5. Keep the weights in `snipmint/analyze/weights.json`, so tuning needs no code change.

### 1.8 Analysis service: `snipmint/analyze/service.py`
```python
def analyze(source_id, provider="auto", opts) -> list[Moment]:
    # auto = gemini if key available else local_llm if LLM_BASE_URL else heuristic
    # any provider exception -> heuristic, with fallback=True on every moment
    # writes moments.{provider}.json
```

### 1.9 Caption adapter: `snipmint/captions/from_transcript.py` (CP-1)
```python
def clip_words(transcript, start, end, keywords=()) -> tuple[ScriptAnalysis, list[WordTiming]]:
    words = [w for w in transcript.words if w.end > start and w.start < end]
    # shift to clip time, clamp to [0, end-start]
    # tokens: Token(text=w.text, norm=normalise(w.text), start_char/end_char from joined text,
    #               sentence_idx, emphasis = norm in keywords)
    # sentences: split after .?! or gap >= 0.7 s
    # WordTiming(token_idx=i, text=w.text, start, end, source="aligned")
```
- Feed the result to NarraReel `captions.ass.build_captions(words, analysis, style, fmt)`. The `style` comes from `captions/styles.py` presets and `fmt` from `presets/formats/reels.json`.
- **Test:** a fixed transcript fixture plus each preset produces valid ASS. A snapshot test sits beside NarraReel's `tests/golden/`.

### 1.10 Center-crop reframe: `snipmint/reframe/center.py` (RF-1, RF-5)
- `cut_and_crop(source, start, end, fmt) -> clip_raw.mp4`.
- Uses a single ffmpeg call: `-ss start -to end -i source` and `crop=ih*9/16:ih,scale=1080:1920`.
- If the source is already vertical (h > w), it only scales or pads.
- Port the encode args from `openshorts/ffmpeg_utils.py` `video_encode_args` (x264, `-preset veryfast -crf 20`, AAC 192k, `+faststart`).

### 1.11 Clip render: `snipmint/render/clip_render.py`
- Extend NarraReel `RenderSpec` with `background="video"` + `audio_from_background=True`. The audio then maps to `0:a` instead of a separate `voice.wav`.
- Order of work:
  1. `clip_raw.mp4`
  2. `build_captions` → `clip.ass` (fonts are copied into the job's `fonts/`, the same as NarraReel)
  3. ffmpeg `subtitles=clip.ass:fontsdir=fonts`, output `clip_NN.mp4`
- Write `clip_NN.srt` from the same words, using NarraReel `captions/srt.py`.

### 1.12 Pipeline: `snipmint/pipeline/`
| File | Job | Stages (progress events) |
|---|---|---|
| `source.py` | "prepare source" task | `probe` 0-5% → `download` 5-40% → `audio` 40-45% → `transcribe` 45-85% → `signals` 85-92% → `analyze` 92-100% |
| `clips.py` | "make clips" job | per selected moment: `cut` → `captions` → `render`. One clip failing logs an error event and continues (NF-4) |

Both run on the NarraReel `JobRunner` and stream SSE events through the existing `/api/jobs/{id}/events` and `/api/tasks/{id}/events`.

### 1.13 API (in `snipmint/server/app.py` + `schemas.py`)
| Method | Path | Body / result |
|---|---|---|
| POST | `/api/sources` | `{url}` → `{source_id, task_id}` (starts "prepare source") |
| GET | `/api/sources/{id}` | meta + `status` per stage + `moments` if ready |
| GET | `/api/sources/{id}/thumbnail.jpg` | thumbnail |
| GET | `/api/sources/{id}/frame.jpg?t=` | a frame at time t (for the caption preview) |
| POST | `/api/sources/{id}/analyze` | `{provider, min_s, max_s, count}` → `task_id` |
| PATCH | `/api/sources/{id}/moments/{mid}` | `{start, end, selected}` |
| POST | `/api/preview/frame` | (NarraReel, extended) + `{source_id, t}` uses the clip frame as background |
| POST | `/api/jobs` | `{source_id, moment_ids, captions, format, framing, export}` → `job_id` |
| GET | `/api/jobs/{id}/files/{name}` | (NarraReel) download outputs |
| GET | `/api/health` | + `ai: {gemini, local_llm}`, `gpu: bool` |

After changing the API, run `python scripts/export_openapi.py` and `npm run api:types` to regenerate `frontend/src/api/schema.d.ts`.

### 1.14 Frontend (NarraReel shell, new steps)
**`flow.ts`:**
```ts
export const STEPS = [
  { path: '/source',   label: 'Video' },
  { path: '/moments',  label: 'Moments' },
  { path: '/audio',    label: 'Audio' },     // Phase 4; hidden until then
  { path: '/captions', label: 'Captions' },
  { path: '/export',   label: 'Make clips' },
]
```

| File | Content |
|---|---|
| `steps/StartScreen.tsx` | Replace "Write a script" with a **YouTube link input** + "Start". Keep "Open project". Add an AI mode chip (Free / Advanced; disabled until Phase 3) |
| `steps/SourceStep.tsx` | Thumbnail, title, duration; quality warning < 720p; stage list with progress from SSE; auto-advance to Moments when done; error banner with retry |
| `steps/MomentsStep.tsx` | Length band segmented control, clip count; grid of `ClipCard`; "Select all / none"; Next blocked if none selected |
| `components/ClipCard.tsx` | Score badge (yellow), title, `why`, `mm:ss–mm:ss`, preview using `<video src=…#t=start,end>`, ±1 s nudges, checkbox, fallback badge |
| `state/source.ts` | Zustand: `sourceId, meta, stages, moments, selectedIds, aiMode` |
| `steps/CaptionsStep.tsx` | Keep; the preview calls `/api/preview/frame` with `source_id` + the first selected moment's midpoint |
| `steps/ExportStep.tsx` | Keep the layout; add framing (Center only in Phase 1) and format; list of rendered clips with download links and `titles.json` |
| `state/project.ts` | Replace the script/voice draft with `{source_url, moments, captions, format, framing, export}` |

Keep NarraReel's styles, `ui.tsx` components, `PhoneFrame`, `Toasts` and accessibility patterns unchanged.

### 1.15 Tests
| Test | Checks |
|---|---|
| `tests/unit/test_guard.py` | Accepts valid YouTube forms; rejects playlists, other hosts and private IPs |
| `tests/unit/test_windows.py` | Candidate spans respect min/max and sentence starts |
| `tests/unit/test_heuristic.py` | On a fixture transcript and heatmap, the top moment is the planted "hook" span; NMS removes overlaps; deterministic |
| `tests/unit/test_from_transcript.py` | Times are shifted and clamped; tokens line up with words; emphasis comes from keywords |
| `tests/unit/test_clip_render.py` | The ffmpeg command contains crop, subtitles and audio mapping (no ffmpeg run needed) |
| `tests/integration/test_pipeline.py` (marked `slow`) | A 60 s Creative-Commons sample file → 1+ clip MP4 exists, 1080×1920, has an audio stream |
| `frontend/src/steps/*.test.tsx` | Flow guards: can't reach Moments without a source; Next is blocked with no selection |

**Acceptance check (M1):** in Docker with no key, paste a 20-minute podcast link and get ≥ 5 selectable moments. Render 3 of them and download 3 MP4s at 1080×1920. Captions use the chosen preset, and SRT files plus `titles.json` are present.

---

## Phase 2: Smart framing (M2)

### 2.1 Port the speaker-tracking reframe → `snipmint/reframe/track.py` (RF-2)
- **Port** from `openshorts/`:
  - `reframe_v2.py`: analyse at a downscale, render the crop path natively in ffmpeg
  - `active_speaker.py`: mouth activity per face, `normalise_activity`
  - `main.SmoothedCameraman` and `SpeakerTracker` (classes at `main.py:112` and `:267`)
  - `ffmpeg_utils.blurred_backdrop`
- **Strip:**
  - `gpu_decode`, `gpu_render` and `osrender` (use the CPU ffmpeg path)
  - `camera_inset`, `punch_in`, `screencast_layout`, `layout_ranges`: later, if needed
  - watermark arguments
- **Face detection:** OpenCV YuNet (MediaPipe 1.x dropped its face detector). No face model or OpenCV: centre crop.
- **Interface:** `reframe(source, start, end, fmt, mode) -> clip_raw.mp4`, with `mode ∈ {center, track, split}`.
- If tracking fails, fall back to center and log a warning event.

### 2.2 Split layout → `snipmint/reframe/split.py` (RF-3)
- Port `openshorts/split_layout.py` + `frame_sampler.py`: two faces → top and bottom halves.
- **Captions on the seam:** NarraReel `layout.position="middle"` + `fmt.position_y`. Check the caption sits between the halves; OpenShorts needed `{\an5}` per word for this.

### 2.3 Formats, zip and re-render
| Item | Detail |
|---|---|
| RF-6 | Expose NarraReel `portrait` and `square` formats; crop the ratio from the format's width/height |
| EX-3 | `GET /api/jobs/{id}/archive.zip` streams a zip of the outputs |
| EX-4 | `POST /api/jobs/{id}/rerender {moment_id, captions}` reuses `clip_raw.mp4` and only redoes the captions and burn-in (extends NarraReel's rerender endpoint) |

UI: the Export step gets a framing control (Auto track / Center / Split) with small diagrams, a "Download all" button and per-clip "Edit captions & re-render".

**Acceptance check (M2):** on 10 test clips (interviews, solo talking head, 2-person podcast), the speaker's face stays fully in frame in ≥ 90% of sampled frames, and split mode shows both people.

---

## Phase 3: Advanced AI with Gemini (M3)

### 3.1 Key plumbing (ST-1..3, ST-5)
- **Where the key lives:** only `GEMINI_API_KEY` in `.env` (server environment). The UI never asks for it; when it is missing, Advanced mode shows how to add it to `.env`.
- `snipmint/ai/keys.py`:
  - `resolve_key(request) -> str | None`
  - logging filter that redacts `AIza…` patterns
  - the key is never put in `Job` params on disk: it's passed to the task closure in memory only
- `POST /api/ai/check-key` → `client.models.list()` with a timeout → `{ok, model_available, error}`.
- `/api/health.ai.gemini` = env key present (the UI also knows about its own local key).
- **Frontend:** `components/AiModeToggle.tsx` on the Start screen plus a settings modal.
  - The key field (password type) is stored in `localStorage` under `snipmint.geminiKey`, with a "Check key" button.
  - `api/client.ts` adds the header when the mode is Advanced.

### 3.2 Gemini provider → `snipmint/analyze/providers/gemini.py` (AN-2)
- **Port:**
  - `openshorts/gemini_worker.py`: `ScoreResponse`, `DetailResponse`, `SCORE_PROMPT_TEMPLATE`, `DETAIL_PROMPT_TEMPLATE`, `_parse_json_response_text`
  - `main.py`: `_run_gemini_stage` (retries), `_run_stage_split`, `score_batch_size`, `get_viral_clips`
- **Changes while porting:**
  - Add hints to the score prompt: per window, `heatmap_mean`, `energy_z`, `chapter_title`. Gemini is told they're signals, not answers.
  - Map `DetailClipModel` → `Moment` (`video_title_for_youtube_short` → `title`, `viral_hook_text` → `hook_text`, `predicted_score` → `score`, `why` → `why`, descriptions).
  - Add a `keywords: list[str]` field to the detail schema (≤ 5 words to emphasise).
  - Enforce the `min_clips` floor in code (OpenShorts rule).
- **Cost:** log the token usage per stage; show the estimated cost on the Moments screen.
- **Fallback (AN-8):** any exception → heuristic moments with `fallback=True`; the UI shows "Used free analysis: Gemini error: …".

### 3.3 Layout picker → `snipmint/reframe/layout_ai.py` (RF-4)
- Port `openshorts/layout_picker.py` + `LAYOUT_CHOICE_PROMPT`: 12 frames at 1024px, a closed choice between track, split and screencast.
- **Rule from OpenShorts:** the model only *adds* options. An explicit user choice always wins.
- Run it once per source and store it in `meta.json`. The Export framing control defaults to the result.

### 3.4 Caption emphasis (CP-4)
- The `keywords` from Gemini moments flow into `from_transcript.clip_words(keywords=…)` → `Token.emphasis`.
- The `keyword-emphasis` and `bold-pop` presets then highlight them.

### 3.5 Tests
- Gemini is mocked through an injectable `client` (the same pattern as OpenShorts' `llm_backend._client`).
- Schema mapping; fallback on exception; the key never appears in logs (caplog assertion) or in job files on disk.

**Acceptance check (M3):**
- With a valid key, moments have model-written titles and reasons, and the layout is auto-picked.
- With an invalid key, the job still completes on heuristic results and shows the fallback badge.
- `grep -r AIza data/` finds nothing.

---

## Phase 4: Extras (M4)

| Task | Files | Notes |
|---|---|---|
| IN-2 Upload | `ingest/upload.py`, `POST /api/sources/upload` | `python-multipart`; source_id = sha1 of the file head |
| IN-4 Cookies | `ingest/youtube.py` | `YOUTUBE_COOKIES_FILE` mounted into `/data/cookies.txt`; settings note in the UI |
| IN-6 Max length | `ingest/youtube.py` | Refuse if `duration > MAX_SOURCE_MINUTES` |
| AN-3 Local AI | `providers/local_llm.py` | Port `openshorts/llm_backend.py` as-is (json_schema → json_object → plain fallback); heuristic shortlist as the windows; `score_batch_size=3` |
| AN-10 Silent video | `providers/gemini.py` `visual_moments()` | Port `get_visual_clips` + `VISUAL_PROMPT_TEMPLATE`; **add a guard**: refuse > 45 min (≈ 300 tok/s) or sample segments; free mode uses heatmap or even splits |
| CP-5 ASR cleanup | `ai/cleanup.py` | Gemini gets the clip words + video title and returns only corrections `[{index, text}]`; timings stay untouched |
| CP-6 Captions off | `clips.py` | Skip burn-in, still write SRT |
| AU-2 Hook voice-over | `pipeline/clips.py`, `steps/AudioStep.tsx` | NarraReel `engines` speak `hook_text`; ffmpeg `sidechaincompress` ducks the original audio for that duration |
| AU-3 Loudness | `render/clip_render.py` | Reuse NarraReel `audio/process.py` loudnorm at -14 LUFS |
| EX-5 Project file | `project.py`, `lib/projectFile.ts` | `SnipmintProject` pydantic model; versioned; never contains a key |
| ST-4 Model select | Settings modal | `GEMINI_MODEL` default; free-text override |

---

## Phase 5: YouTube publishing (PRD §6.9, YT-1..7)

| Step | Files | Check |
|---|---|---|
| 5.1 OAuth + token storage | `snipmint/youtube/{settings,secrets,store,oauth}.py`, `server/youtube_api.py` | `tests/youtube/test_oauth.py`: PKCE URL, state single-use + browser binding, open-redirect guard, encrypted at rest, never in responses or logs, refresh, revoke on disconnect |
| 5.2 Resumable upload | `youtube/client.py`, `youtube/publisher.py` | `tests/youtube/test_upload.py`: chunks + 308 resume, backoff on 5xx, 401 refresh, quota error, cancel, duplicates, concurrency limit, public/unlisted/private sent as chosen, a visibility YouTube changed is reported |
| 5.3 Metadata | `snipmint/metadata/*`, `server/metadata_api.py` | `tests/youtube/test_metadata.py`: fallback uses only clip words, unique titles, languages, mocked Gemini, edits survive regeneration |
| 5.4 Editor UI | `steps/PublishStep.tsx`, `components/MetadataEditor.tsx`, `lib/metadata.ts` | `src/lib/metadata.test.ts`, `src/steps/PublishStep.test.tsx` |
| 5.5 Batch + dashboard | `components/PublishDashboard.tsx`, `components/ConfirmDialog.tsx` | confirmation required (`confirmed: true`), statuses, retry |
| 5.6 Scheduling | `publish_at` in `PublishRequest`: private + `status.publishAt` | persisted, resumed after restart, `/api/youtube/scheduled` |
| 5.8 Dev vs production config | `youtube/settings.py` (dev localhost default, redirect rules), `config.load_env_file`, `serve --env-file`, `.env.development.example`, `docker-compose.dev.yml` | `tests/youtube/test_settings.py` |
| 5.7 Integration | `tests/youtube/mock_google.py`, `test_integration_mock_server.py` | real HTTP against a mock Google/YouTube server, env-only configuration |

**Endpoints:** `GET /api/youtube/status` · `POST /api/youtube/oauth/start` · `GET /api/youtube/oauth/callback` (no API token; state + cookie + PKCE) · `POST /api/youtube/disconnect` · `POST /api/youtube/channels/{id}/default` · `POST /api/youtube/publish` · `GET /api/youtube/publish` · `GET /api/youtube/scheduled` · `GET /api/youtube/publish/{id}` · `POST /api/youtube/publish/{id}/cancel` · `POST /api/youtube/publish/{id}/retry` · `DELETE /api/youtube/publish/{id}` · `GET /api/metadata/options` · `GET /api/metadata/{source_id}` · `POST /api/metadata/{source_id}/generate` · `POST /api/metadata/{source_id}/{clip_id}/regenerate` · `PUT /api/metadata/{source_id}/{clip_id}`

**Env** (production in `.env`, development in `.env.development`): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `SNIPMINT_SECRET_KEY`, `YOUTUBE_UPLOAD_CONCURRENCY`, `SNIPMINT_UI_URL`; test-only endpoint overrides `YOUTUBE_{AUTH,TOKEN,REVOKE,API,UPLOAD}_URL`.

**Acceptance check:** with a development OAuth client and `snipmint serve --dev` on localhost, connect a channel, publish one clip as public and one scheduled, see both in YouTube Studio; disconnect removes Snipmint from myaccount.google.com/permissions.

## Conventions

- **Python:** ruff (NarraReel config), type hints, pydantic models at module boundaries, and `SnipmintError` subclasses for user-facing errors (extend NarraReel `errors.py`).
- **Progress:** every long step reports `(stage, fraction, message)` through the job callback; the UI shows the message.
- **Ported code:** keep a header comment `# Ported from openshorts/<file> (MIT, © 2024 OpenShorts)` in each ported module.
- **Never commit:** `.env`, `data/`, cookies, API keys.
- **Before pushing:** `ruff check . && pytest -m "not slow"` and `cd frontend && npm run build && npm test`.

## Definition of done (v1)

- [ ] M1–M4 acceptance checks pass in Docker on Windows (Docker Desktop + WSL2) and Linux.
- [ ] CI is green: ruff, pytest, frontend build and tests, docker build.
- [ ] README: quick start (`docker compose up`), Free vs Advanced table, cookies help, license notes.
- [ ] `NOTICE` lists OpenShorts and NarraReel; no file from `openshorts/cloud/` is present.
