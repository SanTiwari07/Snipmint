import { getJson, json, mediaUrl, send } from './client'

export type StageName = 'probe' | 'download' | 'audio' | 'transcribe' | 'signals' | 'analyze'
export type StageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'
export type ProviderName = 'heuristic' | 'local_llm' | 'gemini'
export type ProviderChoice = 'auto' | ProviderName

export interface SourceMeta {
  id: string
  url: string
  video_id: string | null
  title: string
  channel: string
  duration: number
  width: number | null
  height: number | null
  thumbnail: string | null
  chapters: { start: number; end: number; title: string }[]
  heatmap: { start: number; end: number; value: number }[]
  language: string | null
}

export interface Moment {
  id: string
  start: number
  end: number
  score: number
  why: string
  title: string
  hook_text: string
  descriptions: Record<string, string>
  keywords: string[]
  provider: ProviderName
  fallback: boolean
  layout: string | null
  text: string
}

export interface AnalyzeOptions {
  provider: ProviderChoice
  min_seconds: number
  max_seconds: number
  count: number | null
}

export interface AnalysisResult {
  provider: ProviderName
  requested: ProviderChoice
  fallback_reason: string | null
  options: AnalyzeOptions
  moments: Moment[]
  cost_usd: number | null
}

export interface TaskInfo {
  id: string
  kind: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  error: string | null
  fraction: number
  message: string
  stage: string | null
}

export interface SourceState {
  source_id: string
  meta: SourceMeta | null
  vertical: boolean
  status: { stages: Record<StageName, StageState>; error: string | null; task_id: string | null; warnings: string[] }
  has_video: boolean
  has_transcript: boolean
  analysis: AnalysisResult | null
  task: TaskInfo | null
}

export interface ClipSpec {
  id: string
  start: number
  end: number
  title: string
  hook_text: string
  keywords: string[]
  descriptions: Record<string, string>
}

export interface CaptionsConfig {
  preset: string
  overrides: Record<string, unknown>
  font: string | null
  keep_emoji?: boolean
}

export interface ClipsRequest {
  source_id: string
  clips: ClipSpec[]
  captions: CaptionsConfig
  captions_enabled: boolean
  format: string
  framing: 'center' | 'track' | 'split'
  export: { quality: 'fast' | 'balanced' | 'high' }
}

export interface ClipOutput {
  id: string
  index: number
  title: string
  start: number
  end: number
  video: string | null
  srt: string | null
  error: string | null
}

export interface ClipsJob {
  id: string
  status: TaskInfo['status']
  error: string | null
  result: { clips: ClipOutput[] } | null
}

// ------------------------------------------------------------------ Gemini key (this browser only)

const KEY_STORE = 'snipmint.geminiKey'
const MODE_STORE = 'snipmint.aiMode'
export type AiMode = 'free' | 'advanced'

export function getGeminiKey(): string {
  try {
    return window.localStorage.getItem(KEY_STORE) ?? ''
  } catch {
    return ''
  }
}

export function setGeminiKey(key: string) {
  try {
    if (key) window.localStorage.setItem(KEY_STORE, key)
    else window.localStorage.removeItem(KEY_STORE)
  } catch {
    /* storage blocked: the key is used for this request only */
  }
}

export function getAiMode(): AiMode {
  try {
    return window.localStorage.getItem(MODE_STORE) === 'advanced' ? 'advanced' : 'free'
  } catch {
    return 'free'
  }
}

export function setAiMode(mode: AiMode) {
  try {
    window.localStorage.setItem(MODE_STORE, mode)
  } catch {
    /* ignore */
  }
}

/** Header with the Gemini key, only in Advanced mode (the server falls back to its own key). */
function aiHeaders(): Record<string, string> {
  const key = getGeminiKey().trim()
  return getAiMode() === 'advanced' && key ? { 'X-Gemini-Key': key } : {}
}

function postWithAi<T>(path: string, body: unknown): Promise<T> {
  const init = json(body)
  return send(path, { ...init, headers: { ...(init.headers as Record<string, string>), ...aiHeaders() } }).then(
    (r) => r.json() as Promise<T>,
  )
}

export function providerFor(mode: AiMode): ProviderChoice {
  return mode === 'advanced' ? 'gemini' : 'heuristic'
}

// ------------------------------------------------------------------ calls

export const clipsApi = {
  createSource: (url: string, analysis: AnalyzeOptions) =>
    postWithAi<{ source_id: string; task_id: string }>('/api/sources', { url, analysis }),
  source: (id: string, signal?: AbortSignal) => getJson<SourceState>(`/api/sources/${encodeURIComponent(id)}`, signal),
  analyze: (id: string, opts: AnalyzeOptions) =>
    postWithAi<{ task_id: string }>(`/api/sources/${encodeURIComponent(id)}/analyze`, opts),
  preview: async (
    body: {
      source_id: string
      start: number
      end: number
      keywords: string[]
      captions: CaptionsConfig
      captions_enabled: boolean
      format: string
    },
    signal?: AbortSignal,
  ): Promise<Blob> => (await send('/api/clips/preview', { ...json(body), signal })).blob(),
  createClips: (req: ClipsRequest) => postWithAi<{ job_id: string }>('/api/clips', req),
  job: (id: string) => getJson<ClipsJob>(`/api/jobs/${id}`),
  checkKey: async (key: string): Promise<{ ok: boolean; error: string | null }> => {
    const init = json({})
    return (
      await send('/api/ai/check-key', {
        ...init,
        headers: { ...(init.headers as Record<string, string>), 'X-Gemini-Key': key },
      })
    ).json()
  },
}

export const sourceVideoUrl = (id: string) => mediaUrl(`/api/sources/${encodeURIComponent(id)}/video`)
export const clipFileUrl = (jobId: string, name: string, download = false) =>
  mediaUrl(`/api/clips/${jobId}/files/${name}${download ? '?download=true' : ''}`)

// ------------------------------------------------------------------ helpers

export const LENGTH_BANDS = {
  short: { label: '15–30 s', min: 15, max: 30 },
  medium: { label: '30–60 s', min: 30, max: 60 },
  long: { label: '60–90 s', min: 60, max: 90 },
} as const
export type Band = keyof typeof LENGTH_BANDS

export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

export const STAGE_LABELS: Record<StageName, string> = {
  probe: 'Reading video details',
  download: 'Downloading',
  audio: 'Extracting audio',
  transcribe: 'Transcribing every word',
  signals: 'Measuring energy and replays',
  analyze: 'Finding the best moments',
}
export const STAGE_ORDER: StageName[] = ['probe', 'download', 'audio', 'transcribe', 'signals', 'analyze']
