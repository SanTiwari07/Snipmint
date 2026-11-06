import { useEffect } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { ReelRail, StepNav, TopNav, Toasts } from './components/Shell'
import { Banner, Button } from './components/ui'
import { screenFor, useFlowState } from './flowState'
import { useCatalog } from './state/catalog'
import { useClips } from './state/clips'
import { useSession } from './state/session'
import { CaptionsStep } from './steps/CaptionsStep'
import { ExportStep } from './steps/ExportStep'
import { MomentsStep } from './steps/MomentsStep'
import { PublishStep } from './steps/PublishStep'
import { SourceStep } from './steps/SourceStep'
import { StartScreen } from './steps/StartScreen'

function Layout() {
  const { pathname, search } = useLocation()
  const { loaded, error, load } = useCatalog()
  const started = useSession((s) => s.started)
  const start = useSession((s) => s.start)
  // coming back from Google's consent screen is a full page load: carry on where the person was
  const backFromGoogle = pathname === '/publish' && new URLSearchParams(search).has('youtube')
  const sourceId = useClips((s) => s.sourceId)
  const flow = useFlowState()
  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    window.scrollTo({ top: 0 })
  }, [pathname])

  useEffect(() => {
    if (backFromGoogle && sourceId && !started) start()
  }, [backFromGoogle, sourceId, started, start])

  // every visit starts on the start screen: paste a link, or carry on with the last video
  if ((!started && !backFromGoogle) || !sourceId) return <Navigate to="/" replace />

  const screen = screenFor(pathname, flow)
  const rail = !!screen?.rail
  const stepClass = pathname === '/captions' ? 'workspace--captions' : ''

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <TopNav />
      <main id="main" className={rail ? 'page' : 'page page--solo'}>
        {error && (
          <Banner
            tone="error"
            title="Can’t reach Snipmint"
            action={
              <Button variant="secondary" onClick={() => void load()}>
                Try again
              </Button>
            }
          >
            {error} Check that Snipmint is still running on your computer.
          </Banner>
        )}
        {loaded &&
          (rail ? (
            <div className={`workspace ${stepClass}`}>
              <div className="workspace__main">
                <Outlet />
              </div>
              <aside className="workspace__rail" aria-label="Your clip">
                <ReelRail />
              </aside>
            </div>
          ) : (
            <div className="solo">
              <Outlet />
            </div>
          ))}
        {!loaded && !error && <p className="loading">Loading…</p>}
      </main>
      {loaded && <StepNav />}
    </>
  )
}

export default function App() {
  return (
    <>
      <Routes>
        <Route index element={<StartScreen />} />
        <Route element={<Layout />}>
          <Route path="/source" element={<SourceStep />} />
          <Route path="/moments" element={<MomentsStep />} />
          <Route path="/captions" element={<CaptionsStep />} />
          <Route path="/export" element={<ExportStep />} />
          <Route path="/publish" element={<PublishStep />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toasts />
    </>
  )
}
