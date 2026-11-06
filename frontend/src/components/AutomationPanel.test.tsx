import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AutomationStatus, AutomationTask } from '../api/automation'
import { RECORD } from '../lib/metadata.test'
import { AutomationPanel } from './AutomationPanel'
import { MetadataEditor } from './MetadataEditor'

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

const SETTINGS = {
  enabled: true, auto_approve: false, language: 'en', privacy: 'private' as const, made_for_kids: false,
  batch_delay_seconds: 30, batch_concurrency: 1, max_attempts: 4, max_lateness_minutes: 360,
}

function status(over: Partial<AutomationStatus> = {}): AutomationStatus {
  return {
    configured: true, config_error: null, webhook_url_set: true, reachable: true, unreachable_reason: null,
    settings: SETTINGS, task_counts: {}, events: { pending: 0, delivered: 3, dead: 0 }, ...over,
  }
}

const TASK = (over: Partial<AutomationTask>): AutomationTask => ({
  id: 'task_1', kind: 'publish', status: 'FAILED', label: 'Failed', title: 'A clip', privacy: 'private',
  clip: { job_id: 'j', clip_file: 'clip_01.mp4', clip_id: 'm01', source_id: 's' }, run_at: null, batch_id: null,
  attempts: 4, max_attempts: 4, next_attempt_at: null, error: 'Gave up after 6 retries', video_url: null,
  approved: true, has_metadata: true, history: [], created: 0, updated: 0, ...over,
})

const CLIPS = [
  { job_id: 'j', clip_file: 'clip_01.mp4', clip_id: 'm01', source_id: 's' },
  { job_id: 'j', clip_file: 'clip_02.mp4', clip_id: 'm02', source_id: 's' },
]

beforeEach(() => {
  calls = []
})

describe('AutomationPanel', () => {
  it('stays a short hint when n8n is not set up', async () => {
    mockFetch({ 'GET /api/automation/status': () => status({ configured: false }) })
    render(<AutomationPanel clips={CLIPS} approvedIds={[]} channelId="UC1" privacy="private" madeForKids={false} />)
    expect(await screen.findByText(/SNIPMINT_N8N_WEBHOOK_SECRET/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /hand/i })).toBeNull()
  })

  it('shows meaningful statuses, offline n8n and failures, with retry', async () => {
    mockFetch({
      'GET /api/automation/status': () => status({ reachable: false, unreachable_reason: 'n8n is not reachable (ConnectError).' }),
      'GET /api/automation/tasks': () => [
        TASK({}),
        TASK({ id: 'task_2', status: 'AWAITING_APPROVAL', label: 'Waiting for approval', error: null, attempts: 0 }),
        TASK({ id: 'task_3', status: 'SCHEDULED', label: 'Scheduled', error: null, attempts: 0, run_at: '2030-01-01T10:00:00Z' }),
        TASK({ id: 'task_4', status: 'PUBLISHED', label: 'Published', error: null, video_url: 'https://www.youtube.com/shorts/x' }),
      ],
      'GET /api/automation/executions': () => [
        { id: 'e1', workflow: 'batch-publishing', status: 'error', task_id: null, batch_id: 'b', message: 'Snipmint unreachable', started: 0, updated: 0 },
      ],
      'POST /api/automation/tasks/task_1/retry': () => TASK({ status: 'UPLOADING' }),
    })
    render(<AutomationPanel clips={CLIPS} approvedIds={['m01']} channelId="UC1" privacy="private" madeForKids={false} />)
    expect(await screen.findByText('n8n offline')).toBeInTheDocument()
    expect(await screen.findByText('Waiting for approval')).toBeInTheDocument()
    expect(screen.getByText('Scheduled', { selector: '.badge' })).toBeInTheDocument()
    expect(screen.getByText('Published')).toBeInTheDocument()
    expect(screen.getByText('Gave up after 6 retries')).toBeInTheDocument()
    expect(screen.getByText('Snipmint unreachable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(calls.some((c) => c.path === '/api/automation/tasks/task_1/retry')).toBe(true))
  })

  it('hands picked clips over as a batch with delay and concurrency', async () => {
    mockFetch({
      'GET /api/automation/status': () => status(),
      'GET /api/automation/tasks': () => [],
      'GET /api/automation/executions': () => [],
      'POST /api/automation/tasks': () => ({ batch_id: 'b1', tasks: [] }),
    })
    render(<AutomationPanel clips={CLIPS} approvedIds={['m01']} channelId="UC1" privacy="unlisted" madeForKids={false} />)
    expect(await screen.findByText(/2 clip\(s\) picked, 1 approved/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'As a batch' }))
    fireEvent.change(screen.getByLabelText(/seconds between uploads/i), { target: { value: '90' } })
    fireEvent.click(screen.getByRole('button', { name: /hand 2 clips/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/api/automation/tasks')).toBe(true))
    const body = calls.find((c) => c.method === 'POST' && c.path === '/api/automation/tasks')!.body as Record<string, unknown>
    expect(body).toMatchObject({ batch: true, delay_seconds: 90, concurrency: 1, privacy: 'unlisted', channel_id: 'UC1', run_at: null })
    expect(body.clips).toHaveLength(2)
  })

  it('needs automation on before handing clips over', async () => {
    mockFetch({
      'GET /api/automation/status': () => status({ settings: { ...SETTINGS, enabled: false } }),
      'GET /api/automation/tasks': () => [],
      'GET /api/automation/executions': () => [],
    })
    render(<AutomationPanel clips={CLIPS} approvedIds={[]} channelId={null} privacy="private" madeForKids={false} />)
    expect(await screen.findByText('Turn automation on first.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /hand 2 clips/i })).toBeDisabled()
  })
})

describe('approval in the metadata editor', () => {
  it('approves the current metadata', async () => {
    mockFetch({ 'POST /api/metadata/yt-AAAAAAAAAAA/m01/approve': () => ({ ...RECORD, approved: true, approved_by: 'user' }) })
    let got = RECORD
    render(<MetadataEditor record={RECORD} categories={{ '27': 'Education' }} language="en" onRecord={(r) => (got = r)} />)
    expect(screen.getByText('Not approved yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(got.approved).toBe(true))
    expect(calls[0].body).toEqual({ approved: true })
  })
})
