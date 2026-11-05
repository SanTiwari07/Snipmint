import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from './App'
import type { SourceState } from './api/clips'
import { screenFor } from './flow'
import { useCatalog } from './state/catalog'
import { useClips } from './state/clips'
import { DEFAULT_DRAFT, useDraft } from './state/project'
import { useSession } from './state/session'

const SOURCE: SourceState = {
  source_id: 'yt-AAAAAAAAAAA',
  meta: {
    id: 'yt-AAAAAAAAAAA',
    url: 'https://www.youtube.com/watch?v=AAAAAAAAAAA',
    video_id: 'AAAAAAAAAAA',
    title: 'A long podcast',
    channel: 'Someone',
    duration: 1800,
    width: 1920,
    height: 1080,
    thumbnail: null,
    chapters: [],
    heatmap: [],
    language: 'en',
  },
  vertical: false,
  status: {
    stages: { probe: 'done', download: 'done', audio: 'done', transcribe: 'done', signals: 'done', analyze: 'done' },
    error: null,
    task_id: 'source-1',
    warnings: [],
  },
  has_video: true,
  has_transcript: true,
  analysis: {
    provider: 'heuristic',
    requested: 'heuristic',
    fallback_reason: null,
    options: { provider: 'heuristic', min_seconds: 30, max_seconds: 60, count: null },
    cost_usd: null,
    moments: [1, 2, 3, 4].map((i) => ({
      id: `m0${i}`,
      start: i * 100,
      end: i * 100 + 40,
      score: 100 - i,
      why: 'Opens with a strong line',
      title: `Moment number ${i}`,
      hook_text: 'Why do you never see it?',
      descriptions: {},
      keywords: ['never'],
      provider: 'heuristic' as const,
      fallback: false,
      layout: null,
      text: 'Why do you never see it? Because nobody tells you.',
    })),
  },
  task: null,
}

let posted: { path: string; body: unknown }[] = []

function mockApi() {
  posted = []
  const routes: Record<string, unknown> = {
    '/api/health': { version: '0', ffmpeg: true, libass: true, align_available: false, auth_required: false, demo: false, transcribe_available: true, ai: { gemini_installed: true, gemini_server_key: false, local_llm: false } },
    '/api/presets/formats': [
      { id: 'reels', name: 'Reels / TikTok / Shorts', width: 1080, height: 1920, fps: 30, positions: {}, max_text_width: 0.84, safe_zone: {}, default_style: 'bold-pop', note: '' },
    ],
    '/api/presets/styles': [],
    '/api/fonts': [],
    '/api/engines': [],
    '/api/voices': [],
    '/api/sources': { source_id: 'yt-AAAAAAAAAAA', task_id: 'source-1' },
    '/api/sources/yt-AAAAAAAAAAA': SOURCE,
  }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).split('?')[0]
    if (init?.method === 'POST') posted.push({ path, body: init.body ? JSON.parse(String(init.body)) : null })
    if (path === '/api/clips/preview') return new Response(new Blob(['x']), { status: 200 })
    if (!(path in routes)) return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
    return new Response(JSON.stringify(routes[path]), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
}

class FakeEventSource {
  addEventListener() {}
  close() {}
  onerror: (() => void) | null = null
}

beforeEach(() => {
  mockApi()
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  useDraft.setState({ ...DEFAULT_DRAFT })
  useCatalog.setState({ loaded: false, error: null })
  useClips.getState().reset()
  useSession.setState({ started: false })
})

describe('App', () => {
  it('starts by asking for a YouTube link', async () => {
    render(
      <MemoryRouter initialEntries={['/moments']}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByLabelText(/youtube link/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /find clips/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/paste a youtube link/i)
    expect(posted).toHaveLength(0)
  })

  it('sends the link with the free analysis and opens the video step', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    )
    fireEvent.change(await screen.findByLabelText(/youtube link/i), {
      target: { value: 'https://youtu.be/AAAAAAAAAAA' },
    })
    fireEvent.click(screen.getByRole('button', { name: /find clips/i }))
    await waitFor(() => expect(posted.some((p) => p.path === '/api/sources')).toBe(true))
    const body = posted.find((p) => p.path === '/api/sources')!.body as { analysis: { provider: string } }
    expect(body.analysis.provider).toBe('heuristic')
    expect(await screen.findByRole('heading', { name: /your video is ready/i })).toBeInTheDocument()
  })

  it('lists the moments with the best three picked', async () => {
    useClips.setState({ sourceId: 'yt-AAAAAAAAAAA', url: 'https://youtu.be/AAAAAAAAAAA' })
    useClips.getState().setSource(SOURCE)
    useSession.setState({ started: true })
    render(
      <MemoryRouter initialEntries={['/moments']}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Moment number 1')).toBeInTheDocument()
    expect(screen.getAllByRole('checkbox', { checked: true })).toHaveLength(3)
    fireEvent.click(screen.getByRole('checkbox', { name: /moment number 4/i }))
    expect(useClips.getState().selected).toContain('m04')
  })
})

describe('projects', () => {
  it('opens a saved project on the make-clips step with its picks', async () => {
    const project = {
      snipmint_project: 1,
      source: { id: 'yt-AAAAAAAAAAA', url: 'https://youtu.be/AAAAAAAAAAA', title: 'A long podcast' },
      analysis: { band: 'medium', count: null },
      selected: ['m02', 'm04'],
      edits: { m04: { start: 401, end: 438 } },
      captions: { preset: 'karaoke', overrides: {}, font: null },
      captions_enabled: true,
      format: 'reels',
      framing: 'track',
      export: { quality: 'fast', loudness: true },
      voiceover: { enabled: false, engine: 'edge', voice_id: 'en-US-AvaNeural' },
    }
    const { container } = render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    )
    await screen.findByLabelText(/youtube link/i)
    const input = container.querySelector('input[accept=".json,application/json"]') as HTMLInputElement
    const file = new File([JSON.stringify(project)], 'talk.snipmint.json', { type: 'application/json' })
    fireEvent.change(input, { target: { files: [file] } })
    expect(await screen.findByRole('heading', { name: /ready to make your clips/i })).toBeInTheDocument()
    const st = useClips.getState()
    expect(st.selected).toEqual(['m02', 'm04'])
    expect(st.edits.m04).toEqual({ start: 401, end: 438 })
    expect(st.framing).toBe('track')
    expect(useDraft.getState().captions.preset).toBe('karaoke')
    expect(posted.some((p) => p.path === '/api/sources')).toBe(false) // the video was already prepared
  })
})

describe('flow guards', () => {
  it('needs a ready video and at least one moment', () => {
    expect(screenFor('/source', { ready: false, selected: 0 })?.next?.blocked).toBeTruthy()
    expect(screenFor('/source', { ready: true, selected: 0 })?.next?.blocked).toBeFalsy()
    expect(screenFor('/moments', { ready: true, selected: 0 })?.next?.blocked).toBeTruthy()
    expect(screenFor('/moments', { ready: true, selected: 2 })?.next?.blocked).toBeFalsy()
    expect(screenFor('/nope', { ready: true, selected: 2 })).toBeNull()
  })
})
