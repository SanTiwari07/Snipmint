import { formatDuration, slugFirstWords, speedQuality } from './time'

describe('formatDuration', () => {
  it('formats minutes and seconds', () => {
    expect(formatDuration(308.7)).toBe('5:09')
    expect(formatDuration(59.4)).toBe('0:59')
  })
  it('formats hours', () => {
    expect(formatDuration(3723)).toBe('1:02:03')
  })
  it('handles missing values', () => {
    expect(formatDuration(null)).toBe('–')
    expect(formatDuration(Number.NaN)).toBe('–')
  })
})

describe('speedQuality', () => {
  it('matches the server zones', () => {
    expect(speedQuality(1)).toBe('natural')
    expect(speedQuality(0.85)).toBe('noticeable')
    expect(speedQuality(1.31)).toBe('strong')
    expect(speedQuality(1.8)).toBe('not_recommended')
  })
})

describe('slugFirstWords', () => {
  it('keeps the first three words', () => {
    expect(slugFirstWords('Wake up early! Grind hard.')).toBe('wake-up-early')
    expect(slugFirstWords('???')).toBe('video')
    expect(slugFirstWords('नमस्ते दुनिया')).toContain('दुनिया')
  })
})
