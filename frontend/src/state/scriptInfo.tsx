import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import { useAnalysis, usePlan } from '../hooks/useAsync'
import { useDraft } from './project'

type Info = {
  analysis: ReturnType<typeof useAnalysis>
  plan: ReturnType<typeof usePlan>
  voiceRef: string
}

const Ctx = createContext<Info | null>(null)

/** One analysis + plan request shared by the summary bar and every step. */
export function ScriptInfoProvider({ children }: { children: ReactNode }) {
  const script = useDraft((s) => s.script)
  const voice = useDraft((s) => s.voice)
  const targetSeconds = useDraft((s) => s.targetSeconds)
  const voiceRef = `${voice.engine}:${voice.voice_id}`
  const analysis = useAnalysis(script, voiceRef)
  const plan = usePlan(script, voiceRef, targetSeconds)
  return <Ctx.Provider value={{ analysis, plan, voiceRef }}>{children}</Ctx.Provider>
}

export function useScriptInfo(): Info {
  const v = useContext(Ctx)
  if (!v) throw new Error('useScriptInfo must be used inside ScriptInfoProvider')
  return v
}
