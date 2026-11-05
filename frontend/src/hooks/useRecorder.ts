import { useCallback, useEffect, useRef, useState } from 'react'

export type RecorderState = 'idle' | 'asking' | 'recording' | 'done' | 'unsupported'

/** Records the microphone in the browser (MediaRecorder). The server decodes whatever it produces. */
export function useRecorder() {
  const [state, setState] = useState<RecorderState>(() =>
    typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function' && typeof MediaRecorder !== 'undefined'
      ? 'idle'
      : 'unsupported',
  )
  const [blob, setBlob] = useState<Blob | null>(null)
  const [seconds, setSeconds] = useState(0)
  const [level, setLevel] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const rec = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const timer = useRef<number | null>(null)
  const raf = useRef<number | null>(null)
  const ctx = useRef<AudioContext | null>(null)

  const cleanup = useCallback(() => {
    if (timer.current) window.clearInterval(timer.current)
    if (raf.current) cancelAnimationFrame(raf.current)
    stream.current?.getTracks().forEach((t) => t.stop())
    void ctx.current?.close().catch(() => {})
    timer.current = raf.current = null
    stream.current = null
    ctx.current = null
    setLevel(0)
  }, [])

  useEffect(() => cleanup, [cleanup])

  const start = useCallback(async () => {
    setError(null)
    setBlob(null)
    setSeconds(0)
    setState('asking')
    try {
      // Raw audio: noise suppression and auto gain change how the voice sounds, and the clone copies that.
      const media = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      })
      stream.current = media
      const chunks: BlobPart[] = []
      const recorder = new MediaRecorder(media)
      rec.current = recorder
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data)
      recorder.onstop = () => {
        setBlob(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
        setState('done')
        cleanup()
      }
      const audioCtx = new AudioContext()
      ctx.current = audioCtx
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 512
      audioCtx.createMediaStreamSource(media).connect(analyser)
      const buf = new Uint8Array(analyser.fftSize)
      const tick = () => {
        analyser.getByteTimeDomainData(buf)
        let peak = 0
        for (const v of buf) peak = Math.max(peak, Math.abs(v - 128))
        setLevel(Math.min(1, peak / 100))
        raf.current = requestAnimationFrame(tick)
      }
      tick()
      recorder.start()
      const began = Date.now()
      timer.current = window.setInterval(() => setSeconds(Math.floor((Date.now() - began) / 1000)), 250)
      setState('recording')
    } catch (e) {
      cleanup()
      const denied = e instanceof DOMException && (e.name === 'NotAllowedError' || e.name === 'SecurityError')
      setError(
        denied
          ? 'The browser blocked the microphone. Allow it in the address bar, then try again.'
          : 'No microphone was found. Plug one in, or upload a recording instead.',
      )
      setState('idle')
    }
  }, [cleanup])

  const stop = useCallback(() => {
    if (rec.current && rec.current.state !== 'inactive') rec.current.stop()
  }, [])

  const reset = useCallback(() => {
    setBlob(null)
    setSeconds(0)
    setState((s) => (s === 'unsupported' ? s : 'idle'))
  }, [])

  const load = useCallback((file: Blob) => {
    setBlob(file)
    setState('done')
  }, [])

  return { state, blob, seconds, level, error, start, stop, reset, load }
}
