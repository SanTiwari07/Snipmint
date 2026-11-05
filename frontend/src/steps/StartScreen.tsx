import { type FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { type AiMode, LENGTH_BANDS, clipsApi, getAiMode, getGeminiKey, providerFor } from '../api/clips'
import type { Health } from '../api/types'
import { AiSettings } from '../components/AiSettings'
import { Logo } from '../components/Shell'
import { Button } from '../components/ui'
import { useClips } from '../state/clips'
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
    if (mode === 'advanced' && !getGeminiKey() && !(health?.ai?.gemini_server_key ?? false)) {
      setError('Advanced mode needs a Gemini API key. Add one below, or switch to Free.')
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
          <AiSettings mode={mode} onMode={setMode} health={health} onToast={toast} />
        </form>

        {sourceId && (
          <div className="start__choices">
            <button type="button" className="start-card" onClick={carryOn}>
              <span className="start-card__title">Continue with your last video</span>
              <span className="start-card__text">{lastTitle ?? lastUrl}</span>
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
