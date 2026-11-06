import type { ClipMetadata, ClipToPublish, FieldName, MetadataRecord, Origin } from '../api/youtube'

export type TextField = Exclude<FieldName, 'title_variants' | 'category'>

export interface FieldSpec {
  label: string
  /** Hard limit YouTube (or Snipmint) enforces. */
  limit: number
  /** What the counter measures. */
  unit: 'chars' | 'items'
  min?: number
  multiline?: boolean
  list?: boolean
  hint?: string
}

export const FIELD_SPECS: Record<TextField, FieldSpec> = {
  title: { label: 'Title', limit: 100, unit: 'chars', hint: 'Shorts feeds show about 40–70 characters.' },
  description: { label: 'Description', limit: 4800, unit: 'chars', multiline: true },
  hashtags: { label: 'Hashtags', limit: 5, min: 3, unit: 'items', list: true, hint: 'Added to the end of the description.' },
  tags: { label: 'Tags', limit: 500, unit: 'chars', list: true, hint: 'Separate with commas. YouTube allows 500 characters in total.' },
  hook: { label: 'Hook', limit: 150, unit: 'chars', hint: 'The opening line that keeps people watching.' },
  pinned_comment: { label: 'Pinned comment', limit: 500, unit: 'chars', multiline: true, hint: 'Post and pin it yourself after publishing.' },
  seo_keywords: { label: 'SEO keywords', limit: 15, unit: 'items', list: true, hint: 'Separate with commas.' },
}

export const TEXT_FIELDS = Object.keys(FIELD_SPECS) as TextField[]

/** The field as the person sees and copies it. */
export function fieldText(meta: ClipMetadata, field: TextField): string {
  const v = meta[field]
  if (Array.isArray(v)) return field === 'hashtags' ? v.join(' ') : v.join(', ')
  return v
}

/** Text from the input back into the API's shape. */
export function parseField(field: TextField, text: string): string | string[] {
  if (field === 'hashtags') {
    return text
      .split(/[\s,]+/)
      .map((h) => h.trim())
      .filter(Boolean)
      .map((h) => (h.startsWith('#') ? h : `#${h}`))
  }
  if (FIELD_SPECS[field].list) {
    return text
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
  }
  return text
}

/** Count against the field's limit: characters, or items for lists like hashtags. */
export function measure(field: TextField, text: string): { count: number; limit: number; over: boolean; under: boolean } {
  const spec = FIELD_SPECS[field]
  let count: number
  if (spec.unit === 'items') count = (parseField(field, text) as string[]).length
  else if (field === 'tags') {
    const tags = parseField(field, text) as string[]
    count = tags.reduce((n, t) => n + t.length + (t.includes(' ') ? 2 : 0), 0) + Math.max(0, tags.length - 1)
  } else count = [...text].length
  return { count, limit: spec.limit, over: count > spec.limit, under: spec.min !== undefined && count < spec.min }
}

export type Drafts = Partial<Record<TextField, string>>

/** What the input shows: an unsaved draft wins over the stored value. */
export function shown(record: MetadataRecord, drafts: Drafts, field: TextField): string {
  return drafts[field] ?? fieldText(record.metadata, field)
}

/**
 * Drafts to keep after a regeneration. Regenerating one field only drops that field's draft;
 * every other unsaved edit stays. "Regenerate all" keeps every draft (the server keeps edited fields too).
 */
export function draftsAfterRegenerate(drafts: Drafts, field: FieldName | null): Drafts {
  if (!field || field === 'title_variants' || field === 'category') return { ...drafts }
  const rest = { ...drafts }
  delete rest[field as TextField]
  return rest
}

export function originOf(record: MetadataRecord, field: FieldName, drafts: Drafts = {}): Origin {
  if (field in drafts) return 'edited'
  return record.origin[field] ?? (record.provider === 'gemini' ? 'ai' : 'fallback')
}

export const ORIGIN_LABEL: Record<Origin, string> = {
  ai: 'AI',
  fallback: 'From transcript',
  edited: 'Edited',
}

/** The upload request for one rendered clip, from its saved metadata. */
export function toClipToPublish(
  record: MetadataRecord,
  clip: { jobId: string; file: string; sourceId: string },
): ClipToPublish {
  const m = record.metadata
  return {
    job_id: clip.jobId,
    clip_file: clip.file,
    clip_id: record.clip_id,
    source_id: clip.sourceId,
    title: m.title,
    description: m.description,
    hashtags: m.hashtags,
    tags: m.tags,
    category_id: m.category_id,
    language: m.language === 'hinglish' ? 'hi' : m.language,
  }
}

/** Titles that clash inside one video (they must be unique). */
export function duplicateTitles(records: MetadataRecord[]): Set<string> {
  const seen = new Map<string, number>()
  for (const r of records) {
    const k = r.metadata.title.toLowerCase().replace(/[\W_]+/gu, ' ').trim()
    seen.set(k, (seen.get(k) ?? 0) + 1)
  }
  return new Set(records.filter((r) => (seen.get(r.metadata.title.toLowerCase().replace(/[\W_]+/gu, ' ').trim()) ?? 0) > 1).map((r) => r.clip_id))
}

/** `datetime-local` value (local time) → ISO string with the local offset. */
export function localToIso(local: string): string | null {
  if (!local) return null
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) return null
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const pad = (n: number) => String(Math.floor(Math.abs(n))).padStart(2, '0')
  return `${local.length === 16 ? `${local}:00` : local}${sign}${pad(off / 60)}:${pad(off % 60)}`
}
