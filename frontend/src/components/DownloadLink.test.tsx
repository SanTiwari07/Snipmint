import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useSession } from '../state/session'
import { DownloadLink, TOKEN_HELP } from './DownloadLink'

describe('DownloadLink', () => {
  let clicks = 0
  beforeEach(() => {
    clicks = 0
    useSession.setState({ toasts: [] })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
      clicks++
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('explains an expired access link instead of saving the error', async () => {
    globalThis.fetch = vi.fn(async () => new Response('{"error":"A valid access token is required."}', { status: 401 })) as typeof fetch
    render(<DownloadLink href="/api/clips/j/files/clip_01.mp4?download=true">Download</DownloadLink>)
    fireEvent.click(screen.getByText('Download'))
    await waitFor(() => expect(useSession.getState().toasts.at(-1)?.message).toBe(TOKEN_HELP))
    expect(clicks).toBe(0)
  })

  it('downloads when the file is there', async () => {
    globalThis.fetch = vi.fn(async () => new Response('x', { status: 206 })) as typeof fetch
    render(<DownloadLink href="/api/clips/j/files/clip_01.mp4?download=true">Download</DownloadLink>)
    fireEvent.click(screen.getByText('Download'))
    await waitFor(() => expect(clicks).toBe(1))
    expect(vi.mocked(globalThis.fetch).mock.calls[0][1]).toEqual({ headers: { Range: 'bytes=0-0' } })
    expect(useSession.getState().toasts).toHaveLength(0)
  })
})
