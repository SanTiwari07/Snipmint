# Snipmint: Product Requirements Document

| | |
|---|---|
| **Status** | Draft v1 |
| **Date** | 2026-11-05 |
| **Owner** | Sanskar Tiwari |
| **Related** | [PLAN.md](PLAN.md) (strategy and architecture) · [IMPLEMENTATION.md](IMPLEMENTATION.md) (build steps) |

---

## 1. Summary

The user pastes a YouTube link and Snipmint returns ready-to-post vertical shorts (9:16):
- the best moments, picked automatically
- each clip cropped to follow the speaker
- word-by-word animated captions

**Origins:**
- The look and the captions come from **NarraReel**.
- The video engine comes from **OpenShorts**.

**AI modes:**
- **Default:** runs entirely on the user's computer with no API key.
- **Optional:** with a **Gemini API key**, an "Advanced AI" mode picks moments like a human editor and writes titles and hooks.

## 2. Problem

Creators and editors who want shorts from a long video (podcast, interview, lecture, stream) today must:
1. Watch the whole video to find good moments. This takes hours.
2. Cut, reframe to vertical and caption each clip by hand.
3. Or pay for a SaaS clipper (Opus Clip and similar), which means subscriptions, watermarks, uploading their video and no control.

| Existing tool | Gap |
|---|---|
| OpenShorts (open source) | Needs a Gemini key (or a local LLM) just to work, and the UI is cluttered with SaaS features |
| NarraReel (open source) | Has great captions and UX, but only works from a written script |

## 3. Target users

| Persona | Need |
|---|---|
| **Solo creator** (podcaster, YouTuber) | Turn each long upload into 5-10 shorts quickly, free |
| **Clipper / editor** | Fast first pass on moments, then fine control over the cuts |
| **Indian creator (Hindi/Hinglish)** | Captions that render Devanagari correctly (NarraReel already supports this) |
| **Privacy-conscious / offline user** | Nothing leaves the machine unless they add a key |

## 4. Goals and non-goals

### Goals
1. **G1:** From YouTube link to downloaded, captioned 9:16 clips in one guided flow, with **no API key**.
2. **G2:** An optional Gemini key clearly improves moment quality, titles and framing, and never breaks the flow when it fails.
3. **G3:** The UI matches NarraReel: one question per screen, a stepper and a live phone preview.
4. **G4:** One-command setup with Docker on Windows, macOS and Linux.

### Non-goals (v1)
- User accounts, billing and a hosted multi-user SaaS. Self-host only.
- Auto-posting to TikTok, Instagram or YouTube.
- A full timeline video editor. We only nudge cut start and end points.
- Generating videos from a topic, or adding stock B-roll.
- Cloning anyone's voice except the user's own.

## 5. User flow

| # | Screen | User does | System does |
|---|---|---|---|
| 1 | **Start** | Pastes a YouTube link (or uploads a file, or opens a saved project). Optionally switches AI mode to Advanced | Validates the URL, shows the AI mode badge |
| 2 | **Source** | Watches progress | Downloads, transcribes and analyses, with live progress (%) and a quality warning |
| 3 | **Moments** | Reviews the clip cards, selects or deselects them, nudges start and end, sets length band and count | Shows score, reason, title, time range and a preview for each clip |
| 4 | **Audio** (optional) | Keeps the original audio (default), or adds an AI voice-over for the hook | Previews the voice |
| 5 | **Captions** | Picks a preset, font, size and position | Live preview on a real frame of the clip |
| 6 | **Export** | Picks framing (Auto / Center / Split) and format, then clicks **Make clips** | Renders all clips with progress. Offers MP4, SRT and titles for download |

## 6. Functional requirements

Priority: **P0** = MVP, **P1** = v1, **P2** = later.

### 6.1 Input
| ID | Requirement | Pri |
|---|---|---|
| IN-1 | Accept youtube.com / youtu.be / Shorts URLs, single videos only (no playlists) | P0 |
| IN-2 | Accept a local video upload (mp4, mov, mkv, webm) | P1 |
| IN-3 | Show title, thumbnail, duration and resolution before processing. Warn below 720p | P0 |
| IN-4 | Optional YouTube cookies file for bot-check blocks | P1 |
| IN-5 | Reject non-video URLs and private network addresses (SSRF guard) | P0 |
| IN-6 | Configurable max source length (default 3 h) | P1 |

### 6.2 Transcription
| ID | Requirement | Pri |
|---|---|---|
| TR-1 | Local transcription with word-level timestamps (faster-whisper) | P0 |
| TR-2 | Auto-detect language. Support English, Hindi and other Whisper languages | P0 |
| TR-3 | Selectable model size (small default; large-v3-turbo on GPU) | P1 |
| TR-4 | Cache the transcript per video, so re-analysis doesn't re-transcribe | P0 |

### 6.3 Moment analysis
| ID | Requirement | Pri |
|---|---|---|
| AN-1 | **Free mode:** heuristic scorer, no network AI. See §7 | P0 |
| AN-2 | **Advanced mode:** Gemini 2-pass (score windows, then detail the shortlist) with structured output | P1 |
| AN-3 | **Local AI mode:** any OpenAI-compatible endpoint (Ollama, LM Studio) | P2 |
| AN-4 | Every moment has: start, end, score 0-100, one-line reason, title, hook text, keywords, provider | P0 |
| AN-5 | Clip length bands: 15-30 s, 30-60 s (default), 60-90 s | P0 |
| AN-6 | Clip count: automatic (based on duration) or a user-set number | P0 |
| AN-7 | Cuts snap to word boundaries and full sentences | P0 |
| AN-8 | If Gemini fails, fall back to the heuristic and show a "Used free analysis" badge | P1 |
| AN-9 | Re-run analysis with another provider without re-downloading | P1 |
| AN-10 | Silent or music-only video: heuristic uses heatmap and scene density; Advanced uses Gemini vision with a length guard | P2 |

### 6.4 Reframing
| ID | Requirement | Pri |
|---|---|---|
| RF-1 | **Center crop** to 9:16 | P0 |
| RF-2 | **Auto track:** follow the active speaker's face (MediaPipe) | P1 |
| RF-3 | **Split:** two speakers stacked, for podcasts | P1 |
| RF-4 | Advanced: Gemini layout picker chooses Track / Split / Screencast from 12 frames | P1 |
| RF-5 | Sources that are already vertical skip reframing | P0 |
| RF-6 | Formats: Reels 9:16 (default), Portrait 4:5, Square 1:1 | P1 |

### 6.5 Captions
| ID | Requirement | Pri |
|---|---|---|
| CP-1 | All 10 NarraReel presets on the clip's real speech timings | P0 |
| CP-2 | 19 fonts, size, position, uppercase, emoji toggle | P0 |
| CP-3 | Live preview frame from the actual clip | P0 |
| CP-4 | Keyword emphasis from analysis keywords (Advanced: Gemini-chosen) | P1 |
| CP-5 | Advanced: Gemini fixes misheard names and terms before the captions are built | P2 |
| CP-6 | Captions off option (clean export + SRT only) | P1 |

### 6.6 Audio (optional)
| ID | Requirement | Pri |
|---|---|---|
| AU-1 | Keep original audio (default) | P0 |
| AU-2 | AI voice-over of the hook text over the first seconds, with the original audio ducked | P2 |
| AU-3 | Loudness normalization (-14 LUFS) | P1 |

### 6.7 Export
| ID | Requirement | Pri |
|---|---|---|
| EX-1 | Render selected clips to MP4 (H.264/AAC) | P0 |
| EX-2 | Per clip: `.mp4`, `.srt`, plus one `titles.json` (title, descriptions, hook) per job | P0 |
| EX-3 | Download one clip or all as a zip | P1 |
| EX-4 | Re-render one clip with changed captions without re-analysing | P1 |
| EX-5 | Save and open a project file (`.snipmint.json`) | P1 |

### 6.8 Settings and keys
| ID | Requirement | Pri |
|---|---|---|
| ST-1 | Gemini key from `.env` or entered in the UI. Keys entered in the UI stay in the browser only | P1 |
| ST-2 | "Check key" button validates the key | P1 |
| ST-3 | The key is never logged, never written to project files, and never returned by the API | P0 |
| ST-4 | Model selection (default `gemini-3.1-flash-lite`) | P2 |
| ST-5 | Health endpoint reports the available AI modes, and the UI reflects them | P1 |

## 7. Free vs Advanced: what the user gets

| Capability | Free (default) | Advanced (Gemini key) |
|---|---|---|
| Moment picking | Rules: replay heatmap, chapters, hook phrases, loudness, keywords, sentence boundaries | Semantic: stories, punchlines, payoffs, self-contained check |
| Clip reason | Templated ("Most replayed section · strong question hook") | Natural sentence written by the model |
| Title / hook | Extracted from the transcript or the chapter title | Written per platform |
| Framing | Face tracking or center | + layout picked from frames (Split / Screencast) |
| Silent video | Heatmap or even splits | Vision-based picking |
| Cost | Free | A few cents per hour of video (text + 12 frames) |
| Privacy | Nothing leaves the machine except the YouTube download | The transcript and 12 frames go to Google |

## 8. Non-functional requirements

| ID | Requirement |
|---|---|
| NF-1 | **Setup:** `docker compose up` gives a working app at `http://localhost:8000` |
| NF-2 | **Performance (CPU, 8 cores):** a 30-min video gets its moments in ≤ 10 min (transcription dominates), and each 45 s clip renders in ≤ 60 s |
| NF-3 | **Performance (GPU profile):** the same 30-min video in ≤ 3 min |
| NF-4 | **Reliability:** a failed clip doesn't fail the whole job, and a restart resumes from the cached download and transcript |
| NF-5 | **Privacy:** no telemetry, and no network calls except YouTube, model downloads and (if enabled) Gemini |
| NF-6 | **Security:** localhost-only by default; token required when exposed. SSRF guard on URLs |
| NF-7 | **Accessibility:** keyboard navigable, visible focus, WCAG AA contrast (inherited from NarraReel) |
| NF-8 | **Licensing:** MIT. OpenShorts `cloud/` code is never included. Attribution in `NOTICE` |
| NF-9 | **Portability:** works on Windows (Docker Desktop + WSL2), macOS and Linux |

## 9. Success metrics

| Metric | Target |
|---|---|
| Time from link to first downloaded clip (30-min video, CPU) | < 15 min |
| Clips the user keeps out of those suggested (Free) | ≥ 40% |
| Clips the user keeps (Advanced) | ≥ 60% |
| Jobs that finish without error | ≥ 95% |
| Clips that start or end mid-sentence | < 5% |

## 10. Risks

| Risk | Mitigation |
|---|---|
| YouTube blocks downloads | Keep yt-dlp updatable, support a cookies file, give clear error messages |
| Heuristic picks feel random | Use heatmap data, tune the weights on 10-20 test videos, make the weights configurable |
| Heavy dependencies and a large image | Optional extras and compose profiles; CPU image stays about 2-3 GB |
| Gemini cost or quota surprises | Only text and frames are sent; estimate the cost before running; fall back to free |
| Copyright misuse | UI note: only clip content you own or have rights to |

## 11. Release milestones

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M1 Free MVP** | IN-1,3,5 · TR-1,2,4 · AN-1,4-7 · RF-1,5 · CP-1-3 · AU-1 · EX-1,2 · Docker | 20-min podcast link → 5+ captioned 9:16 clips, no key |
| **M2 Smart framing** | RF-2,3,6 · EX-3,4 | Speaker stays in frame in ≥ 90% of test clips |
| **M3 Advanced AI** | AN-2,8,9 · RF-4 · CP-4 · ST-1-3,5 | Gemini mode works end to end; a bad key falls back cleanly |
| **M4 Extras** | IN-2,4,6 · AN-3,10 · CP-5,6 · AU-2,3 · EX-5 · ST-4 | Feature complete for v1 |
