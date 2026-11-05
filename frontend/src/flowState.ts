import { type FlowState, screenFor } from './flow'
import { useClips } from './state/clips'

export { screenFor }

/** What the flow guards need to know, from the clip store. */
export function useFlowState(): FlowState {
  const ready = useClips((s) => !!s.source?.analysis?.moments.length && s.source.status.stages.analyze === 'done')
  const selected = useClips((s) => s.selected.length)
  return { ready, selected }
}
