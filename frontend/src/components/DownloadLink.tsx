import { type MouseEvent, type ReactNode, useState } from 'react'
import { useSession } from '../state/session'

export const TOKEN_HELP =
  'Snipmint was restarted with a new access link. Run `docker compose logs snipmint`, open the link it prints, and try again.'

/** Why a download can't happen, in words the person can act on. */
async function problem(res: Response): Promise<string> {
  if (res.status === 401) return TOKEN_HELP
  if (res.status === 404) return 'This file is no longer on the server. Make the clips again.'
  try {
    const body = await res.json()
    if (body?.error) return String(body.error)
  } catch {
    /* not JSON */
  }
  return `The download failed (${res.status}).`
}

/**
 * A download button that checks the file is really there before saving it. A plain
 * `<a download>` would quietly save the server's error message (a .json) instead of the video.
 */
export function DownloadLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  const toast = useSession((s) => s.toast)
  const [checking, setChecking] = useState(false)

  const onClick = async (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault()
    if (checking) return
    setChecking(true)
    try {
      // one byte is enough to know the file exists and the access link is still valid
      const res = await fetch(href, { headers: { Range: 'bytes=0-0' } })
      if (!res.ok) {
        toast('error', await problem(res))
        return
      }
      void res.body?.cancel()
      const a = document.createElement('a')
      a.href = href
      a.download = ''
      document.body.appendChild(a)
      a.click()
      a.remove()
    } catch {
      toast('error', 'Could not reach the Snipmint server. Check that it is still running.')
    } finally {
      setChecking(false)
    }
  }

  return (
    <a href={href} download className={className} onClick={onClick} aria-busy={checking || undefined}>
      {children}
    </a>
  )
}
