import type { Band, Framing } from '../api/clips'
import { LENGTH_BANDS } from '../api/clips'
import type { ClipsDraft } from '../state/clips'
import type { Draft } from '../state/project'

/** A saved Snipmint project: the video, the picked moments and every choice. Never a key. */
export interface ProjectFile {
  snipmint_project: 1
  saved_at: string
  source: { id: string; url: string; title: string }
  analysis: { band: Band; count: number | null }
  selected: string[]
  edits: Record<string, { start: number; end: number }>
  captions: Draft['captions']
  captions_enabled: boolean
  format: string
  framing: Framing
  export: { quality: ClipsDraft['quality']; loudness: boolean }
  voiceover: ClipsDraft['voiceover']
}

export function toProjectFile(clips: ClipsDraft, draft: Pick<Draft, 'captions' | 'format'>, title: string): ProjectFile {
  if (!clips.sourceId) throw new Error('There is no video to save yet.')
  return {
    snipmint_project: 1,
    saved_at: new Date().toISOString(),
    source: { id: clips.sourceId, url: clips.url, title },
    analysis: { band: clips.band, count: clips.count },
    selected: clips.selected,
    edits: clips.edits,
    captions: draft.captions,
    captions_enabled: clips.captionsEnabled,
    format: draft.format,
    framing: clips.framing,
    export: { quality: clips.quality, loudness: clips.loudness },
    voiceover: clips.voiceover,
  }
}

const FRAMINGS = new Set(['auto', 'track', 'split', 'fit', 'center'])
const QUALITIES = new Set(['fast', 'balanced', 'high'])

/** Parse and check a project file; throws an Error with a readable message. */
export function parseProjectFile(text: string): ProjectFile {
  let data: Partial<ProjectFile>
  try {
    data = JSON.parse(text)
  } catch {
    throw new Error('That file is not a Snipmint project (it is not JSON).')
  }
  if (!data || data.snipmint_project !== 1 || !data.source?.id || typeof data.source.url !== 'string') {
    throw new Error('That file is not a Snipmint project.')
  }
  const band = data.analysis?.band && data.analysis.band in LENGTH_BANDS ? data.analysis.band : 'medium'
  return {
    snipmint_project: 1,
    saved_at: String(data.saved_at ?? ''),
    source: { id: String(data.source.id), url: data.source.url, title: String(data.source.title ?? '') },
    analysis: { band, count: typeof data.analysis?.count === 'number' ? data.analysis.count : null },
    selected: Array.isArray(data.selected) ? data.selected.map(String) : [],
    edits: data.edits && typeof data.edits === 'object' ? data.edits : {},
    captions: {
      preset: data.captions?.preset ?? 'bold-pop',
      overrides: data.captions?.overrides ?? {},
      font: data.captions?.font ?? null,
    },
    captions_enabled: data.captions_enabled !== false,
    format: data.format ?? 'reels',
    framing: FRAMINGS.has(String(data.framing)) ? (data.framing as Framing) : 'auto',
    export: {
      quality: QUALITIES.has(String(data.export?.quality)) ? (data.export!.quality as ClipsDraft['quality']) : 'balanced',
      loudness: data.export?.loudness !== false,
    },
    voiceover: {
      enabled: !!data.voiceover?.enabled,
      engine: data.voiceover?.engine ?? 'edge',
      voice_id: data.voiceover?.voice_id ?? 'en-US-AvaNeural',
    },
  }
}

export function downloadProjectFile(project: ProjectFile) {
  const stem = (project.source.title || 'snipmint').replace(/[^\w\- ]+/g, '').trim().slice(0, 40) || 'snipmint'
  const blob = new Blob([JSON.stringify(project, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${stem}.snipmint.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(a.href), 1000)
}
