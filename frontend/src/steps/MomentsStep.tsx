import { useEffect, useRef, useState } from 'react'
import { watchJob } from '../api/client'
import {
  type Band,
  LENGTH_BANDS,
  type Moment,
  clipsApi,
  clock,
  getAiMode,
  providerFor,
  sourceVideoUrl,
} from '../api/clips'
import { Banner, Button, ProgressBar, Segmented } from '../components/ui'
import { edited, useClips } from '../state/clips'
import { useSession } from '../state/session'

const COUNTS = [
  { id: 'auto', label: 'Auto' },
  { id: '3', label: '3' },
  { id: '5', label: '5' },
  { id: '8', label: '8' },
  { id: '10', label: '10' },
] as const

const PROVIDER_NAME = { heuristic: 'Free analysis', gemini: 'Gemini', local_llm: 'Local AI' } as const

function ClipCard({ m, on, index }: { m: Moment; on: boolean; index: number }) {
  const { toggle, nudge, sourceId, edits } = useClips()
  const shown = edited(m, edits)
  const video = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  const nudged = !!edits[m.id]

  const play = () => {
    const v = video.current
    if (!v) return
    if (!v.paused) {
      v.pause()
      return
    }
    v.currentTime = shown.start
    void v.play()
  }

  return (
    <li className={`clip-card ${on ? 'clip-card--on' : ''}`}>
      <div className="clip-card__media">
        {sourceId && (
          <video
            ref={video}
            className="clip-card__video"
            src={`${sourceVideoUrl(sourceId)}#t=${shown.start.toFixed(1)}`}
            preload="metadata"
            playsInline
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onTimeUpdate={(e) => {
              if (e.currentTarget.currentTime >= shown.end) e.currentTarget.pause()
            }}
          />
        )}
        <button type="button" className="clip-card__play" onClick={play} aria-label={playing ? 'Pause' : `Play moment ${index}`}>
          {playing ? '❚❚' : '▶'}
        </button>
        <span className="clip-card__score" title="How strong this moment looks (0–100)">
          {m.score}
        </span>
      </div>
      <div className="clip-card__body">
        <label className="clip-card__pick">
          <input type="checkbox" checked={on} onChange={() => toggle(m.id)} />
          <span className="clip-card__title">{m.title || `Moment ${index}`}</span>
        </label>
        {m.why && <p className="clip-card__why">{m.why}</p>}
        {m.hook_text && <p className="clip-card__hook">“{m.hook_text}”</p>}
        <div className="clip-card__time">
          <span className="clip-card__range">
            {clock(shown.start)}–{clock(shown.end)} · {Math.round(shown.end - shown.start)} s{nudged ? ' (adjusted)' : ''}
          </span>
          <span className="clip-card__nudges" role="group" aria-label="Adjust start and end">
            <button type="button" className="chip" onClick={() => nudge(m, 'start', -1)} aria-label="Start one second earlier">
              −1s start
            </button>
            <button type="button" className="chip" onClick={() => nudge(m, 'start', 1)} aria-label="Start one second later">
              +1s start
            </button>
            <button type="button" className="chip" onClick={() => nudge(m, 'end', -1)} aria-label="End one second earlier">
              −1s end
            </button>
            <button type="button" className="chip" onClick={() => nudge(m, 'end', 1)} aria-label="End one second later">
              +1s end
            </button>
          </span>
        </div>
        {m.text && (
          <details className="clip-card__text">
            <summary>Transcript</summary>
            <p>{m.text}</p>
          </details>
        )}
        {m.fallback && <span className="badge badge--warn">Free analysis (Gemini failed)</span>}
      </div>
    </li>
  )
}

/** Pick the moments to turn into clips. */
export function MomentsStep() {
  const { source, sourceId, band, count, selected, patch, refresh, selectAll } = useClips()
  const toast = useSession((s) => s.toast)
  const [running, setRunning] = useState<{ fraction: number; message: string } | null>(null)
  const stop = useRef<(() => void) | null>(null)
  useEffect(() => {
    if (!source) void refresh()
    return () => stop.current?.()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const analysis = source?.analysis
  const moments = analysis?.moments ?? []
  const changed =
    !!analysis &&
    (analysis.options.min_seconds !== LENGTH_BANDS[band].min ||
      analysis.options.max_seconds !== LENGTH_BANDS[band].max ||
      (analysis.options.count ?? null) !== count)

  const again = async () => {
    if (!sourceId) return
    const b = LENGTH_BANDS[band]
    setRunning({ fraction: 0, message: 'Starting' })
    try {
      const { task_id } = await clipsApi.analyze(sourceId, {
        provider: providerFor(getAiMode()),
        min_seconds: b.min,
        max_seconds: b.max,
        count,
      })
      stop.current = watchJob(
        task_id,
        (e) => setRunning({ fraction: e.fraction, message: e.message }),
        async (end) => {
          setRunning(null)
          await refresh()
          if (end.status === 'done') toast('success', 'New moments are ready.')
          else toast('error', end.error ?? 'Could not find moments.')
        },
        () => {
          setRunning(null)
          void refresh()
        },
        'tasks',
      )
    } catch (e) {
      setRunning(null)
      toast('error', e instanceof Error ? e.message : 'Could not start.')
    }
  }

  return (
    <div className="step">
      <header className="step-head">
        <h1 className="display">Which moments should become clips?</h1>
        <p className="lede">
          The best ones are picked already. Play any of them, nudge where it starts or ends, and tick the ones you want.
        </p>
      </header>

      <section className="section" aria-labelledby="length-title">
        <div className="section__head">
          <h2 className="section__title" id="length-title">
            Clip length and number
          </h2>
          <p className="section__hint">Change these and find the moments again.</p>
        </div>
        <div className="moment-controls">
          <Segmented<Band>
            label="Clip length"
            value={band}
            onChange={(b) => patch({ band: b })}
            items={(Object.keys(LENGTH_BANDS) as Band[]).map((b) => ({ id: b, label: LENGTH_BANDS[b].label }))}
          />
          <Segmented
            label="Number of clips"
            value={count === null ? 'auto' : String(count)}
            onChange={(v) => patch({ count: v === 'auto' ? null : Number(v) })}
            items={COUNTS.map((c) => ({ id: c.id, label: c.label }))}
          />
          <Button variant={changed ? 'primary' : 'secondary'} busy={!!running} onClick={again}>
            Find moments again
          </Button>
        </div>
        {running && (
          <div className="stack">
            <ProgressBar fraction={running.fraction} label="Finding moments" />
            <p className="field__hint">{running.message}</p>
          </div>
        )}
      </section>

      {analysis?.fallback_reason && (
        <Banner tone="warn" title="Used the free analysis">
          {analysis.fallback_reason}
        </Banner>
      )}

      {moments.length > 0 ? (
        <section className="section" aria-labelledby="moments-title">
          <div className="section__head">
            <h2 className="section__title" id="moments-title">
              {moments.length} moments · {selected.length} picked
            </h2>
            <p className="section__hint">
              Picked by {analysis ? PROVIDER_NAME[analysis.provider] : '…'}.{' '}
              <button type="button" className="link-btn" onClick={() => selectAll(moments.map((m) => m.id))}>
                Pick all
              </button>{' '}
              ·{' '}
              <button type="button" className="link-btn" onClick={() => selectAll([])}>
                None
              </button>
            </p>
          </div>
          <ol className="clip-list">
            {moments.map((m, i) => (
              <ClipCard key={m.id} m={m} index={i + 1} on={selected.includes(m.id)} />
            ))}
          </ol>
        </section>
      ) : (
        !running && <p className="empty">No moments yet. Go back to the video step to see how it is going.</p>
      )}
    </div>
  )
}
