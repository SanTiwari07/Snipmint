/** WCAG relative luminance of a `#RRGGBB` (or `#RRGGBBAA`) colour; null when it cannot be parsed. */
function luminance(hex: string): number | null {
  const m = /^#?([0-9a-f]{6})/i.exec(hex.trim())
  if (!m) return null
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(m[1].slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Contrast ratio between two colours, from 1 (identical) to 21; null when either is not a colour. */
export function contrastRatio(a: string, b: string): number | null {
  const la = luminance(a)
  const lb = luminance(b)
  if (la === null || lb === null) return null
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/** Below this the eye can barely tell two colours apart. */
export const LOW_CONTRAST = 1.5
