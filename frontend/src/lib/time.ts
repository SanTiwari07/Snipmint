/** 308.7 -> "5:09"; 3723 -> "1:02:03" */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '–'
  const total = Math.round(Math.max(0, seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`
}

export const QUICK_TARGETS: { label: string; seconds: number }[] = [
  { label: '15 s', seconds: 15 },
  { label: '30 s', seconds: 30 },
  { label: '60 s', seconds: 60 },
  { label: '90 s', seconds: 90 },
  { label: '3 min', seconds: 180 },
  { label: '5 min', seconds: 300 },
  { label: '10 min', seconds: 600 },
]

export const QUICK_SPEEDS = [0.75, 1, 1.25, 1.5, 2]

export function slugFirstWords(text: string, count = 3): string {
  const words = text.toLowerCase().match(/[\p{L}\p{M}\p{N}]+/gu) ?? []
  return words.slice(0, count).join('-') || 'video'
}

export type SpeedQuality = 'natural' | 'noticeable' | 'strong' | 'not_recommended'

/** Same zones as the server (script/duration.py): used to badge the manual speed slider. */
export function speedQuality(speed: number): SpeedQuality {
  if (speed >= 0.9 && speed <= 1.15) return 'natural'
  if ((speed >= 0.8 && speed < 0.9) || (speed > 1.15 && speed <= 1.3)) return 'noticeable'
  if ((speed >= 0.7 && speed < 0.8) || (speed > 1.3 && speed <= 1.5)) return 'strong'
  return 'not_recommended'
}
