import { describe, expect, it } from 'vitest'
import type { MetadataRecord } from '../api/youtube'
import { draftsAfterRegenerate, duplicateTitles, fieldText, localToIso, measure, originOf, parseField, toClipToPublish } from './metadata'

export const RECORD: MetadataRecord = {
  clip_id: 'm01',
  source_id: 'yt-AAAAAAAAAAA',
  start: 0,
  end: 30,
  provider: 'fallback',
  note: null,
  variant: 0,
  updated: 0,
  origin: { title: 'fallback', description: 'edited' },
  metadata: {
    title: 'Why do you never save?',
    title_variants: [
      { title: 'Why do you never save?', reason: 'question' },
      { title: 'Saving and budgeting', reason: 'topics' },
      { title: 'Track every expense', reason: 'line' },
    ],
    title_reason: 'It asks a question',
    description: 'A clip about saving.',
    hashtags: ['#Shorts', '#Saving', '#Money'],
    tags: ['saving', 'money tips'],
    hook: 'Why do you never save?',
    pinned_comment: 'What do you think?',
    language: 'hinglish',
    category_id: '27',
    category: 'Education',
    seo_keywords: ['saving'],
  },
}

describe('metadata helpers', () => {
  it('counts characters and list items against the limits', () => {
    expect(measure('title', 'x'.repeat(101))).toMatchObject({ count: 101, limit: 100, over: true })
    expect(measure('hashtags', '#a #b')).toMatchObject({ count: 2, under: true })
    expect(measure('hashtags', '#a #b #c #d #e #f').over).toBe(true)
    // tags: YouTube counts quotes around tags with spaces and the commas between them
    expect(measure('tags', 'saving, money tips').count).toBe(6 + 10 + 2 + 1)
    expect(measure('title', 'नमस्ते').count).toBe([...'नमस्ते'].length)
  })

  it('parses list fields', () => {
    expect(parseField('hashtags', 'Shorts, #Money  saving')).toEqual(['#Shorts', '#Money', '#saving'])
    expect(parseField('tags', 'a, b c ,')).toEqual(['a', 'b c'])
    expect(fieldText(RECORD.metadata, 'hashtags')).toBe('#Shorts #Saving #Money')
    expect(fieldText(RECORD.metadata, 'tags')).toBe('saving, money tips')
  })

  it('regenerating one field keeps every other unsaved edit', () => {
    const drafts = { description: 'my words', hook: 'old hook draft' }
    expect(draftsAfterRegenerate(drafts, 'hook')).toEqual({ description: 'my words' })
    expect(draftsAfterRegenerate(drafts, null)).toEqual(drafts)
    expect(draftsAfterRegenerate(drafts, 'category')).toEqual(drafts)
  })

  it('knows where each field came from', () => {
    expect(originOf(RECORD, 'description')).toBe('edited')
    expect(originOf(RECORD, 'title')).toBe('fallback')
    expect(originOf(RECORD, 'hook', { hook: 'typing' })).toBe('edited')
  })

  it('builds the upload request from saved metadata', () => {
    const c = toClipToPublish(RECORD, { jobId: 'j1', file: 'clip_01.mp4', sourceId: 's' })
    expect(c).toMatchObject({ job_id: 'j1', clip_file: 'clip_01.mp4', title: RECORD.metadata.title, language: 'hi', category_id: '27' })
  })

  it('finds titles shared by two clips', () => {
    const other = { ...RECORD, clip_id: 'm02', metadata: { ...RECORD.metadata, title: 'why do you NEVER save' } }
    const third = { ...RECORD, clip_id: 'm03', metadata: { ...RECORD.metadata, title: 'Different' } }
    expect([...duplicateTitles([RECORD, other, third])].sort()).toEqual(['m01', 'm02'])
  })

  it('turns a local date-time into an ISO time with the offset', () => {
    const iso = localToIso('2030-01-02T03:04')!
    expect(iso).toMatch(/^2030-01-02T03:04:00[+-]\d\d:\d\d$/)
    expect(new Date(iso).getTime()).toBe(new Date('2030-01-02T03:04').getTime())
    expect(localToIso('')).toBeNull()
  })
})
