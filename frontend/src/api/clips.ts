import { ApiError, getJson, getToken, json, mediaUrl, send } from './client'

export type StageName = 'probe' | 'download' | 'audio' | 'transcribe' | 'signals' | 'analyze'
export type StageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'
export type ProviderName = 'heuristic' | 'local_llm' | 'gemini'
export type ProviderChoice = 'auto' | ProviderName
export type Framing = 'auto' | 'track' | 'split' | 'fit' | 'center'

/** How each finished clip was framed (shown on the result cards). */
export const LAYOUT_LABEL: Record<string, string> = {
  track: 'Follows the speaker',
  split: 'Two people stacked',
  mixed: 'Two people + close-ups',
  fit: 'Whole picture',
  center: 'Centre crop',
}

export const FRAMINGS: { id: Framing; label: string; hint: string }[] = [
  { id: 'auto', label: 'Auto', hint: 'Picks for each clip: follow the speaker, two people stacked, or the whole picture.' },
  { id: 'track', label: 'Follow speaker', hint: 'A vertical window that follows the face of whoever is on screen.' },
  { id: 'split', label: 'Two people', hint: 'Two people side by side become two stacked halves; close-ups in between follow the speaker.' },
  { id: 'fit', label: 'Whole picture', hint: 'Nothing is cut off: the full frame over a blurred copy. Good for screens and slides.' },
  { id: 'center', label: 'Centre', hint: 'A fixed crop from the middle of the picture.' },
]

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
  framing: Framing
  export: { quality: 'fast' | 'balanced' | 'high'; loudness: boolean }
  voiceover: { enabled: boolean; voice: { engine: string; voice_id: string } }
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
  layout: 'center' | 'track' | 'split' | 'fit' | 'mixed' | null
  framing_note: string | null
  voiceover: boolean
  note: string | null
}

export interface ClipsJob {
  id: string
  status: TaskInfo['status']
  error: string | null
  result: { clips: ClipOutput[] } | null
}

// ------------------------------------------------------------------ AI mode (the Gemini key lives in .env)

const MODE_STORE = 'snipmint.aiMode'
export type AiMode = 'free' | 'advanced'

/** Earlier versions kept a Gemini key in this browser; the key now lives only in .env. */
export function forgetBrowserKey() {
  try {
    window.localStorage.removeItem('snipmint.geminiKey')
  } catch {
    /* ignore */
  }
}

const MODEL_STORE = 'snipmint.geminiModel'

export function getGeminiModel(): string {
  try {
    return window.localStorage.getItem(MODEL_STORE) ?? ''
  } catch {
    return ''
  }
}

export function setGeminiModel(model: string) {
  try {
    if (model) window.localStorage.setItem(MODEL_STORE, model)
    else window.localStorage.removeItem(MODEL_STORE)
  } catch {
    /* ignore */
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

/** The chosen Gemini model, in Advanced mode. The key itself never leaves the server. */
function aiHeaders(): Record<string, string> {
  if (getAiMode() !== 'advanced') return {}
  const model = getGeminiModel().trim()
  return model ? { 'X-Gemini-Model': model } : {}
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
      framing: Framing
    },
    signal?: AbortSignal,
  ): Promise<Blob> => (await send('/api/clips/preview', { ...json(body), signal })).blob(),
  prepare: (id: string, opts: AnalyzeOptions) =>
    postWithAi<{ source_id: string; task_id: string }>(`/api/sources/${encodeURIComponent(id)}/prepare`, opts),
  /** Upload a video file with progress (fetch cannot report upload progress, XHR can). */
  upload: (file: File, analysis: AnalyzeOptions, onProgress: (fraction: number) => void) =>
    new Promise<{ source_id: string; task_id: string }>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/sources/upload')
      const token = getToken()
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      for (const [k, v] of Object.entries(aiHeaders())) xhr.setRequestHeader(k, v)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total)
      }
      xhr.onload = () => {
        let body: { error?: string; type?: string } & Record<string, unknown> = {}
        try {
          body = JSON.parse(xhr.responseText)
        } catch {
          /* not JSON */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as { source_id: string; task_id: string })
        else reject(new ApiError(body.error ?? `The upload failed (${xhr.status}).`, xhr.status, body.type ?? 'HTTPError'))
      }
      xhr.onerror = () => reject(new ApiError('Could not reach the Snipmint server.', 0, 'NetworkError'))
      const form = new FormData()
      form.append('file', file)
      form.append('analysis', JSON.stringify(analysis))
      xhr.send(form)
    }),
  createClips: (req: ClipsRequest) => postWithAi<{ job_id: string }>('/api/clips', req),
  job: (id: string) => getJson<ClipsJob>(`/api/jobs/${id}`),
  /** Test the server's own Gemini key (from .env). */
  checkKey: async (): Promise<{ ok: boolean; error: string | null }> =>
    (await send('/api/ai/check-key', json({}))).json(),
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
