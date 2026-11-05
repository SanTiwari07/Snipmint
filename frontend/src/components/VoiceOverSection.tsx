import { useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { useCatalog } from '../state/catalog'
import { previewMoment, useClips } from '../state/clips'
import { useSession } from '../state/session'
import { Button, Switch } from './ui'

/** An AI voice reads each clip's hook over its first seconds; the clip's own sound dips. */
export function VoiceOverSection() {
  const voices = useCatalog((s) => s.voices)
  const engines = useCatalog((s) => s.engines)
  const { voiceover, patch, source, selected, edits } = useClips()
  const toast = useSession((s) => s.toast)
  const [busy, setBusy] = useState(false)
  const audio = useRef<HTMLAudioElement | null>(null)
  const sample = useMemo(
    () => previewMoment({ source, selected, edits })?.hook_text || 'This is how the hook will sound.',
    [source, selected, edits],
  )
  const usable = useMemo(() => {
    const ready = new Set(engines.filter((e) => e.status.available && !e.status.needs_download).map((e) => e.id))
    return voices.filter((v) => ready.size === 0 || ready.has(v.engine))
  }, [voices, engines])
  const set = (p: Partial<typeof voiceover>) => patch({ voiceover: { ...voiceover, ...p } })
  const current = `${voiceover.engine}:${voiceover.voice_id}`

  const listen = async () => {
    setBusy(true)
    try {
      const blob = await api.voicePreview(sample, {
        engine: voiceover.engine,
        voice_id: voiceover.voice_id,
        speed: 1,
        energy: 0.6,
      })
      audio.current?.pause()
      audio.current = new Audio(URL.createObjectURL(blob))
      await audio.current.play()
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not play the voice.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="section" aria-labelledby="voice-title">
      <div className="section__head">
        <h2 className="section__title" id="voice-title">
          Voice-over
        </h2>
        <p className="section__hint">Optional. An AI voice reads the hook at the start of each clip.</p>
      </div>
      <Switch
        label="Read the hook aloud"
        hint="The clip's own sound dips while the voice speaks, then comes back."
        checked={voiceover.enabled}
        onChange={(v) => set({ enabled: v })}
      />
      {voiceover.enabled && (
        <div className="link-form__row">
          <select
            className="select"
            aria-label="Voice"
            value={current}
            onChange={(e) => {
              const [engine, ...rest] = e.target.value.split(':')
              set({ engine, voice_id: rest.join(':') })
            }}
          >
            {!usable.some((v) => `${v.engine}:${v.id}` === current) && <option value={current}>{voiceover.voice_id}</option>}
            {usable.map((v) => (
              <option key={`${v.engine}:${v.id}`} value={`${v.engine}:${v.id}`}>
                {v.name} · {v.language}
                {v.custom ? ' · your voice' : v.offline ? ' · offline' : ''}
              </option>
            ))}
          </select>
          <Button variant="secondary" busy={busy} onClick={listen}>
            Listen
          </Button>
        </div>
      )}
    </section>
  )
}
