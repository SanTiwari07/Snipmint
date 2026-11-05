import { DEFAULT_DRAFT, effectiveStyle, getIn, setIn, toProject, voiceKey } from './project'
import type { StylePreset } from '../api/types'

const preset = {
  id: 'bold-pop',
  colors: { text: '#FFFFFF', highlight: '#FFE600' },
  font: { id: 'montserrat-black', size: 100 },
  layout: { words_per_chunk: 3 },
} as unknown as StylePreset

describe('setIn / getIn', () => {
  it('sets nested paths without mutating', () => {
    const a = { colors: { text: '#fff' } }
    const b = setIn(a, 'colors.highlight', '#f00')
    expect(b).toEqual({ colors: { text: '#fff', highlight: '#f00' } })
    expect(a).toEqual({ colors: { text: '#fff' } })
    expect(getIn(b, 'colors.highlight')).toBe('#f00')
    expect(getIn(b, 'nope.deeper')).toBeUndefined()
  })
})

describe('effectiveStyle', () => {
  it('folds overrides into the preset', () => {
    const eff = effectiveStyle(preset, { colors: { highlight: '#FF0000' }, layout: { words_per_chunk: 2 } })!
    expect(eff.colors.highlight).toBe('#FF0000')
    expect(eff.colors.text).toBe('#FFFFFF')
    expect(eff.layout.words_per_chunk).toBe(2)
    expect(effectiveStyle(undefined, {})).toBeUndefined()
  })
})

describe('toProject', () => {
  it('maps the draft to the API body', () => {
    const p = toProject({
      ...DEFAULT_DRAFT,
      script: 'Hello there.',
      targetSeconds: 30,
      durationOption: 'speed_only',
      energy: 0.8,
      captions: { preset: 'neon', overrides: { colors: { text: '#000000' } }, font: 'anton-regular' },
    })
    expect(p.script).toBe('Hello there.')
    expect(p.duration).toEqual({ target_seconds: 30, option: 'speed_only' })
    expect(p.voice).toMatchObject({ engine: 'edge', energy: 0.8 })
    expect(p.captions).toMatchObject({ preset: 'neon', font: 'anton-regular' })
  })
  it('omits the duration option when there is no target', () => {
    const p = toProject({ ...DEFAULT_DRAFT, script: 'x', durationOption: 'speed_only' })
    expect(p.duration).toEqual({})
  })
})

describe('voiceKey', () => {
  it('ignores caption changes but not voice changes', () => {
    const a = { ...DEFAULT_DRAFT, script: 'Hi.' }
    expect(voiceKey({ ...a, captions: { ...a.captions, preset: 'neon' } })).toBe(voiceKey(a))
    expect(voiceKey({ ...a, energy: 0.9 })).not.toBe(voiceKey(a))
    expect(voiceKey({ ...a, script: 'Hi!' })).not.toBe(voiceKey(a))
    expect(voiceKey({ ...a, targetSeconds: 30 })).not.toBe(voiceKey(a))
  })
})
