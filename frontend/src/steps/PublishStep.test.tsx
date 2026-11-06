import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MetadataRecord, PublishItem } from '../api/youtube'
import { MetadataEditor } from '../components/MetadataEditor'
import { PublishDashboard } from '../components/PublishDashboard'
import { RECORD } from '../lib/metadata.test'
import { useClips } from '../state/clips'
import { PublishStep } from './PublishStep'

type Call = { path: string; method: string; body: unknown }
let calls: Call[] = []

function mockFetch(routes: Record<string, (body: unknown) => unknown>) {
  calls = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).split('?')[0]
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ path, method, body })
    const handler = routes[`${method} ${path}`]
    if (!handler) return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
    return new Response(JSON.stringify(handler(body)), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
}

class FakeEventSource {
  addEventListener() {}
  close() {}
  onerror: (() => void) | null = null
}

beforeEach(() => {
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource
})

describe('MetadataEditor', () => {
  it('shows counts and origins, and regenerating one field keeps another unsaved edit', async () => {
    const regenerated: MetadataRecord = {
      ...RECORD,
      origin: { ...RECORD.origin, hook: 'fallback' },
      metadata: { ...RECORD.metadata, hook: 'A brand new hook', description: 'server text' },
    }
    mockFetch({ 'POST /api/metadata/yt-AAAAAAAAAAA/m01/regenerate': () => regenerated })
    let current = RECORD
    const { rerender } = render(
      <MetadataEditor record={current} categories={{ '27': 'Education' }} language="en" onRecord={(r) => (current = r)} />,
    )
    expect(screen.getByLabelText(/title: 22 \/ 100 characters/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/hashtags: 3 \/ 3–5 items/i)).toBeInTheDocument()
    expect(screen.getAllByText('Edited').length).toBeGreaterThan(0)

    // the person types in the description (not saved yet) ...
    const desc = screen.getByLabelText('Description', { selector: 'textarea' }) as HTMLTextAreaElement
    fireEvent.change(desc, { target: { value: 'My own description' } })
    // ... then regenerates the hook
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate Hook' }))
    await waitFor(() => expect(calls.some((c) => c.path.endsWith('/regenerate'))).toBe(true))
    expect(calls.find((c) => c.path.endsWith('/regenerate'))!.body).toMatchObject({ field: 'hook' })
    await waitFor(() => expect(current).toEqual(regenerated))
    rerender(<MetadataEditor record={current} categories={{ '27': 'Education' }} language="en" onRecord={(r) => (current = r)} />)
    expect((screen.getByLabelText('Hook', { selector: 'input' }) as HTMLInputElement).value).toBe('A brand new hook')
    expect((screen.getByLabelText('Description', { selector: 'textarea' }) as HTMLTextAreaElement).value).toBe('My own description')
  })

  it('saves an edit on blur', async () => {
    mockFetch({ 'PUT /api/metadata/yt-AAAAAAAAAAA/m01': (b) => ({ ...RECORD, metadata: { ...RECORD.metadata, ...(b as { changes: object }).changes } }) })
    render(<MetadataEditor record={RECORD} categories={{ '27': 'Education' }} language="en" onRecord={() => {}} />)
    const tags = screen.getByLabelText('Tags', { selector: 'input' })
    fireEvent.change(tags, { target: { value: 'one, two words' } })
    fireEvent.blur(tags)
    await waitFor(() => expect(calls[0]).toMatchObject({ method: 'PUT', body: { changes: { tags: ['one', 'two words'] } } }))
  })
})

const ITEM: PublishItem = {
  id: 'i1', batch_id: 'b', channel_id: 'UC1', job_id: 'j', clip_file: 'clip_01.mp4', clip_id: 'm01', source_id: 's',
  file_size: 1, status: 'FAILED', progress: 0.3, attempts: 1, video_id: null, video_url: null, error: 'Gave up after 6 retries',
  note: null, created: 0, updated: 0, scheduled: false,
  details: { title: 'A clip', description: '', tags: [], category_id: '22', privacy: 'private', made_for_kids: false, language: null, publish_at: null },
}

describe('PublishDashboard', () => {
  it('shows status, links, errors and retry', async () => {
    mockFetch({ 'POST /api/youtube/publish/i1/retry': () => ({ batch_id: 'b2', task_id: 'b2', items: [] }) })
    const onChange = vi.fn()
    const published = { ...ITEM, id: 'i2', status: 'PUBLISHED' as const, error: null, video_url: 'https://www.youtube.com/shorts/abc' }
    const uploading = { ...ITEM, id: 'i3', status: 'UPLOADING' as const, error: null, progress: 0.42 }
    render(<PublishDashboard items={[ITEM, published, uploading]} channels={[{ id: 'UC1', title: 'My Channel', custom_url: null, thumbnail: null, connected_at: '', scopes: [] }]} onChange={onChange} />)
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('Gave up after 6 retries')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /shorts\/abc/ })).toHaveAttribute('href', 'https://www.youtube.com/shorts/abc')
    expect(screen.getByText('Uploading 42%')).toBeInTheDocument()
    expect(screen.getAllByText(/My Channel/).length).toBe(3)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(calls[0].path).toBe('/api/youtube/publish/i1/retry')
  })
})

describe('PublishStep', () => {
  const routes = (configured: boolean) => ({
    'GET /api/youtube/status': () => ({
      configured, encryption_available: true, redirect_uri: null, concurrency: 2, scopes: [],
      channels: configured ? [{ id: 'UC1', title: 'My Channel', custom_url: '@me', thumbnail: null, connected_at: '', scopes: [] }] : [],
      default_channel: configured ? 'UC1' : null,
    }),
    'GET /api/metadata/options': () => ({ languages: [{ code: 'en', label: 'English' }, { code: 'hi', label: 'Hindi' }], categories: { '22': 'People & Blogs', '27': 'Education' } }),
    'GET /api/youtube/publish': () => [],
    'GET /api/jobs/job1': () => ({
      id: 'job1', status: 'done', error: null,
      result: { clips: [{ id: 'm01', index: 1, title: 'Moment', start: 0, end: 30, video: 'clip_01.mp4', srt: null, error: null, layout: null, framing_note: null, voiceover: false, note: null }] },
    }),
    'GET /api/metadata/yt-AAAAAAAAAAA': () => ({ source_id: 'yt-AAAAAAAAAAA', clips: { m01: RECORD } }),
    'POST /api/youtube/publish': () => ({ batch_id: 'b1', task_id: 'b1', items: [] }),
  })

  beforeEach(() => {
    useClips.setState({ sourceId: 'yt-AAAAAAAAAAA', jobId: 'job1' })
  })

  it('explains the setup when YouTube is not configured', async () => {
    mockFetch(routes(false))
    render(
      <MemoryRouter>
        <PublishStep />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/isn’t set up on this server/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /publish \d+ clip/i })).toBeDisabled()
  })

  it('shows why the Google settings cannot work', async () => {
    mockFetch({
      ...routes(false),
      'GET /api/youtube/status': () => ({
        configured: false, encryption_available: true, environment: 'production', redirect_uri: 'http://clips.example.com/api/youtube/oauth/callback',
        config_error: 'GOOGLE_REDIRECT_URI must use https:// on a public host.', concurrency: 2, scopes: [], channels: [], default_channel: null,
      }),
    })
    render(
      <MemoryRouter>
        <PublishStep />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/misconfigured/i)).toBeInTheDocument()
    expect(screen.getByText(/must use https/)).toBeInTheDocument()
    expect(screen.queryByText(/isn’t set up on this server/i)).toBeNull()
  })

  it('asks for confirmation before uploading', async () => {
    mockFetch(routes(true))
    render(
      <MemoryRouter>
        <PublishStep />
      </MemoryRouter>,
    )
    expect(await screen.findByText('My Channel')).toBeInTheDocument()
    await screen.findByText('Why do you never save?')
    fireEvent.click(screen.getByRole('radio', { name: 'Unlisted' }))
    const go = screen.getByRole('button', { name: /publish 1 clip/i })
    await waitFor(() => expect(go).toBeEnabled())
    fireEvent.click(go)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('unlisted')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/youtube/publish')).toBe(false)
    fireEvent.click(within(dialog).getByRole('button', { name: /upload 1/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/api/youtube/publish')).toBe(true))
    const body = calls.find((c) => c.path === '/api/youtube/publish' && c.method === 'POST')!.body as Record<string, unknown>
    expect(body).toMatchObject({ confirmed: true, privacy: 'unlisted', made_for_kids: false, channel_id: 'UC1', publish_at: null })
    expect((body.clips as { title: string }[])[0].title).toBe('Why do you never save?')
  })
})
