import { useEffect, useState } from 'react'
import { type AiMode, clipsApi, forgetBrowserKey, getGeminiModel, setAiMode, setGeminiModel } from '../api/clips'
import type { Health } from '../api/types'
import type { Toast } from '../state/session'
import { Button, Segmented } from './ui'

const ENV_LINE = 'GEMINI_API_KEY=your-key-here'

/** Free (default, on this computer) or Advanced (Gemini, with the key from the server's .env). */
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
  const [model, setModel] = useState(getGeminiModel())
  const [checking, setChecking] = useState(false)
  const [keyState, setKeyState] = useState<'unknown' | 'ok' | 'bad'>('unknown')
  const [keyError, setKeyError] = useState<string | null>(null)
  const ai = (health as (Health & { ai?: Record<string, boolean> }) | null)?.ai
  const serverKey = !!ai?.gemini_server_key
  const installed = ai ? !!ai.gemini_installed : true

  useEffect(() => forgetBrowserKey(), [])

  const choose = (m: AiMode) => {
    setAiMode(m)
    onMode(m)
  }

  const check = async () => {
    setChecking(true)
    setKeyError(null)
    try {
      const r = await clipsApi.checkKey()
      setKeyState(r.ok ? 'ok' : 'bad')
      if (r.ok) onToast('success', 'The Gemini key in .env works.')
      else setKeyError(r.error ?? 'Google did not accept the key in .env.')
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
          {serverKey ? (
            <div className="link-form__row ai-settings__key">
              <span className="field__hint">Using the Gemini key from the server’s .env file.</span>
              <Button variant="secondary" busy={checking} onClick={check}>
                {keyState === 'ok' ? 'Key works ✓' : 'Check key'}
              </Button>
            </div>
          ) : (
            <div className="ai-settings__howto" role="note">
              <strong>Add your Gemini key to the .env file</strong>
              <ol>
                <li>
                  Get a key at{' '}
                  <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
                    aistudio.google.com/apikey
                  </a>
                  .
                </li>
                <li>
                  In the Snipmint folder (next to <code>docker-compose.yml</code>), open <code>.env</code> (copy{' '}
                  <code>.env.example</code> if it isn’t there) and set:
                  <pre className="ai-settings__code">{ENV_LINE}</pre>
                </li>
                <li>
                  Restart Snipmint: <code>docker compose up -d</code>, then reload this page.
                </li>
              </ol>
              <span className="field__hint">
                The key stays on your computer in .env. It is never typed into this page or stored in the browser.
              </span>
            </div>
          )}
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
        </div>
      )}
    </div>
  )
}
