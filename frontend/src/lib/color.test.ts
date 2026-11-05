import { describe, expect, it } from 'vitest'
import { LOW_CONTRAST, contrastRatio } from './color'

describe('contrastRatio', () => {
  it('is 21 for black on white and 1 for the same colour', () => {
    expect(contrastRatio('#000000', '#FFFFFF')).toBeCloseTo(21, 0)
    expect(contrastRatio('#336699', '#336699')).toBe(1)
  })
  it('ignores an alpha suffix', () => {
    expect(contrastRatio('#FFFFFF80', '#000000')).toBeCloseTo(21, 0)
  })
  it('flags a near-black box on a near-black background', () => {
    expect(contrastRatio('#1D2A04', '#101010')!).toBeLessThan(LOW_CONTRAST)
    expect(contrastRatio('#FFE600', '#101010')!).toBeGreaterThan(LOW_CONTRAST)
  })
  it('returns null for something that is not a colour', () => {
    expect(contrastRatio('red', '#000000')).toBeNull()
  })
})
