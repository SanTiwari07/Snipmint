import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { watchJob } from '../api/client'
import {
  LENGTH_BANDS,
  STAGE_LABELS,
  STAGE_ORDER,
  type StageName,
  clipsApi,
  clock,
  getAiMode,
  providerFor,
} from '../api/clips'
import { Banner, Button, ProgressBar } from '../components/ui'
import { useClips } from '../state/clips'
import { useSession } from '../state/session'

interface Live {
  stage: string
  fraction: number
  message: string
}

/** Download, transcribe and analyse, with live progress. */
export function SourceStep() {
  const navigate = useNavigate()
  const toast = useSession((s) => s.toast)
  const { source, taskId, url, band, count, refresh, startSource, sourceError } = useClips()
  const [live, setLive] = useState<Live | null>(null)
  const [retrying, setRetrying] = useState(false)
  const stop = useRef<(() => void) | null>(null)

  // follow the task while it runs; when it ends, load the result
  useEffect(() => {
    let cancelled = false
    void refresh().then((s) => {
      if (cancelled || !s) return
      const running = s.task && (s.task.status === 'running' || s.task.status === 'queued')
      if (!running || !taskId) return
      stop.current = watchJob(
        taskId,
        (e) => setLive(e),
        async (end) => {
          setLive(null)
          const fresh = await refresh()
          if (end.status === 'done' && fresh?.analysis?.moments.length) {
            toast('success', `Found ${fresh.analysis.moments.length} moments.`)
            navigate('/moments')
          }
        },
        () => {
          setLive(null)
          void refresh()
        },
        'tasks',
      )
    })
    return () => {
      cancelled = true
      stop.current?.()
    }
  }, [taskId]) // eslint-disable-line react-hooks/exhaustive-deps

  const retry = async () => {
    setRetrying(true)
    try {
      const b = LENGTH_BANDS[band]
      const created = await clipsApi.createSource(url, {
        provider: providerFor(getAiMode()),
        min_seconds: b.min,
        max_seconds: b.max,
        count,
      })
      startSource(url, created.source_id, created.task_id)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not start again.')
    } finally {
      setRetrying(false)
    }
  }

  const meta = source?.meta
  const stages = source?.status.stages
  const failed = source?.status.error
  const running = !!live || source?.task?.status === 'running' || source?.task?.status === 'queued'
  const liveStage = (live?.stage ?? '') as StageName
  const current = STAGE_ORDER.find((s) => stages?.[s] === 'running') ?? (STAGE_ORDER.includes(liveStage) ? liveStage : null)
  const ready = !!source?.analysis?.moments.length && stages?.analyze === 'done'
  const interrupted = !running && !ready && !failed && !!source

  return (
    <div className="step">
      <header className="step-head">
        <h1 className="display">{ready ? 'Your video is ready' : failed ? 'Something went wrong' : 'Getting your video ready'}</h1>
        <p className="lede">
          {ready
            ? 'Every word is transcribed and the best moments are picked. Next, choose the ones you want.'
            : 'Snipmint downloads the video, writes down every word with its timing and finds the best moments. A 30-minute video takes a few minutes.'}
        </p>
      </header>

      {sourceError && <Banner tone="error">{sourceError}</Banner>}

      {meta ? (
        <section className="source-meta card" aria-label="The video">
          {meta.thumbnail && <img className="source-meta__thumb" src={meta.thumbnail} alt="" />}
          <div className="source-meta__text">
            <strong className="source-meta__title">{meta.title || 'Untitled video'}</strong>
            <span className="field__hint">
              {[meta.channel, clock(meta.duration), meta.height ? `${meta.height}p` : null].filter(Boolean).join(' · ')}
            </span>
            <span className="source-meta__chips">
              {meta.chapters.length > 0 && <span className="chip">{meta.chapters.length} chapters</span>}
              {meta.heatmap.length > 0 && <span className="chip">“Most replayed” data</span>}
              {source?.vertical && <span className="chip">Already vertical</span>}
            </span>
          </div>
        </section>
      ) : (
        <p className="muted">{url}</p>
      )}

      {meta && meta.height !== null && meta.height < 720 && (
        <Banner tone="warn">This video is only {meta.height}p, so the vertical clips will look soft.</Banner>
      )}

      {(running || current) && !failed && (
        <section className="make" aria-label="Progress" aria-live="polite">
          <h2 className="make__title">{current ? STAGE_LABELS[current] : 'Waiting to start'}…</h2>
          <ProgressBar fraction={live?.fraction ?? source?.task?.fraction ?? 0} label="Overall progress" />
          <ol className="stages" aria-label="Steps">
            {STAGE_ORDER.map((s) => (
              <li
                key={s}
                className={stages?.[s] === 'done' ? 'stage stage--done' : s === current ? 'stage stage--now' : 'stage'}
              >
                {STAGE_LABELS[s]}
              </li>
            ))}
          </ol>
          {live?.message && <p className="make__line">{live.message}</p>}
        </section>
      )}

      {failed && (
        <Banner
          tone="error"
          title="The video could not be prepared"
          action={
            <Button variant="secondary" busy={retrying} onClick={retry}>
              Try again
            </Button>
          }
        >
          {failed}
        </Banner>
      )}

      {interrupted && (
        <Banner
          tone="warn"
          title="Preparation stopped"
          action={
            <Button variant="secondary" busy={retrying} onClick={retry}>
              Continue
            </Button>
          }
        >
          Snipmint was restarted before this video was ready. Finished steps are kept, so continuing is quick.
        </Banner>
      )}

      {ready && source?.status.warnings.map((w) => (
        <Banner key={w} tone="warn" title="Used the free analysis">
          {w}
        </Banner>
      ))}

      {ready && (
        <div className="row">
          <Button variant="primary" className="btn--lg" onClick={() => navigate('/moments')}>
            See the {source?.analysis?.moments.length} moments
          </Button>
        </div>
      )}
    </div>
  )
}
