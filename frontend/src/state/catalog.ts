import { create } from 'zustand'
import { api } from '../api/client'
import type { EngineInfo, FontInfo, FormatInfo, Health, StylePreset, Voice } from '../api/types'

interface Catalog {
  loaded: boolean
  error: string | null
  health: Health | null
  formats: FormatInfo[]
  styles: StylePreset[]
  fonts: FontInfo[]
  voices: Voice[]
  engines: EngineInfo[]
  load: () => Promise<void>
  refreshFonts: () => Promise<void>
  refreshStyles: () => Promise<void>
  refreshVoices: () => Promise<void>
  refreshEngines: () => Promise<void>
}

let inflight: Promise<void> | null = null

const FORMAT_ORDER = ['reels', 'youtube', 'square', 'portrait', 'audio']
const byFormatOrder = (a: FormatInfo, b: FormatInfo) =>
  (FORMAT_ORDER.indexOf(a.id) + 1 || 99) - (FORMAT_ORDER.indexOf(b.id) + 1 || 99)

export const useCatalog = create<Catalog>((set, get) => ({
  loaded: false,
  error: null,
  health: null,
  formats: [],
  styles: [],
  fonts: [],
  voices: [],
  engines: [],
  load: () => {
    if (get().loaded) return Promise.resolve()
    inflight ??= (async () => {
      try {
        const [health, formats, styles, fonts, engines] = await Promise.all([
          api.health(),
          api.formats(),
          api.styles(),
          api.fonts(),
          api.engines(),
        ])
        set({ health, formats: formats.sort(byFormatOrder), styles, fonts, engines, loaded: true, error: null })
        // voices come from the network for Edge; do not block the first paint on them
        api
          .voices()
          .then((voices) => set({ voices }))
          .catch(() => {})
      } catch (e) {
        set({ error: e instanceof Error ? e.message : String(e) })
      } finally {
        inflight = null
      }
    })()
    return inflight
  },
  refreshFonts: async () => set({ fonts: await api.fonts() }),
  refreshStyles: async () => set({ styles: await api.styles() }),
  refreshVoices: async () => set({ voices: await api.voices() }),
  refreshEngines: async () => set({ engines: await api.engines() }),
}))
