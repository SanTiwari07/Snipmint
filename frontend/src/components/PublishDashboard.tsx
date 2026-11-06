import { ACTIVE_STATUSES, type Channel, type PublishItem, type PublishStatus, youtubeApi } from '../api/youtube'
import { useSession } from '../state/session'
import { Badge, Button, ProgressBar } from './ui'

export const STATUS_LABEL: Record<PublishStatus, string> = {
  QUEUED: 'Queued',
  PREPARING: 'Preparing',
  UPLOADING: 'Uploading',
  PUBLISHED: 'Published',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}
const STATUS_TONE: Record<PublishStatus, 'plain' | 'yellow' | 'good' | 'bad' | 'warn'> = {
  QUEUED: 'plain',
  PREPARING: 'yellow',
  UPLOADING: 'yellow',
  PUBLISHED: 'good',
  FAILED: 'bad',
  CANCELLED: 'warn',
}

export function whenText(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** Status of every upload: channel, per-clip progress, links, errors, retry and cancel. */
export function PublishDashboard({
  items,
  channels,
  onChange,
}: {
  items: PublishItem[]
  channels: Channel[]
  onChange: () => void
}) {
  const toast = useSession((s) => s.toast)
  const act = async (what: () => Promise<unknown>, done: string) => {
    try {
      await what()
      toast('info', done)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'That did not work.')
    } finally {
      onChange()
    }
  }
  if (!items.length) return <p className="field__hint">Nothing published from Snipmint yet.</p>
  const channelName = (id: string) => channels.find((c) => c.id === id)?.title ?? id
  return (
    <ol className="publish-list" aria-label="Uploads">
      {items.map((i) => {
        const active = ACTIVE_STATUSES.includes(i.status)
        return (
          <li key={i.id} className="publish-row" data-status={i.status}>
            <div className="publish-row__main">
              <strong className="publish-row__title">{i.details.title}</strong>
              <span className="field__hint">
                {channelName(i.channel_id)} · {i.details.privacy}
                {i.scheduled && i.details.publish_at ? ` · goes public ${whenText(i.details.publish_at)}` : ''}
              </span>
              {i.status === 'UPLOADING' && <ProgressBar fraction={i.progress} label={`Uploading ${i.details.title}`} />}
              {i.error && <span className="field__error">{i.error}</span>}
              {i.note && !i.error && <span className="field__hint">{i.note}</span>}
              {i.video_url && (
                <a className="publish-row__link" href={i.video_url} target="_blank" rel="noreferrer">
                  {i.video_url}
                </a>
              )}
            </div>
            <div className="publish-row__side">
              <Badge tone={STATUS_TONE[i.status]}>
                {i.scheduled && i.status === 'PUBLISHED' ? 'Scheduled' : STATUS_LABEL[i.status]}
                {i.status === 'UPLOADING' ? ` ${Math.round(i.progress * 100)}%` : ''}
              </Badge>
              <div className="row">
                {active && (
                  <Button variant="text" onClick={() => void act(() => youtubeApi.cancel(i.id), 'Cancelling…')}>
                    Cancel
                  </Button>
                )}
                {(i.status === 'FAILED' || i.status === 'CANCELLED') && (
                  <>
                    <Button variant="secondary" onClick={() => void act(() => youtubeApi.retry(i.id), 'Trying again.')}>
                      Retry
                    </Button>
                    <Button variant="text" onClick={() => void act(() => youtubeApi.remove(i.id), 'Removed from the list.')}>
                      Remove
                    </Button>
                  </>
                )}
              </div>
            </div>
          </li>
        )
      })}
    </ol>
  )
}
