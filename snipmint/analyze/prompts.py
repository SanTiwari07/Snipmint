"""Prompts and response schemas for LLM moment picking (Gemini or a local model).

Ported from openshorts/gemini_worker.py (MIT, (c) 2024 OpenShorts): the two-pass
"score every window, then detail the shortlist" design and its prompt rules. Snipmint adds
the free signals (YouTube heatmap, loudness, chapter) to each window as hints.
"""

from __future__ import annotations

from pydantic import BaseModel


class ScoredWindow(BaseModel):
    id: str
    start: float
    end: float
    score: int
    reason: str = ""


class ScoreResponse(BaseModel):
    windows: list[ScoredWindow]


class DetailClip(BaseModel):
    start: float
    end: float
    source_window_id: str = ""
    predicted_score: int = 50
    video_description_for_tiktok: str = ""
    video_description_for_instagram: str = ""
    video_title_for_youtube_short: str = ""
    viral_hook_text: str = ""
    why: str = ""
    keywords: list[str] = []


class DetailResponse(BaseModel):
    shorts: list[DetailClip]


SCORE_PROMPT_TEMPLATE = """
You are a senior short-form video strategist.
RANK these candidate windows by how well each would work as a standalone short.

Rules:
- Return only valid JSON.
- Score EVERY window in this batch: exactly one entry per input window, with
  the id you were given. Do not drop the weak ones; say they are weak.
- `score` must be an integer from 0 to 100, and the ranking is what matters:
  use the whole range instead of clustering. Most windows of a normal video
  are not clippable, so reserve 70+ for the ones that pass the test below,
  and put weak filler, housekeeping, outros, rambling transitions and
  low-signal padding under 30 even when the topic is interesting.
- THE 2-SECOND TEST is the main criterion: would the first 2 seconds of this
  moment force a cold viewer (no context) to keep watching? Windows that only
  work with prior context score low.
- Prefer windows with strong hooks, conflict, surprise, emotion, novelty,
  big numbers, or a clear payoff.
- Each window may carry HINTS measured from the video: `replay` (0-1, how much
  YouTube viewers re-watched this part; null when unknown), `energy` (loudness
  relative to the rest of the video, 0 = average) and `chapter`. Treat them as
  evidence, not as the answer: the words decide.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_TITLE: {title}
VIDEO_DURATION_SECONDS: {video_duration}
WINDOWS_JSON:
{windows_json}

Return only:
{{
  "windows": [
    {{"id": "<window id>", "start": <number>, "end": <number>, "score": <integer 0-100>,
      "reason": "<very short reason>"}}
  ]
}}
"""

DETAIL_PROMPT_TEMPLATE = """
You are a senior short-form video editor and viral copywriter.
Choose the BEST short clips from these shortlisted candidate windows.

CLIP RULES:
- Return only valid JSON.
- Each clip must be {min_secs:g} to {max_secs:g} seconds long, in absolute seconds from the start of the source video.
- Stay within the candidate window boundaries.
- THE 2-SECOND RULE: the clip MUST open on its strongest moment. If the first
  2 seconds would not stop a cold viewer from scrolling, move the start or skip the clip.
- Start slightly before the hook and end slightly after the payoff when possible.
- Do not cut in the middle of a word or phrase.
- No generic intros/outros unless they are the hook.
- STANDS ALONE: the clip must make sense to someone who has seen nothing else.
  If it opens on a pronoun, a "that", a "so anyway", or an answer whose question
  was asked earlier, move the start back to where the idea begins or skip it.
  Fix this by moving the START earlier, never by cutting the ending short.
- HOW MANY: return {min_clips} to {max_clips} clips. Work through EVERY candidate
  window; they were already scored as the best moments in the video. Two or three
  clips from one window are fine when they are genuinely different moments. Only
  fall short of {min_clips} when the material truly does not hold them, and never
  pad with a clip you would not publish yourself.
- DIVERSITY: never return two clips that make the same point or tell the same story.

HOOK PLAYBOOK: pick the strongest fitting pattern for `viral_hook_text` (max 10 words):
- Open question: "Why does everyone get this wrong?"
- Hot take: "Stop doing this. Seriously."
- Number / fact shock: "97% of people miss this."
- Story loop: "This one email almost ruined me."
- POV / pattern interrupt: "POV: you finally understand it."
(These are English PATTERNS: always write the actual hook in TRANSCRIPT_LANGUAGE.)
- ABOUT THIS MOMENT, NOT THE VIDEO: the hook and the title name the concrete thing
  that happens inside this clip. If nothing concrete can be named, quote the clip's
  strongest sentence instead of summarising the topic.

COPY RULES: ALL text fields MUST be written in TRANSCRIPT_LANGUAGE ({language}):
- Descriptions (TikTok + Instagram): 1-2 punchy sentences that tease the payoff
  without spoiling it, then 3-5 topically relevant hashtags.
- `video_title_for_youtube_short`: max 100 chars, curiosity-driven, no fake claims.
- `predicted_score`: honest 0-100 estimate of viral potential.
- `why`: one sentence, max 20 words, naming what makes THIS moment worth a clip.
- `keywords`: 2-5 single words spoken in the clip that deserve visual emphasis in
  the captions (names, numbers, the key claim), copied exactly as spoken.

TRANSCRIPT_LANGUAGE: {language}
VIDEO_TITLE: {title}
VIDEO_DURATION_SECONDS: {video_duration}
CANDIDATE_WINDOWS_JSON:
{windows_json}

Return only:
{{
  "shorts": [
    {{"start": <number>, "end": <number>, "source_window_id": "<window id>",
      "predicted_score": <integer 0-100>,
      "video_description_for_tiktok": "<description + hashtags>",
      "video_description_for_instagram": "<description + hashtags>",
      "video_title_for_youtube_short": "<title max 100 chars>",
      "viral_hook_text": "<short overlay max 10 words>",
      "why": "<one sentence, max 20 words>",
      "keywords": ["<word>", "<word>"]}}
  ]
}}
"""


class VisualClip(BaseModel):
    start: float
    end: float
    predicted_score: int = 50
    video_description_for_tiktok: str = ""
    video_description_for_instagram: str = ""
    video_title_for_youtube_short: str = ""
    viral_hook_text: str = ""
    why: str = ""


class VisualResponse(BaseModel):
    shorts: list[VisualClip]


# Ported from openshorts/gemini_worker.py VISUAL_PROMPT_TEMPLATE (MIT, (c) 2024 OpenShorts).
VISUAL_PROMPT_TEMPLATE = """
You are a senior short-form video editor. This video has little or no speech: judge it by what
you SEE. Watch the whole thing and pick the {min_clips} to {max_clips} MOST engaging visual
moments for TikTok / Reels / Shorts (action, reveals, transformations, striking or funny shots,
satisfying payoffs, dramatic movement).

TIME CONTRACT (STRICT):
- Timestamps in ABSOLUTE SECONDS from the start, numbers with up to 3 decimals.
- 0 <= start < end <= {video_duration}.
- Each clip {min_secs:g} to {max_secs:g} seconds long. If the whole video is shorter than
  {min_secs:g}s, return one clip spanning the full video.
- Cut on visual scene changes, never mid-motion.

For each clip write catchy copy in {language}: a scroll-stopping hook (max 10 words), a TikTok
and an Instagram description, a YouTube title (max 100 chars) and `why` (one sentence on what
makes it worth a clip). Order clips best to worst.
VIDEO_TITLE: {title}
"""


class WordFix(BaseModel):
    i: int
    text: str


class FixResponse(BaseModel):
    fixes: list[WordFix]


FIX_PROMPT_TEMPLATE = """
You proofread automatic speech recognition for on-screen captions.
Below are the words of short clips from a video titled "{title}" (language: {language}), each as
[index, word]. Fix ONLY words the recogniser clearly got wrong: misheard names of people, brands,
products, places and technical terms, and obvious mis-hearings that make no sense in context.

Rules:
- Return only valid JSON: {{"fixes": [{{"i": <index>, "text": "<corrected word>"}}]}}.
- One word in, one word out: never merge, split, add or remove words; keep the punctuation
  attached to the word as it was.
- Do not rephrase, do not fix grammar or style, do not change words that are plausible.
- When unsure, leave the word alone. An empty list is a fine answer.

CLIPS_JSON:
{clips_json}
"""
