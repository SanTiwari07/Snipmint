import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useNavigate } from 'react-router-dom'
import { api, watchJob } from '../api/client'
import { type ClipsJob, FRAMINGS, LAYOUT_LABEL, clipFileUrl, clipsApi, clock } from '../api/clips'
import { Banner, Button, ProgressBar, Segmented, Switch } from '../components/ui'
import { useCatalog } from '../state/catalog'
import { selectedClips, useClips } from '../state/clips'
import { useDraft } from '../state/project'
import { useSession } from '../state/session'

interface Live {
  fraction: number
  message: string
}

/** Make the clips, then watch and download them. */
export function ExportStep() {
  const navigate = useNavigate()
  const toast = useSession((s) => s.toast)
  const draft = useDraft()
  const styles = useCatalog((s) => s.styles)
  const formats = useCatalog((s) => s.formats)
  const health = useCatalog((s) => s.health)
  const { source, selected, edits, sourceId, captionsEnabled, quality, loudness, framing, jobId, patch } = useClips()
  const clips = useMemo(() => selectedClips({ source, selected, edits }), [source, selected, edits])
  const [job, setJob] = useState<ClipsJob | null>(null)
  const [live, setLive] = useState<Live | null>(null)
  const [running, setRunning] = useState<string | null>(null)
  const stop = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (jobId && !job) {
      clipsApi
        .job(jobId)
        .then(setJob)
        .catch(() => patch({ jobId: null }))
    }
    return () => stop.current?.()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const run = async () => {
    if (!sourceId || !clips.length) return
    setLive({ fraction: 0, message: 'Starting' })
    setJob(null)
    try {
      const { job_id } = await clipsApi.createClips({
        source_id: sourceId,
        clips,
        captions: { preset: draft.captions.preset, overrides: draft.captions.overrides, font: draft.captions.font },
        captions_enabled: captionsEnabled,
        format: draft.format,
        framing,
        export: { quality, loudness },
      })
      setRunning(job_id)
      patch({ jobId: job_id })
      stop.current = watchJob(
        job_id,
        (e) => setLive({ fraction: e.fraction, message: e.message }),
        async () => {
          const status = await clipsApi.job(job_id)
          setRunning(null)
          setLive(null)
          setJob(status)
          if (status.status === 'done') {
            const made = status.result?.clips.filter((c) => c.video).length ?? 0
            toast('success', `${made} clip${made === 1 ? '' : 's'} ready.`)
          } else if (status.status === 'cancelled') {
            toast('info', 'Stopped.')
          } else {
            toast('error', status.error ?? 'The clips could not be made.')
          }
        },
        () => {
          setRunning(null)
          setLive(null)
          toast('error', 'Lost connection to Snipmint. Check that it is still running, then try again.')
        },
      )
    } catch (e) {
      setRunning(null)
      setLive(null)
      toast('error', e instanceof Error ? e.message : 'Could not start.')
    }
  }

  const cancel = async () => {
    if (running) await api.cancelJob(running).catch(() => {})
  }

  const busy = running !== null
  const outputs = job?.status === 'done' ? (job.result?.clips ?? []) : []
  const made = outputs.filter((o) => o.video)
  const styleName = styles.find((s) => s.id === draft.captions.preset)?.name ?? draft.captions.preset
  const fmt = formats.find((f) => f.id === draft.format)
  const total = clips.reduce((acc, c) => acc + (c.end - c.start), 0)
  const checklist = [
    { label: 'Video', value: source?.meta?.title ?? '…', to: '/source' },
    { label: 'Clips', value: clips.length ? `${clips.length} (${clock(total)} in total)` : 'None picked', to: '/moments', missing: !clips.length },
    { label: 'Captions', value: captionsEnabled ? styleName : 'Off (SRT files only)', to: '/captions' },
    { label: 'Shape', value: fmt?.name ?? draft.format, to: '/captions' },
  ]

  const bar = createPortal(
    <div className="step-nav">
      <div className="step-nav__inner">
        <Button variant="text" onClick={() => navigate('/captions')}>
          Back
        </Button>
        <div className="step-nav__next">
          {busy ? (
            <Button variant="secondary" className="btn--lg" onClick={cancel}>
              Stop
            </Button>
          ) : (
            <Button
              variant={made.length ? 'secondary' : 'primary'}
              className="btn--lg"
              disabled={!clips.length || (health ? !health.ffmpeg : false)}
              onClick={() => {
                window.scrollTo({ top: 0 })
                void run()
              }}
            >
              {made.length ? 'Make again' : `Make ${clips.length} clip${clips.length === 1 ? '' : 's'}`}
            </Button>
          )}
          {made.length > 1 && !busy && job && (
            <a className="btn btn--primary btn--lg" href={clipFileUrl(job.id, 'clips.zip', true)} download>
              Download all
            </a>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )

  return (
    <div className="step">
      {bar}
      <header className="step-head">
        <h1 className="display">{busy ? 'Making your clips' : made.length ? 'Your clips are ready' : 'Ready to make your clips?'}</h1>
        <p className="lede">
          {busy
            ? 'Each clip is cut, cropped to the format and captioned. Keep this tab open.'
            : made.length
              ? 'Watch them here and download them. Change anything and make them again whenever you like.'
              : 'Check your choices below. Click any of them to change it.'}
        </p>
      </header>

      {busy && live && (
        <section className="make" aria-label="Making your clips" aria-live="polite">
          <h2 className="make__title">Working…</h2>
          <ProgressBar fraction={live.fraction} label="Progress" />
          <p className="make__line">{live.message}</p>
        </section>
      )}

      {health && (!health.ffmpeg || !health.libass) && (
        <Banner tone="error" title={health.ffmpeg ? 'Your ffmpeg can’t draw captions' : 'ffmpeg is missing'}>
          Snipmint needs ffmpeg with libass to make clips. The Docker image includes it.
        </Banner>
      )}

      {job && outputs.length > 0 && (
        <section className="section" aria-label="Your clips">
          <ol className="result-grid">
            {outputs.map((o) => (
              <li key={o.id} className="result-card">
                {o.video ? (
                  <video className="result-card__video" controls preload="metadata" src={clipFileUrl(job.id, o.video)} />
                ) : (
                  <div className="result-card__error">{o.error ?? 'Failed'}</div>
                )}
                <div className="result-card__body">
                  <strong className="result-card__title">{o.title || `Clip ${o.index}`}</strong>
                  <span className="field__hint">
                    {clock(o.start)}–{clock(o.end)} · {Math.round(o.end - o.start)} s
                  </span>
                  {o.layout && (
                    <span className="field__hint" title={o.framing_note ?? undefined}>
                      {LAYOUT_LABEL[o.layout] ?? o.layout}
                      {o.framing_note?.includes('Black bars') ? ' · black bars removed' : ''}
                    </span>
                  )}
                  {o.video && (
                    <div className="row">
                      <a className="btn btn--primary" href={clipFileUrl(job.id, o.video, true)} download>
                        Download
                      </a>
                      {o.srt && (
                        <a className="btn btn--secondary" href={clipFileUrl(job.id, o.srt, true)} download>
                          Subtitles
                        </a>
                      )}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
          <p className="field__hint">
            Titles, hooks and descriptions for every clip are in{' '}
            <a href={clipFileUrl(job.id, 'titles.json', true)} download>
              titles.json
            </a>
            .
          </p>
        </section>
      )}

      <section className="section" aria-labelledby="framing-title">
        <div className="section__head">
          <h2 className="section__title" id="framing-title">
            Framing
          </h2>
          <p className="section__hint">How the wide video becomes vertical. Black bars are always removed.</p>
        </div>
        <Segmented
          label="Framing"
          value={framing}
          onChange={(f) => patch({ framing: f })}
          items={FRAMINGS.map((f) => ({ id: f.id, label: f.label, hint: f.hint }))}
        />
        <p className="field__hint">{FRAMINGS.find((f) => f.id === framing)?.hint}</p>
      </section>

      <section className="section" aria-labelledby="quality-title">
        <h2 className="section__title" id="quality-title">
          Quality
        </h2>
        <Segmented
          label="Encoding quality"
          value={quality}
          onChange={(q) => patch({ quality: q })}
          items={[
            { id: 'fast', label: 'Fast' },
            { id: 'balanced', label: 'Balanced' },
            { id: 'high', label: 'Best' },
          ]}
        />
        <Switch
          label="Level the volume"
          hint="Brings every clip to -14 LUFS, the loudness TikTok, Reels and Shorts play at."
          checked={loudness}
          onChange={(v) => patch({ loudness: v })}
        />
      </section>

      <section className="section" aria-labelledby="choices-title">
        <h2 className="section__title" id="choices-title">
          Your choices
        </h2>
        <ul className="checklist">
          {checklist.map((c) => (
            <li key={c.label}>
              <Link to={c.to} className={`checklist__row ${c.missing ? 'checklist__row--missing' : ''}`}>
                <span className="checklist__label">{c.label}</span>
                <span className="checklist__value">{c.value}</span>
                <span className="checklist__edit">Change</span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
