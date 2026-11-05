import { useState } from 'react'
import { type AiMode, clipsApi, getGeminiKey, getGeminiModel, setAiMode, setGeminiKey, setGeminiModel } from '../api/clips'
import type { Health } from '../api/types'
import type { Toast } from '../state/session'
import { Button, Segmented } from './ui'

/** Free (default, on this computer) or Advanced (your Gemini key). */
export function AiSettings({
  mode,
  onMode,
  health,
  onToast,
}: {
  mode: AiMode
  onMode: (m: AiMode) => void
  health: Health | null
  onToast: (kind: Toast['kind'], message: string) => void
}) {
  const [key, setKey] = useState(getGeminiKey())
  const [model, setModel] = useState(getGeminiModel())
  const [checking, setChecking] = useState(false)
  const [keyState, setKeyState] = useState<'unknown' | 'ok' | 'bad'>('unknown')
  const [keyError, setKeyError] = useState<string | null>(null)
  const ai = (health as (Health & { ai?: Record<string, boolean> }) | null)?.ai
  const serverKey = !!ai?.gemini_server_key
  const installed = ai ? !!ai.gemini_installed : true

  const choose = (m: AiMode) => {
    setAiMode(m)
    onMode(m)
  }

  const check = async () => {
    const k = key.trim()
    if (!k) {
      setKeyError('Paste your key first.')
      return
    }
    setChecking(true)
    setKeyError(null)
    try {
      const r = await clipsApi.checkKey(k)
      setKeyState(r.ok ? 'ok' : 'bad')
      if (r.ok) {
        setGeminiKey(k)
        onToast('success', 'Gemini key works. It is saved in this browser only.')
      } else {
        setKeyError(r.error ?? 'Google did not accept this key.')
      }
    } catch (e) {
      setKeyState('bad')
      setKeyError(e instanceof Error ? e.message : 'Could not check the key.')
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="ai-settings">
      <div className="ai-settings__head">
        <span className="field__label">How should moments be picked?</span>
        <Segmented
          label="Analysis mode"
          value={mode}
          onChange={choose}
          items={[
            { id: 'free', label: 'Free' },
            { id: 'advanced', label: 'Advanced (Gemini)' },
          ]}
        />
      </div>
      {mode === 'free' ? (
        <p className="field__hint">
          Runs on your computer with no key: it uses YouTube’s “most replayed” data, strong opening lines, energy and
          keywords. Nothing is sent anywhere except the YouTube download.
        </p>
      ) : (
        <div className="stack">
          <p className="field__hint">
            Gemini reads the transcript like an editor and writes titles and hooks. Only the transcript text is sent to
            Google; it costs a few cents per hour of video on your key. If Gemini fails, the free analysis is used.
          </p>
          {!installed && (
            <p className="field__error">Gemini support isn’t installed on this server (pip install "snipmint[gemini]").</p>
          )}
          {serverKey && !key && <p className="field__hint">This server already has a Gemini key, so you can leave this empty.</p>}
          <div className="link-form__row">
            <input
              className="input"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Gemini API key (AIza…)"
              aria-label="Gemini API key"
              value={key}
              aria-invalid={keyState === 'bad'}
              onChange={(e) => {
                setKey(e.target.value)
                setKeyState('unknown')
                setKeyError(null)
                setGeminiKey(e.target.value.trim())
              }}
            />
            <Button variant="secondary" busy={checking} onClick={check}>
              {keyState === 'ok' ? 'Key works ✓' : 'Check key'}
            </Button>
          </div>
          {keyError && (
            <p className="field__error" role="alert">
              {keyError}
            </p>
          )}
          <label className="field">
            <span className="field__label">Model (optional)</span>
            <input
              className="input"
              type="text"
              spellCheck={false}
              placeholder="gemini-3.1-flash-lite (default)"
              value={model}
              onChange={(e) => {
                setModel(e.target.value)
                setGeminiModel(e.target.value.trim())
              }}
            />
          </label>
          <p className="field__hint">
            Get a key at{' '}
            <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
              aistudio.google.com/apikey
            </a>
            . It stays in this browser and is sent only to your Snipmint server.
          </p>
        </div>
      )}
    </div>
  )
}
