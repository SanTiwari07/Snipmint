import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import { type Band, type ClipSpec, type Framing, type Moment, type SourceState, clipsApi } from '../api/clips'

export interface ClipsDraft {
  url: string
  sourceId: string | null
  taskId: string | null
  band: Band
  count: number | null
  selected: string[]
  /** Hand-nudged start/end per moment id. */
  edits: Record<string, { start: number; end: number }>
  captionsEnabled: boolean
  framing: Framing
  quality: 'fast' | 'balanced' | 'high'
  loudness: boolean
  voiceover: { enabled: boolean; engine: string; voice_id: string }
  jobId: string | null
  /** Picks from an opened project, applied once its moments are loaded. */
  pending: { selected: string[]; edits: ClipsDraft['edits'] } | null
}

const DEFAULT: ClipsDraft = {
  url: '',
  sourceId: null,
  taskId: null,
  band: 'medium',
  count: null,
  selected: [],
  edits: {},
  captionsEnabled: true,
  framing: 'auto',
  quality: 'balanced',
  loudness: true,
  voiceover: { enabled: false, engine: 'edge', voice_id: 'en-US-AvaNeural' },
  jobId: null,
  pending: null,
}

interface ClipsStore extends ClipsDraft {
  /** Latest state of the source from the server (not persisted). */
  source: SourceState | null
  sourceError: string | null
  patch: (p: Partial<ClipsDraft>) => void
  startSource: (url: string, sourceId: string, taskId: string) => void
  refresh: () => Promise<SourceState | null>
  setSource: (s: SourceState | null) => void
  toggle: (id: string) => void
  selectAll: (ids: string[]) => void
  nudge: (m: Moment, edge: 'start' | 'end', delta: number) => void
  reset: () => void
}

export const useClips = create<ClipsStore>()(
  persist(
    (set, get) => ({
      ...DEFAULT,
      source: null,
      sourceError: null,
      patch: (p) => set(p),
      startSource: (url, sourceId, taskId) =>
        set({ url, sourceId, taskId, selected: [], edits: {}, jobId: null, source: null, sourceError: null }),
      refresh: async () => {
        const id = get().sourceId
        if (!id) return null
        try {
          const s = await clipsApi.source(id)
          get().setSource(s)
          return s
        } catch (e) {
          set({ sourceError: e instanceof Error ? e.message : String(e) })
          return null
        }
      },
      setSource: (s) => {
        const prevIds = new Set((get().source?.analysis?.moments ?? []).map((m) => m.id))
        const moments = s?.analysis?.moments ?? []
        const ids = moments.map((m) => m.id)
        const sameSet = ids.length === prevIds.size && ids.every((id) => prevIds.has(id))
        // a new analysis pre-selects the three best moments
        const pending = get().pending
        if (pending && moments.length && pending.selected.every((id) => ids.includes(id))) {
          set({ source: s, sourceError: null, selected: pending.selected, edits: pending.edits, pending: null })
          return
        }
        const selected =
          sameSet && get().selected.length
            ? get().selected.filter((id) => ids.includes(id))
            : moments.slice(0, Math.min(3, moments.length)).map((m) => m.id)
        set({ source: s, sourceError: null, selected, edits: sameSet ? get().edits : {} })
      },
      toggle: (id) =>
        set((s) => ({ selected: s.selected.includes(id) ? s.selected.filter((x) => x !== id) : [...s.selected, id] })),
      selectAll: (ids) => set({ selected: ids }),
      nudge: (m, edge, delta) =>
        set((s) => {
          const cur = s.edits[m.id] ?? { start: m.start, end: m.end }
          const dur = s.source?.meta?.duration ?? Number.POSITIVE_INFINITY
          const next = { ...cur }
          if (edge === 'start') next.start = Math.min(Math.max(0, cur.start + delta), cur.end - 3)
          else next.end = Math.max(Math.min(dur, cur.end + delta), cur.start + 3)
          return { edits: { ...s.edits, [m.id]: next } }
        }),
      reset: () => set({ ...DEFAULT, source: null, sourceError: null }),
    }),
    {
      name: 'snipmint.clips',
      version: 3,
      // an older saved shape: start fresh (it only holds this tab's session)
      migrate: () => ({ ...DEFAULT }) as unknown as ClipsStore,
      storage: createJSONStorage(() => {
        try {
          window.sessionStorage.setItem('snipmint.probe', '1')
          window.sessionStorage.removeItem('snipmint.probe')
          return window.sessionStorage
        } catch {
          return { getItem: () => null, setItem: () => {}, removeItem: () => {} }
        }
      }),
      partialize: (s) => {
        const { url, sourceId, taskId, band, count, selected, edits, captionsEnabled, framing, quality, loudness, voiceover, jobId, pending } = s
        return { url, sourceId, taskId, band, count, selected, edits, captionsEnabled, framing, quality, loudness, voiceover, jobId, pending }
      },
    },
  ),
)

/** A moment with the person's nudges applied. */
export function edited(m: Moment, edits: ClipsDraft['edits']): Moment {
  const e = edits[m.id]
  return e ? { ...m, start: e.start, end: e.end } : m
}

/** The moments the person picked, in time order, as clip specs for the render request. */
export function selectedClips(state: Pick<ClipsStore, 'source' | 'selected' | 'edits'>): ClipSpec[] {
  const moments = state.source?.analysis?.moments ?? []
  return moments
    .filter((m) => state.selected.includes(m.id))
    .map((m) => edited(m, state.edits))
    .sort((a, b) => a.start - b.start)
    .map((m) => ({
      id: m.id,
      start: m.start,
      end: m.end,
      title: m.title,
      hook_text: m.hook_text,
      keywords: m.keywords,
      descriptions: m.descriptions,
    }))
}

/** The clip shown in the phone preview: the best selected moment (or the best overall). */
export function previewMoment(state: Pick<ClipsStore, 'source' | 'selected' | 'edits'>): Moment | null {
  const moments = state.source?.analysis?.moments ?? []
  const m = moments.find((x) => state.selected.includes(x.id)) ?? moments[0]
  return m ? edited(m, state.edits) : null
}
