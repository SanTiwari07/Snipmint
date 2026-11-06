import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { watchJob } from '../api/client'
import { type ClipOutput, type ClipsJob, clipFileUrl, clipsApi, clock } from '../api/clips'
import {
  ACTIVE_STATUSES,
  type MetadataOptions,
  type MetadataRecord,
  type Privacy,
  type PublishItem,
  type YouTubeStatus,
  metadataApi,
  youtubeApi,
} from '../api/youtube'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { MetadataEditor } from '../components/MetadataEditor'
import { PublishDashboard, whenText } from '../components/PublishDashboard'
import { Banner, Button, Segmented, Switch } from '../components/ui'
import { duplicateTitles, localToIso, toClipToPublish } from '../lib/metadata'
import { useClips } from '../state/clips'
import { useSession } from '../state/session'

const LANG_KEY = 'snipmint.metadataLanguage'

function storedLanguage(): string {
  try {
    return window.localStorage.getItem(LANG_KEY) || 'en'
  } catch {
    return 'en'
  }
}

/** Connect a channel, polish each clip's title and description, then publish (now or later). */
export function PublishStep() {
  const navigate = useNavigate()
  const location = useLocation()
  const toast = useSession((s) => s.toast)
  const { sourceId, jobId } = useClips()
  const [status, setStatus] = useState<YouTubeStatus | null>(null)
  const [options, setOptions] = useState<MetadataOptions | null>(null)
  const [job, setJob] = useState<ClipsJob | null>(null)
  const [records, setRecords] = useState<Record<string, MetadataRecord>>({})
  const [items, setItems] = useState<PublishItem[]>([])
  const [language, setLanguage] = useState(storedLanguage)
  const [picked, setPicked] = useState<string[]>([])
  const [privacy, setPrivacy] = useState<Privacy>('private')
  const [kids, setKids] = useState<'no' | 'yes'>('no')
  const [scheduleOn, setScheduleOn] = useState(false)
  const [scheduleAt, setScheduleAt] = useState('')
  const [allowDuplicate, setAllowDuplicate] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const stop = useRef<(() => void) | null>(null)

  const refreshItems = useCallback(async () => {
    try {
      setItems(await youtubeApi.items())
    } catch {
      /* the dashboard just stays as it was */
    }
  }, [])

  // ------------------------------------------------------------ load
  useEffect(() => {
    youtubeApi.status().then(setStatus).catch(() => setStatus(null))
    metadataApi.options().then(setOptions).catch(() => {})
    void refreshItems()
    if (jobId) clipsApi.job(jobId).then(setJob).catch(() => setJob(null))
    if (sourceId) metadataApi.get(sourceId).then((m) => setRecords(m.clips)).catch(() => {})
    return () => stop.current?.()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // back from Google's consent screen
  useEffect(() => {
    const q = new URLSearchParams(location.search)
    const result = q.get('youtube')
    if (!result) return
    if (result === 'connected') toast('success', `Connected ${q.get('channel') || 'your channel'}.`)
    else toast('error', q.get('message') || 'The sign-in did not work.')
    navigate('/publish', { replace: true })
  }, [location.search]) // eslint-disable-line react-hooks/exhaustive-deps

  // keep the dashboard fresh while anything is moving (also uploads resumed after a restart)
  const anyActive = items.some((i) => ACTIVE_STATUSES.includes(i.status))
  useEffect(() => {
    if (!anyActive) return
    const t = window.setInterval(() => void refreshItems(), 3000)
    return () => window.clearInterval(t)
  }, [anyActive, refreshItems])

  const clips: ClipOutput[] = useMemo(
    () => (job?.status === 'done' ? (job.result?.clips ?? []).filter((c) => c.video) : []),
    [job],
  )
  useEffect(() => {
    setPicked(clips.map((c) => c.id))
  }, [clips])

  const channel = status?.channels.find((c) => c.id === status.default_channel) ?? status?.channels[0] ?? null
  // the sign-in cookie only comes back if the page and the redirect URI share a host name
  const redirectHost = (() => {
    try {
      return status?.redirect_uri ? new URL(status.redirect_uri).hostname : null
    } catch {
      return null
    }
  })()
  const hostMismatch = !!redirectHost && redirectHost !== window.location.hostname
  const missing = clips.filter((c) => !records[c.id])
  const dupes = duplicateTitles(clips.map((c) => records[c.id]).filter(Boolean))
  const chosen = clips.filter((c) => picked.includes(c.id) && records[c.id])
  const scheduleIso = scheduleOn ? localToIso(scheduleAt) : null

  const connect = async () => {
    setBusy('connect')
    try {
      const { auth_url } = await youtubeApi.startOAuth('/publish')
      window.location.assign(auth_url)
    } catch (e) {
      setBusy(null)
      toast('error', e instanceof Error ? e.message : 'Could not start the sign-in.')
    }
  }

  const disconnect = async () => {
    setBusy('disconnect')
    try {
      const r = await youtubeApi.disconnect(channel?.id)
      toast('info', r.revoked ? 'Disconnected. Snipmint’s access was revoked at Google.' : 'Disconnected here. Google could not be reached to revoke access; remove Snipmint at myaccount.google.com/permissions.')
      setStatus(await youtubeApi.status())
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not disconnect.')
    } finally {
      setBusy(null)
    }
  }

  const generate = async (which: ClipOutput[]) => {
    if (!sourceId || !which.length) return
    setBusy('generate')
    try {
      const res = await metadataApi.generate(
        sourceId,
        which.map((c) => ({ clip_id: c.id, start: c.start, end: c.end, title: c.title })),
        language,
      )
      setRecords(res.clips)
      setOpen((o) => o ?? which[0]?.id ?? null)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not write the metadata.')
    } finally {
      setBusy(null)
    }
  }

  const publish = async () => {
    if (!jobId || !sourceId) return
    setBusy('publish')
    try {
      const res = await youtubeApi.publish({
        channel_id: channel?.id ?? null,
        clips: chosen.map((c) => toClipToPublish(records[c.id], { jobId, file: c.video!, sourceId })),
        privacy,
        made_for_kids: kids === 'yes',
        publish_at: scheduleIso,
        allow_duplicate: allowDuplicate,
        confirmed: true,
      })
      setConfirming(false)
      toast('success', scheduleIso ? 'Uploading now; YouTube will publish at the time you picked.' : 'Publishing started.')
      await refreshItems()
      stop.current?.()
      stop.current = watchJob(
        res.task_id,
        () => void refreshItems(),
        () => void refreshItems(),
        () => void refreshItems(),
        'tasks',
      )
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not start publishing.')
    } finally {
      setBusy(null)
    }
  }

  const blocked = !channel
    ? 'Connect a channel first.'
    : !chosen.length
      ? 'Pick at least one clip with a title.'
      : dupes.size
        ? 'Two clips share a title.'
        : scheduleOn && !scheduleIso
          ? 'Pick a date and time.'
          : null

  const bar = createPortal(
    <div className="step-nav">
      <div className="step-nav__inner">
        <Button variant="secondary" onClick={() => navigate('/export')}>
          Back
        </Button>
        <div className="step-nav__next">
          {blocked && status?.configured && <span className="step-nav__error">{blocked}</span>}
          <Button variant="primary" className="btn--lg" disabled={!!blocked || !status?.configured} onClick={() => setConfirming(true)}>
            {scheduleOn ? 'Schedule' : 'Publish'} {chosen.length} clip{chosen.length === 1 ? '' : 's'}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  )

  return (
    <div className="step">
      {bar}
      <header className="step-head">
        <h1 className="display">Publish to YouTube</h1>
        <p className="lede">Optional. Upload your clips as Shorts with titles and descriptions written from what is said in them.</p>
      </header>

      {status?.config_error && (
        <Banner tone="error" title="YouTube publishing is misconfigured">
          {status.config_error}
        </Banner>
      )}

      {status && !status.configured && !status.config_error && (
        <Banner tone="info" title="YouTube publishing isn’t set up on this server">
          Snipmint works fine without it: download your clips on the previous screen. To publish from here, create OAuth
          credentials in Google Cloud and set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (plus GOOGLE_REDIRECT_URI) in .env, or in
          .env.development for local development with a localhost callback (see the README). Then restart Snipmint.
        </Banner>
      )}

      {status?.configured && (
        <section className="section" aria-labelledby="channel-title">
          <div className="section__head">
            <h2 className="section__title" id="channel-title">
              Channel
            </h2>
            <p className="section__hint">
              Snipmint only asks to upload videos and read your channel’s name. Your sign-in is stored encrypted on this
              computer and never leaves it except to talk to Google.
            </p>
          </div>
          {channel ? (
            <div className="channel-card">
              {channel.thumbnail && <img className="channel-card__img" src={channel.thumbnail} alt="" width={40} height={40} />}
              <span className="channel-card__text">
                <strong>{channel.title}</strong>
                <span className="field__hint">{channel.custom_url ?? channel.id}</span>
              </span>
              <Button variant="secondary" busy={busy === 'disconnect'} onClick={() => void disconnect()}>
                Disconnect
              </Button>
            </div>
          ) : (
            <div className="row">
              <Button variant="primary" busy={busy === 'connect'} onClick={() => void connect()}>
                Connect a YouTube channel
              </Button>
            </div>
          )}
          {!channel && hostMismatch && (
            <p className="field__hint field__hint--warn">
              Google sends you back to {redirectHost}. Open Snipmint at that address (not {window.location.hostname}) or the
              sign-in can’t finish.
            </p>
          )}
          <p className="field__hint">
            By default a Google Cloud project can upload 100 videos a day (YouTube’s Video Uploads quota); your project’s
            limit may differ. {status.environment === 'development' ? 'Development mode: using the local sign-in settings.' : ''}
          </p>
        </section>
      )}

      {!job || job.status !== 'done' || !clips.length ? (
        <Banner tone="warn" title="Make the clips first">
          Publishing uses the clips from the <Link to="/export">Make clips</Link> step.
        </Banner>
      ) : (
        <section className="section" aria-labelledby="meta-title">
          <div className="section__head">
            <h2 className="section__title" id="meta-title">
              Titles and descriptions
            </h2>
            <p className="section__hint">Written only from what is said in each clip. Edit anything; regenerating a field never touches the others you changed.</p>
          </div>
          <div className="row">
            <label className="field__label" htmlFor="meta-lang">
              Language
            </label>
            <select
              id="meta-lang"
              className="select meta-lang"
              value={language}
              onChange={(e) => {
                setLanguage(e.target.value)
                try {
                  window.localStorage.setItem(LANG_KEY, e.target.value)
                } catch {
                  /* ignore */
                }
              }}
            >
              {(options?.languages ?? [{ code: 'en', label: 'English' }]).map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
            <Button variant={missing.length ? 'primary' : 'secondary'} busy={busy === 'generate'} onClick={() => void generate(missing.length ? missing : clips)}>
              {missing.length ? `Write metadata for ${missing.length} clip${missing.length === 1 ? '' : 's'}` : 'Write all again in this language'}
            </Button>
          </div>

          <ol className="publish-clips">
            {clips.map((c) => {
              const rec = records[c.id]
              const on = picked.includes(c.id)
              return (
                <li key={c.id} className={`publish-clip ${on ? 'publish-clip--on' : ''}`}>
                  <div className="publish-clip__head">
                    <video className="publish-clip__video" preload="metadata" muted src={clipFileUrl(job.id, c.video!)} />
                    <span className="publish-clip__text">
                      <label className="publish-clip__pick">
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={!rec}
                          onChange={() => setPicked((p) => (p.includes(c.id) ? p.filter((x) => x !== c.id) : [...p, c.id]))}
                        />
                        <strong>{rec?.metadata.title ?? c.title ?? `Clip ${c.index}`}</strong>
                      </label>
                      <span className="field__hint">
                        Clip {c.index} · {clock(c.start)}–{clock(c.end)}
                        {rec ? ` · ${rec.metadata.hashtags.join(' ')}` : ' · no metadata yet'}
                      </span>
                    </span>
                    {rec && (
                      <Button variant="text" aria-expanded={open === c.id} onClick={() => setOpen(open === c.id ? null : c.id)}>
                        {open === c.id ? 'Close' : 'Edit'}
                      </Button>
                    )}
                  </div>
                  {rec && open === c.id && (
                    <MetadataEditor
                      record={rec}
                      categories={options?.categories ?? { [rec.metadata.category_id]: rec.metadata.category }}
                      language={language}
                      duplicateTitle={dupes.has(c.id)}
                      onRecord={(r) => setRecords((all) => ({ ...all, [r.clip_id]: r }))}
                    />
                  )}
                  {rec && open !== c.id && dupes.has(c.id) && <p className="field__error">Same title as another clip. Edit one of them.</p>}
                </li>
              )
            })}
          </ol>
        </section>
      )}

      {status?.configured && clips.length > 0 && (
        <section className="section" aria-labelledby="visibility-title">
          <h2 className="section__title" id="visibility-title">
            Visibility
          </h2>
          <Segmented
            label="Visibility"
            value={privacy}
            onChange={setPrivacy}
            items={[
              { id: 'private', label: 'Private' },
              { id: 'unlisted', label: 'Unlisted' },
              { id: 'public', label: 'Public' },
            ]}
          />
          <div className="field">
            <span className="field__label">Made for kids?</span>
            <Segmented
              label="Made for kids"
              value={kids}
              onChange={setKids}
              items={[
                { id: 'no', label: 'No, it’s not made for kids' },
                { id: 'yes', label: 'Yes, it’s made for kids' },
              ]}
            />
            <p className="field__hint">YouTube requires this answer by law (COPPA). Made-for-kids videos lose comments and some features.</p>
          </div>
          <Switch
            label="Schedule instead of publishing now"
            hint="The clips upload now as private and YouTube makes them public at this time, even if Snipmint is closed."
            checked={scheduleOn}
            onChange={setScheduleOn}
          />
          {scheduleOn && (
            <div className="field">
              <label className="field__label" htmlFor="publish-at">
                Goes public at (your time)
              </label>
              <input id="publish-at" className="input meta-when" type="datetime-local" value={scheduleAt} onChange={(e) => setScheduleAt(e.target.value)} />
            </div>
          )}
          <label className="publish-clip__pick">
            <input type="checkbox" checked={allowDuplicate} onChange={(e) => setAllowDuplicate(e.target.checked)} />
            Upload again anyway if a clip is already on this channel
          </label>
        </section>
      )}

      {status?.configured && (
        <section className="section" aria-labelledby="dash-title">
          <div className="section__head">
            <h2 className="section__title" id="dash-title">
              Uploads
            </h2>
            <p className="section__hint">Up to {status.concurrency} upload at the same time. Interrupted uploads continue when Snipmint starts again.</p>
          </div>
          <PublishDashboard items={items} channels={status.channels} onChange={() => void refreshItems()} />
        </section>
      )}

      {confirming && channel && (
        <ConfirmDialog
          title={scheduleIso ? 'Schedule these clips?' : 'Publish these clips?'}
          confirmLabel={scheduleIso ? `Schedule ${chosen.length}` : `Upload ${chosen.length}`}
          busy={busy === 'publish'}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void publish()}
        >
          <p>
            To <strong>{channel.title}</strong> as <strong>{scheduleIso ? 'private, then public' : privacy}</strong>
            {scheduleIso ? ` at ${whenText(scheduleIso)}` : ''}, made for kids: <strong>{kids}</strong>.
          </p>
          <ul className="dialog__list">
            {chosen.map((c) => (
              <li key={c.id}>{records[c.id].metadata.title}</li>
            ))}
          </ul>
          <p className="field__hint">Only publish clips you own or have the rights to. Uploads count against your daily YouTube quota.</p>
        </ConfirmDialog>
      )}
    </div>
  )
}
