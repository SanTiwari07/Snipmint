import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AnalyzeResponse, PlanResponse, Project } from '../api/types'

export function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), ms)
    return () => window.clearTimeout(t)
  }, [value, ms])
  return debounced
}

interface Fetched<T> {
  data: T | null
  loading: boolean
  error: string | null
}

/** Run an abortable request whenever `key` changes. Keeps the previous data while loading. */
function useRequest<T>(key: string, run: ((signal: AbortSignal) => Promise<T>) | null): Fetched<T> {
  const [state, setState] = useState<Fetched<T>>({ data: null, loading: false, error: null })
  const runRef = useRef(run)
  runRef.current = run
  useEffect(() => {
    const fn = runRef.current
    if (!fn) {
      setState({ data: null, loading: false, error: null })
      return
    }
    const ctl = new AbortController()
    setState((s) => ({ ...s, loading: true }))
    fn(ctl.signal)
      .then((data) => setState({ data, loading: false, error: null }))
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return
        setState((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e) }))
      })
    return () => ctl.abort()
  }, [key])
  return state
}

export function useAnalysis(script: string, voiceRef: string): Fetched<AnalyzeResponse> {
  const text = useDebounced(script, 300)
  return useRequest(`${text}\u0000${voiceRef}`, text.trim() ? (s) => api.analyze(text, voiceRef, s) : null)
}

export function usePlan(
  script: string,
  voiceRef: string,
  targetSeconds: number | null,
): Fetched<PlanResponse> {
  const text = useDebounced(script, 300)
  return useRequest(
    `${text}\u0000${voiceRef}\u0000${targetSeconds}`,
    text.trim()
      ? (s) => api.plan({ text, voice: voiceRef, target_seconds: targetSeconds }, s)
      : null,
  )
}

/** A rendered caption frame as an object URL; the previous frame stays until the next arrives. */
export function usePreviewUrl(project: Project | null, enabled: boolean) {
  const key = useDebounced(project && enabled ? JSON.stringify(project) : '', 350)
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  useEffect(() => {
    if (!key) {
      setLoading(false)
      return
    }
    const ctl = new AbortController()
    setLoading(true)
    api
      .framePreview(JSON.parse(key) as Project, ctl.signal)
      .then((blob) => {
        setUrl((old) => {
          if (old) URL.revokeObjectURL(old)
          return URL.createObjectURL(blob)
        })
        setError(null)
      })
      .catch((e: unknown) => {
        if (!ctl.signal.aborted) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false)
      })
    return () => ctl.abort()
  }, [key])
  useEffect(() => () => setUrl((old) => (old ? (URL.revokeObjectURL(old), null) : old)), [])
  return { url, error, loading }
}
