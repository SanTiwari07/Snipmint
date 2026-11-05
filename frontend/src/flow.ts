/** The four things a person decides, in the order they decide them. */
export const STEPS = [
  { path: '/source', label: 'Video' },
  { path: '/moments', label: 'Moments' },
  { path: '/captions', label: 'Captions' },
  { path: '/export', label: 'Make clips' },
] as const

export interface FlowState {
  /** The source has been analysed and has moments to pick from. */
  ready: boolean
  /** How many moments are picked. */
  selected: number
}

export interface Screen {
  /** Index into STEPS: which step the stepper shows as current. */
  step: number
  back: string | null
  /** No `next` means the screen draws its own bar. */
  next?: { to: string; label: string; blocked?: string | null }
  /** Screens where the caption preview matters keep it beside the controls. */
  rail?: boolean
}

/** One question per screen. Back and Next depend on what was answered before. */
export function screenFor(pathname: string, f: FlowState): Screen | null {
  switch (pathname) {
    case '/source':
      return {
        step: 0,
        back: '/',
        next: { to: '/moments', label: 'Next: pick the moments', blocked: f.ready ? null : 'Wait until the video is ready.' },
      }
    case '/moments':
      return {
        step: 1,
        back: '/source',
        next: { to: '/captions', label: 'Next: style the captions', blocked: f.selected ? null : 'Pick at least one moment.' },
        rail: true,
      }
    case '/captions':
      return { step: 2, back: '/moments', next: { to: '/export', label: 'Next: make the clips' }, rail: true }
    case '/export':
      return { step: 3, back: '/captions', rail: true }
    default:
      return null
  }
}
