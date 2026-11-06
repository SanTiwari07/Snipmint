import { parseProjectFile, toProjectFile } from './projectFile'
import type { ClipsDraft } from '../state/clips'

const clips: ClipsDraft = {
  url: 'https://youtu.be/AAAAAAAAAAA',
  sourceId: 'yt-AAAAAAAAAAA',
  taskId: 't',
  band: 'short',
  count: 5,
  selected: ['m02', 'm04'],
  edits: { m02: { start: 10, end: 31 } },
  captionsEnabled: false,
  framing: 'split',
  quality: 'high',
  loudness: false,
  voiceover: { enabled: true, engine: 'edge', voice_id: 'en-GB-RyanNeural' },
  jobId: null,
  jobKey: null,
  pending: null,
}

describe('project files', () => {
  it('round-trips every choice', () => {
    const file = toProjectFile(clips, { captions: { preset: 'karaoke', overrides: { a: 1 }, font: null }, format: 'square' }, 'Talk')
    const back = parseProjectFile(JSON.stringify(file))
    expect(back.source).toEqual({ id: 'yt-AAAAAAAAAAA', url: 'https://youtu.be/AAAAAAAAAAA', title: 'Talk' })
    expect(back.selected).toEqual(['m02', 'm04'])
    expect(back.edits.m02).toEqual({ start: 10, end: 31 })
    expect(back.framing).toBe('split')
    expect(back.export).toEqual({ quality: 'high', loudness: false })
    expect(back.voiceover.voice_id).toBe('en-GB-RyanNeural')
    expect(back.captions_enabled).toBe(false)
    expect(back.format).toBe('square')
    expect(JSON.stringify(file)).not.toMatch(/AIza|gemini.?key/i)
  })

  it('rejects other files and fills safe defaults', () => {
    expect(() => parseProjectFile('nope')).toThrow(/not JSON/)
    expect(() => parseProjectFile('{"script": "hi"}')).toThrow(/not a Snipmint project/)
    const p = parseProjectFile(JSON.stringify({ snipmint_project: 1, source: { id: 'yt-x', url: '' }, framing: 'weird' }))
    expect(p.framing).toBe('auto')
    expect(p.analysis.band).toBe('medium')
    expect(p.export.loudness).toBe(true)
  })
})
