import { useEffect, useMemo, useState } from 'react'
import { clipsApi } from '../api/clips'
import { useDebounced } from '../hooks/useAsync'
import { useCatalog } from '../state/catalog'
import { previewMoment, useClips } from '../state/clips'
import { useDraft } from '../state/project'
import { Switch } from './ui'

/** A real frame of the first picked clip with real captions, from the real renderer. */
export function PhoneFrame() {
  const captions = useDraft((s) => s.captions)
  const formatId = useDraft((s) => s.format)
  const format = useCatalog((s) => s.formats.find((f) => f.id === formatId))
  const sourceId = useClips((s) => s.sourceId)
  const source = useClips((s) => s.source)
  const selected = useClips((s) => s.selected)
  const edits = useClips((s) => s.edits)
  const moment = useMemo(() => previewMoment({ source, selected, edits }), [source, selected, edits])
  const captionsEnabled = useClips((s) => s.captionsEnabled)
  const framing = useClips((s) => s.framing)
  const [zones, setZones] = useState(false)
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const body =
    sourceId && moment && format?.width
      ? {
          source_id: sourceId,
          start: moment.start,
          end: moment.end,
          keywords: moment.keywords,
          captions,
          captions_enabled: captionsEnabled,
          format: format.id,
          framing,
        }
      : null
  const key = useDebounced(body ? JSON.stringify(body) : '', 350)

  useEffect(() => {
    if (!key) return
    const ctl = new AbortController()
    setLoading(true)
    clipsApi
      .preview(JSON.parse(key), ctl.signal)
      .then((blob) => {
        setUrl((old) => {
          if (old) URL.revokeObjectURL(old)
          return URL.createObjectURL(blob)
        })
        setError(null)
        setLoading(false)
      })
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      })
    return () => ctl.abort()
  }, [key])

  const ratio = format?.width && format.height ? `${format.width} / ${format.height}` : '9 / 16'
  const landscape = !!format?.width && !!format.height && format.width > format.height

  return (
    <figure className="preview" aria-label="Clip preview">
      <div className={`phone ${landscape ? 'phone--landscape' : ''}`} style={{ aspectRatio: ratio }} aria-busy={loading}>
        {url && <img className="phone__img" src={url} alt="A frame of your clip with captions" />}
        {!url && !error && <div className="phone__placeholder">{moment ? 'Drawing a frame…' : 'Pick a moment to see it here'}</div>}
        {zones && format && (
          <div className="phone__zones" aria-hidden="true">
            <span className="zone zone--top" style={{ height: `${(format.safe_zone.top ?? 0) * 100}%` }} />
            <span className="zone zone--bottom" style={{ height: `${(format.safe_zone.bottom ?? 0) * 100}%` }} />
            <span className="zone zone--right" style={{ width: `${(format.safe_zone.right ?? 0) * 100}%` }} />
          </div>
        )}
        {loading && url && <span className="phone__busy" aria-hidden="true" />}
      </div>
      <figcaption className="preview__meta">
        <span className="preview__note">{moment ? `From “${moment.title || 'your first clip'}”` : 'Your clip'}</span>
        {format && format.id === 'reels' && <Switch label="Show app buttons" checked={zones} onChange={setZones} />}
      </figcaption>
      {error && <p className="preview__error">{error}</p>}
    </figure>
  )
}
