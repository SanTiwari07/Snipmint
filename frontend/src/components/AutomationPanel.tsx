import { useCallback, useEffect, useState } from 'react'
import {
  type AutomationStatus,
  type AutomationTask,
  type ClipRef,
  type Execution,
  OPEN_TASK,
  TASK_LABEL,
  automationApi,
} from '../api/automation'
import type { Privacy } from '../api/youtube'
import { localToIso } from '../lib/metadata'
import { useSession } from '../state/session'
import { whenText } from './PublishDashboard'
import { Badge, Banner, Button, Segmented, Switch } from './ui'

type Mode = 'now' | 'later' | 'batch'

const TONE: Record<string, 'plain' | 'yellow' | 'good' | 'bad' | 'warn'> = {
  AWAITING_APPROVAL: 'warn',
  READY: 'yellow',
  SCHEDULED: 'plain',
  PROCESSING: 'yellow',
  UPLOADING: 'yellow',
  PUBLISHED: 'good',
  FAILED: 'bad',
  CANCELLED: 'warn',
  SKIPPED: 'warn',
}
const EXEC_TONE: Record<Execution['status'], 'plain' | 'good' | 'bad' | 'yellow' | 'warn'> = {
  running: 'yellow',
  success: 'good',
  error: 'bad',
  waiting: 'warn',
  cancelled: 'warn',
}

/** n8n automation on the Publish screen. Works (and stays out of the way) when n8n is off or down. */
export function AutomationPanel({
  clips,
  approvedIds,
  channelId,
  privacy,
  madeForKids,
}: {
  clips: ClipRef[]
  approvedIds: string[]
  channelId: string | null
  privacy: Privacy
  madeForKids: boolean
}) {
  const toast = useSession((s) => s.toast)
  const [status, setStatus] = useState<AutomationStatus | null>(null)
  const [tasks, setTasks] = useState<AutomationTask[]>([])
  const [execs, setExecs] = useState<Execution[]>([])
  const [mode, setMode] = useState<Mode>('now')
  const [when, setWhen] = useState('')
  const [delay, setDelay] = useState(30)
  const [concurrency, setConcurrency] = useState(1)
  const [busy, setBusy] = useState<string | null>(null)
  const [moving, setMoving] = useState<{ id: string; when: string } | null>(null)

  const refresh = useCallback(async () => {
    try {
      const st = await automationApi.status()
      setStatus(st)
      if (st.configured) {
        const [t, e] = await Promise.all([automationApi.tasks(), automationApi.executions()])
        setTasks(t)
        setExecs(e)
      }
    } catch {
      setStatus(null)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const t = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(t)
  }, [refresh])

  useEffect(() => {
    if (status) {
      setDelay(status.settings.batch_delay_seconds)
      setConcurrency(status.settings.batch_concurrency)
    }
  }, [status?.settings.batch_delay_seconds, status?.settings.batch_concurrency]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (key: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(key)
    try {
      await fn()
      if (done) toast('success', done)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'That did not work.')
    } finally {
      setBusy(null)
      void refresh()
    }
  }

  if (!status) return null
  if (!status.configured) {
    return (
      <section className="section" aria-labelledby="auto-title">
        <h2 className="section__title" id="auto-title">
          Automation with n8n
        </h2>
        {status.config_error ? (
          <Banner tone="error" title="Automation is misconfigured">
            {status.config_error}
          </Banner>
        ) : (
          <p className="field__hint">
            Optional: let n8n publish approved clips for you, on a schedule or in batches, and notify you. Set
            SNIPMINT_N8N_WEBHOOK_SECRET and start n8n (see docs/n8n.md). Publishing by hand above works without it.
          </p>
        )}
      </section>
    )
  }

  const s = status.settings
  const scheduleIso = mode === 'later' ? localToIso(when) : null
  const approvedCount = clips.filter((c) => approvedIds.includes(c.clip_id)).length
  const blocked = !s.enabled
    ? 'Turn automation on first.'
    : !clips.length
      ? 'Pick clips above.'
      : mode === 'later' && !scheduleIso
        ? 'Pick a date and time.'
        : null

  const handOver = () =>
    act(
      'create',
      () =>
        automationApi.create({
          clips,
          privacy,
          made_for_kids: madeForKids,
          channel_id: channelId,
          run_at: scheduleIso,
          batch: mode === 'batch',
          delay_seconds: mode === 'batch' ? delay : undefined,
          concurrency: mode === 'batch' ? concurrency : undefined,
        }),
      mode === 'later' ? 'Scheduled. n8n will start the upload at that time.' : 'Handed to automation.',
    )

  return (
    <section className="section" aria-labelledby="auto-title">
      <div className="section__head">
        <h2 className="section__title" id="auto-title">
          Automation with n8n
        </h2>
        <p className="section__hint">
          n8n decides when; Snipmint still writes the metadata, checks your approval and does every upload.
        </p>
      </div>

      <div className="auto-status" role="status">
        <Badge tone={status.reachable ? 'good' : 'bad'}>{status.reachable ? 'n8n connected' : 'n8n offline'}</Badge>
        <Badge tone={s.enabled ? 'yellow' : 'plain'}>{s.enabled ? 'Automation on' : 'Automation off'}</Badge>
        {status.events.pending > 0 && <span className="field__hint">{status.events.pending} event(s) waiting to reach n8n</span>}
        {!status.reachable && status.unreachable_reason && <span className="field__hint">{status.unreachable_reason}</span>}
      </div>
      {status.events.dead > 0 && (
        <Banner
          tone="warn"
          title={`${status.events.dead} message(s) never reached n8n`}
          action={
            <Button variant="secondary" busy={busy === 'events'} onClick={() => void act('events', automationApi.retryEvents, 'Sending again.')}>
              Send again
            </Button>
          }
        >
          Check that n8n is running and the workflows are active.
        </Banner>
      )}

      <Switch
        label="Automation on"
        hint="When off, n8n can’t start any upload. Scheduled clips wait."
        checked={s.enabled}
        onChange={(v) => void act('settings', () => automationApi.updateSettings({ enabled: v }))}
      />
      <Switch
        label="Approve metadata automatically"
        hint="Off by default. When on, automation may publish titles and descriptions nobody reviewed."
        checked={s.auto_approve}
        onChange={(v) => void act('settings', () => automationApi.updateSettings({ auto_approve: v }))}
      />

      <div className="field">
        <span className="field__label">Hand the picked clips to automation</span>
        <Segmented
          label="When"
          value={mode}
          onChange={setMode}
          items={[
            { id: 'now', label: 'As soon as approved' },
            { id: 'later', label: 'At a time' },
            { id: 'batch', label: 'As a batch' },
          ]}
        />
        {mode === 'later' && (
          <input
            className="input meta-when"
            type="datetime-local"
            aria-label="Upload at (your time)"
            value={when}
            onChange={(e) => setWhen(e.target.value)}
          />
        )}
        {mode === 'batch' && (
          <div className="row">
            <label className="field__label">
              Seconds between uploads{' '}
              <input className="input auto-num" type="number" min={0} max={3600} value={delay} onChange={(e) => setDelay(Number(e.target.value))} />
            </label>
            <label className="field__label">
              At the same time{' '}
              <input className="input auto-num" type="number" min={1} max={4} value={concurrency} onChange={(e) => setConcurrency(Number(e.target.value))} />
            </label>
          </div>
        )}
        <p className="field__hint">
          {clips.length} clip(s) picked, {approvedCount} approved. Clips without approved metadata wait for your approval.
        </p>
        <div className="row">
          <Button variant="primary" disabled={!!blocked} busy={busy === 'create'} onClick={() => void handOver()}>
            Hand {clips.length} clip{clips.length === 1 ? '' : 's'} to automation
          </Button>
          {blocked && <span className="step-nav__error">{blocked}</span>}
        </div>
      </div>

      <div className="field">
        <span className="field__label">Automation activity</span>
        {!tasks.length ? (
          <p className="field__hint">Nothing yet.</p>
        ) : (
          <ol className="publish-list" aria-label="Automation tasks">
            {tasks.map((t) => {
              const open = OPEN_TASK.includes(t.status)
              return (
                <li key={t.id} className="publish-row" data-status={t.status}>
                  <div className="publish-row__main">
                    <strong className="publish-row__title">{t.title || t.clip.clip_file}</strong>
                    <span className="field__hint">
                      {t.kind === 'batch' ? 'Batch' : t.kind === 'scheduled' ? 'Scheduled' : 'Automatic'}
                      {t.run_at ? ` · ${whenText(t.run_at)}` : ''} · attempt {t.attempts}/{t.max_attempts}
                      {t.next_attempt_at ? ` · next try ${new Date(t.next_attempt_at * 1000).toLocaleTimeString()}` : ''}
                    </span>
                    {t.error && <span className="field__error">{t.error}</span>}
                    {t.video_url && (
                      <a className="publish-row__link" href={t.video_url} target="_blank" rel="noreferrer">
                        {t.video_url}
                      </a>
                    )}
                    {moving?.id === t.id && (
                      <div className="row">
                        <input
                          className="input meta-when"
                          type="datetime-local"
                          aria-label="New time"
                          value={moving.when}
                          onChange={(e) => setMoving({ id: t.id, when: e.target.value })}
                        />
                        <Button
                          variant="secondary"
                          disabled={!localToIso(moving.when)}
                          onClick={() =>
                            void act('move', () => automationApi.reschedule(t.id, localToIso(moving.when)!), 'Rescheduled.').then(() =>
                              setMoving(null),
                            )
                          }
                        >
                          Save
                        </Button>
                      </div>
                    )}
                  </div>
                  <div className="publish-row__side">
                    <Badge tone={TONE[t.status] ?? 'plain'}>{TASK_LABEL[t.status] ?? t.label}</Badge>
                    <div className="row">
                      {(t.status === 'SCHEDULED' || t.status === 'AWAITING_APPROVAL') && (
                        <Button variant="text" onClick={() => setMoving({ id: t.id, when: '' })}>
                          Reschedule
                        </Button>
                      )}
                      {open && (
                        <Button variant="text" onClick={() => void act(`c${t.id}`, () => automationApi.cancel(t.id), 'Cancelled.')}>
                          Cancel
                        </Button>
                      )}
                      {['FAILED', 'CANCELLED', 'SKIPPED'].includes(t.status) && (
                        <Button variant="secondary" busy={busy === `r${t.id}`} onClick={() => void act(`r${t.id}`, () => automationApi.retry(t.id), 'Trying again.')}>
                          Retry
                        </Button>
                      )}
                    </div>
                  </div>
                </li>
              )
            })}
          </ol>
        )}
      </div>

      {execs.length > 0 && (
        <div className="field">
          <span className="field__label">Recent n8n runs</span>
          <ul className="auto-execs" aria-label="n8n executions">
            {execs.map((e) => (
              <li key={e.id} className="auto-exec">
                <Badge tone={EXEC_TONE[e.status]}>{e.status}</Badge>
                <span className="auto-exec__name">{e.workflow || 'workflow'}</span>
                <span className="field__hint">{new Date(e.updated * 1000).toLocaleString()}</span>
                {e.message && <span className={e.status === 'error' ? 'field__error' : 'field__hint'}>{e.message}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
