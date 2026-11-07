import { useId, useState } from 'react'
import { type FieldName, type MetadataRecord, metadataApi } from '../api/youtube'
import {
  type Drafts,
  FIELD_SPECS,
  ORIGIN_LABEL,
  TEXT_FIELDS,
  type TextField,
  draftsAfterRegenerate,
  fieldText,
  measure,
  originOf,
  parseField,
  shown,
} from '../lib/metadata'
import { useSession } from '../state/session'
import { Badge, Button } from './ui'

const ORIGIN_TONE = { ai: 'yellow', fallback: 'plain', edited: 'good' } as const

async function copy(text: string, toast: (k: 'success' | 'error', m: string) => void) {
  try {
    await navigator.clipboard.writeText(text)
    toast('success', 'Copied.')
  } catch {
    toast('error', 'Could not copy. Select the text and copy it by hand.')
  }
}

function FieldRow({
  field,
  record,
  drafts,
  busy,
  onDraft,
  onSave,
  onRegenerate,
}: {
  field: TextField
  record: MetadataRecord
  drafts: Drafts
  busy: FieldName | 'all' | null
  onDraft: (field: TextField, text: string) => void
  onSave: (field: TextField) => void
  onRegenerate: (field: TextField) => void
}) {
  const id = useId()
  const toast = useSession((s) => s.toast)
  const spec = FIELD_SPECS[field]
  const value = shown(record, drafts, field)
  const m = measure(field, value)
  const origin = originOf(record, field, drafts)
  const counter =
    spec.unit === 'items' ? `${m.count} / ${spec.min ? `${spec.min}–` : ''}${m.limit}` : `${m.count} / ${m.limit}`
  const Input = spec.multiline ? 'textarea' : 'input'
  return (
    <div className="meta-field">
      <div className="meta-field__head">
        <label className="field__label" htmlFor={id}>
          {spec.label}
        </label>
        <Badge tone={ORIGIN_TONE[origin]}>{ORIGIN_LABEL[origin]}</Badge>
        <span
          className={`meta-field__count ${m.over || m.under ? 'meta-field__count--bad' : ''}`}
          aria-label={`${spec.label}: ${counter} ${spec.unit === 'items' ? 'items' : 'characters'}`}
        >
          {counter}
        </span>
        <span className="meta-field__actions">
          <Button variant="text" busy={busy === field} disabled={busy !== null} onClick={() => onRegenerate(field)} aria-label={`Regenerate ${spec.label}`}>
            Regenerate
          </Button>
          <Button variant="text" onClick={() => void copy(value, toast)} aria-label={`Copy ${spec.label}`}>
            Copy
          </Button>
        </span>
      </div>
      <Input
        id={id}
        className={`input ${spec.multiline ? 'meta-field__area' : ''}`}
        value={value}
        rows={spec.multiline ? 4 : undefined}
        aria-invalid={m.over || m.under || undefined}
        onChange={(e) => onDraft(field, e.target.value)}
        onBlur={() => onSave(field)}
      />
      {spec.hint && <p className="field__hint">{spec.hint}</p>}
    </div>
  )
}

/** Every publishing field of one clip: edit, regenerate one field, copy, with counts and where it came from. */
export function MetadataEditor({
  record,
  categories,
  language,
  onRecord,
  duplicateTitle,
  collapsible,
}: {
  record: MetadataRecord
  categories: Record<string, string>
  language: string
  onRecord: (r: MetadataRecord) => void
  duplicateTitle?: boolean
  /** Show title, description and hashtags right away; keep the rarely changed fields behind "More options". */
  collapsible?: boolean
}) {
  const toast = useSession((s) => s.toast)
  const [drafts, setDrafts] = useState<Drafts>({})
  const [busy, setBusy] = useState<FieldName | 'all' | null>(null)
  const [more, setMore] = useState(false)
  const catId = useId()
  const moreId = useId()

  const save = async (field: TextField) => {
    const text = drafts[field]
    if (text === undefined) return
    if (text === fieldText(record.metadata, field)) {
      setDrafts((d) => draftsAfterRegenerate(d, field))
      return
    }
    try {
      const next = await metadataApi.edit(record.source_id, record.clip_id, { [field]: parseField(field, text) })
      setDrafts((d) => draftsAfterRegenerate(d, field))
      onRecord(next)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not save.')
    }
  }

  const regenerate = async (field: FieldName | null) => {
    setBusy(field ?? 'all')
    try {
      const next = await metadataApi.regenerate(record.source_id, record.clip_id, field, language)
      // only the regenerated field loses its unsaved draft; other edits stay put
      setDrafts((d) => draftsAfterRegenerate(d, field))
      onRecord(next)
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not regenerate.')
    } finally {
      setBusy(null)
    }
  }

  const pickTitle = async (title: string) => {
    try {
      onRecord(await metadataApi.edit(record.source_id, record.clip_id, { title }))
      setDrafts((d) => draftsAfterRegenerate(d, 'title'))
    } catch (e) {
      toast('error', e instanceof Error ? e.message : 'Could not use that title.')
    }
  }

  const m = record.metadata
  const quick: TextField[] = ['title', 'description', 'hashtags']
  const row = (f: TextField) => (
    <FieldRow key={f} field={f} record={record} drafts={drafts} busy={busy} onDraft={(fl, t) => setDrafts((d) => ({ ...d, [fl]: t }))} onSave={save} onRegenerate={(fl) => void regenerate(fl)} />
  )
  const head = (
    <>
      <div className="meta-editor__bar">
        <span className="field__hint">
          {record.provider === 'gemini' ? 'Written by Gemini from the clip’s transcript.' : 'Built from the clip’s own words (free mode).'}
        </span>
        <Button variant="secondary" busy={busy === 'all'} disabled={busy !== null} onClick={() => void regenerate(null)}>
          Regenerate all (keeps your edits)
        </Button>
      </div>
      <div className={`meta-approval ${record.approved ? 'meta-approval--on' : ''}`}>
        <span className="meta-approval__text">
          <strong>{record.approved ? 'Approved for publishing' : 'Not approved yet'}</strong>
          <span className="field__hint">
            {record.approved
              ? record.approved_by === 'auto'
                ? 'Approved automatically (auto-approve is on). Any edit withdraws it.'
                : 'Automation may publish this text. Any edit or regeneration withdraws the approval.'
              : 'Automation only publishes metadata you approved. Check the text first: it is written from the clip and may need fixes.'}
          </span>
        </span>
        <Button
          variant={record.approved ? 'secondary' : 'primary'}
          disabled={busy !== null || Object.keys(drafts).length > 0 || (!record.approved && duplicateTitle)}
          title={Object.keys(drafts).length ? 'Finish your edit first' : undefined}
          onClick={async () => {
            try {
              onRecord(await metadataApi.approve(record.source_id, record.clip_id, !record.approved))
            } catch (e) {
              toast('error', e instanceof Error ? e.message : 'Could not change the approval.')
            }
          }}
        >
          {record.approved ? 'Withdraw approval' : 'Approve'}
        </Button>
      </div>
    </>
  )
  const notes = (
    <>
      {record.note && <p className="field__hint field__hint--warn">{record.note}</p>}
      {duplicateTitle && <p className="field__error">Another clip from this video has the same title. Titles must be unique.</p>}
    </>
  )
  const variants = (
      <div className="meta-variants">
        <div className="meta-field__head">
          <span className="field__label">Title ideas</span>
          <Badge tone={ORIGIN_TONE[originOf(record, 'title_variants')]}>{ORIGIN_LABEL[originOf(record, 'title_variants')]}</Badge>
          <span className="meta-field__actions">
            <Button variant="text" busy={busy === 'title_variants'} disabled={busy !== null} onClick={() => void regenerate('title_variants')} aria-label="Regenerate title ideas">
              Regenerate
            </Button>
          </span>
        </div>
        <ul className="meta-variants__list">
          {m.title_variants.map((v) => (
            <li key={v.title}>
              <button
                type="button"
                className={`meta-variant ${v.title === m.title ? 'meta-variant--on' : ''}`}
                aria-pressed={v.title === m.title}
                onClick={() => void pickTitle(v.title)}
              >
                <span className="meta-variant__title">{v.title}</span>
                <span className="meta-variant__reason">{v.reason}</span>
              </button>
            </li>
          ))}
        </ul>
        {m.title_reason && <p className="field__hint">Why this pick: {m.title_reason}</p>}
      </div>
  )
  const category = (
      <div className="meta-field">
        <div className="meta-field__head">
          <label className="field__label" htmlFor={catId}>
            Category
          </label>
          <Badge tone={ORIGIN_TONE[originOf(record, 'category')]}>{ORIGIN_LABEL[originOf(record, 'category')]}</Badge>
          <span className="meta-field__actions">
            <Button variant="text" busy={busy === 'category'} disabled={busy !== null} onClick={() => void regenerate('category')} aria-label="Regenerate Category">
              Regenerate
            </Button>
          </span>
        </div>
        <select
          id={catId}
          className="select"
          value={m.category_id}
          onChange={async (e) => {
            try {
              onRecord(await metadataApi.edit(record.source_id, record.clip_id, { category_id: e.target.value }))
            } catch (err) {
              toast('error', err instanceof Error ? err.message : 'Could not save.')
            }
          }}
        >
          {Object.entries(categories).map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
      </div>
  )

  if (collapsible) {
    return (
      <div className="meta-editor meta-editor--quick">
        {notes}
        {quick.map(row)}
        <div className="meta-more">
          <Button variant="text" aria-expanded={more} aria-controls={moreId} onClick={() => setMore((v) => !v)}>
            {more ? 'Fewer options' : 'More options'}
          </Button>
          <span className="field__hint">Title ideas, tags, hook, category and approval for automation.</span>
          {record.approved && <Badge tone="good">Approved</Badge>}
        </div>
        {more && (
          <div className="meta-editor" id={moreId}>
            {head}
            {variants}
            {TEXT_FIELDS.filter((f) => !quick.includes(f)).map(row)}
            {category}
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="meta-editor">
      {head}
      {notes}
      {row('title')}
      {variants}
      {TEXT_FIELDS.filter((f) => f !== 'title').map(row)}
      {category}
    </div>
  )
}
