import { type FormEvent, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { type AiMode, LENGTH_BANDS, clipsApi, getAiMode, providerFor } from '../api/clips'
import type { Health } from '../api/types'
import { AiSettings } from '../components/AiSettings'
import { Logo } from '../components/Shell'
import { Button, ProgressBar } from '../components/ui'
import { parseProjectFile } from '../lib/projectFile'
import { useClips } from '../state/clips'
import { useDraft } from '../state/project'
import { useSession } from '../state/session'

/** The first question: which video? */
export function StartScreen() {
  const navigate = useNavigate()
  const { start, toast } = useSession()
  const { url: lastUrl, sourceId, band, count, startSource, reset } = useClips()
  const lastTitle = useClips((s) => s.source?.meta?.title)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<AiMode>(getAiMode())
  const [health, setHealth] = useState<Health | null>(null)
  const [uploading, setUploading] = useState<{ name: string; fraction: number } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const projectRef = useRef<HTMLInputElement>(null)
  const [opening, setOpening] = useState(false)

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null))
  }, [])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const link = url.trim()
    if (!link) {
      setError('Paste a YouTube link first.')
      return
    }
    if (needsKey()) {
      setError('Advanced mode needs a Gemini key in the .env file (see below), or switch to Free.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const b = LENGTH_BANDS[band]
      const created = await clipsApi.createSource(link, {
        provider: providerFor(mode),
        min_seconds: b.min,
        max_seconds: b.max,
        count,
      })
      startSource(link, created.source_id, created.task_id)
      start()
      navigate('/source')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That link could not be used.')
    } finally {
      setBusy(false)
    }
  }

  const needsKey = () =>
    mode === 'advanced' && !((health as (Health & { ai?: Record<string, boolean> }) | null)?.ai?.gemini_server_key ?? false)

  const uploadFile = async (file: File) => {
    if (needsKey()) {
      setError('Advanced mode needs a Gemini key in the .env file (see below), or switch to Free.')
      return
    }
    setError(null)
    setUploading({ name: file.name, fraction: 0 })
    try {
      const b = LENGTH_BANDS[band]
      const created = await clipsApi.upload(
        file,
        { provider: providerFor(mode), min_seconds: b.min, max_seconds: b.max, count },
        (fraction) => setUploading({ name: file.name, fraction }),
      )
      startSource(`file:${file.name}`, created.source_id, created.task_id)
      start()
      navigate('/source')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That file could not be used.')
    } finally {
      setUploading(null)
    }
  }

  const openProject = async (file: File) => {
    setOpening(true)
    setError(null)
    try {
      const p = parseProjectFile(await file.text())
      useDraft.setState({ captions: p.captions, format: p.format })
      const b = LENGTH_BANDS[p.analysis.band]
      const opts = { provider: providerFor(mode), min_seconds: b.min, max_seconds: b.max, count: p.analysis.count }
      let existing = null
      try {
        existing = await clipsApi.source(p.source.id)
      } catch {
        existing = null
      }
      const ready = !!existing?.analysis?.moments.length && existing.status.stages.analyze === 'done'
      if (ready && existing) {
        startSource(p.source.url, p.source.id, existing.status.task_id ?? '')
      } else if (existing?.has_video || (p.source.url && !p.source.url.startsWith('file:'))) {
        const created = existing?.has_video
          ? await clipsApi.prepare(p.source.id, opts)
          : await clipsApi.createSource(p.source.url, opts)
        startSource(p.source.url, created.source_id, created.task_id)
      } else {
        throw new Error('The video of this project is no longer on this computer. Upload it again, then pick the moments.')
      }
      useClips.setState({
        band: p.analysis.band,
        count: p.analysis.count,
        captionsEnabled: p.captions_enabled,
        framing: p.framing,
        quality: p.export.quality,
        loudness: p.export.loudness,
        voiceover: p.voiceover,
        pending: { selected: p.selected, edits: p.edits },
      })
      start()
      if (ready && existing) {
        useClips.getState().setSource(existing)
        toast('success', `Opened ${file.name}. Check your choices, then make the clips.`)
        navigate('/export')
      } else {
        toast('info', 'Opened the project. The video is being prepared again; your picks come back when it is ready.')
        navigate('/source')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That project could not be opened.')
    } finally {
      setOpening(false)
    }
  }

  const carryOn = () => {
    start()
    navigate('/moments')
  }

  return (
    <div className="start">
      <header className="nav">
        <div className="nav__inner">
          <Logo />
        </div>
      </header>
      <main id="main" className="start__main">
        <div className="start__head">
          <h1 className="display">Turn a YouTube video into shorts</h1>
          <p className="lede">
            Paste a link. Snipmint finds the best moments, crops them to vertical and adds captions people watch to
            the end.
          </p>
        </div>

        <form className="link-form card" onSubmit={submit} noValidate>
          <label className="field__label" htmlFor="yt-url">
            YouTube link
          </label>
          <div className="link-form__row">
            <input
              id="yt-url"
              className="input link-form__input"
              type="url"
              inputMode="url"
              autoComplete="off"
              placeholder="https://www.youtube.com/watch?v=…"
              value={url}
              aria-invalid={!!error}
              aria-describedby={error ? 'yt-url-error' : 'yt-url-hint'}
              onChange={(e) => {
                setUrl(e.target.value)
                if (error) setError(null)
              }}
            />
            <Button variant="primary" className="btn--lg" type="submit" busy={busy}>
              Find clips
            </Button>
          </div>
          {error ? (
            <p className="field__error" id="yt-url-error" role="alert">
              {error}
            </p>
          ) : (
            <p className="field__hint" id="yt-url-hint">
              One video at a time: podcasts, interviews, talks and streams work best. Only clip videos you have the
              rights to use.
            </p>
          )}
          <div className="upload-row">
            <span className="field__hint">No link? Use a video file from this computer.</span>
            <Button variant="secondary" disabled={!!uploading || busy} onClick={() => fileRef.current?.click()}>
              Upload a video
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mkv,.m4v,.avi"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) void uploadFile(f)
                e.target.value = ''
              }}
            />
          </div>
          {uploading && (
            <div className="stack" aria-live="polite">
              <ProgressBar fraction={uploading.fraction} label="Upload progress" />
              <span className="field__hint">
                Uploading {uploading.name}… {Math.round(uploading.fraction * 100)}%
              </span>
            </div>
          )}
          <AiSettings mode={mode} onMode={setMode} health={health} onToast={toast} />
        </form>

        <div className="start__choices">
          <button type="button" className="start-card" disabled={opening} onClick={() => projectRef.current?.click()}>
            <span className="start-card__title">{opening ? 'Opening…' : 'Open a saved project'}</span>
            <span className="start-card__text">A .snipmint.json file you saved from the Make clips step.</span>
            <span className="start-card__go" aria-hidden="true">
              →
            </span>
          </button>
          <input
            ref={projectRef}
            type="file"
            accept=".json,application/json"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void openProject(f)
              e.target.value = ''
            }}
          />
        </div>

        {sourceId && (
          <div className="start__choices">
            <button type="button" className="start-card" onClick={carryOn}>
              <span className="start-card__title">Continue with your last video</span>
              <span className="start-card__text">{lastTitle ?? (lastUrl.startsWith('file:') ? lastUrl.slice(5) : lastUrl)}</span>
              <span className="start-card__go" aria-hidden="true">
                →
              </span>
            </button>
            <button type="button" className="link-btn" onClick={reset}>
              Forget it
            </button>
          </div>
        )}
      </main>
    </div>
  )
}
