import '@fontsource-variable/inter'
import '@fontsource/jetbrains-mono/400.css'
import './theme/tokens.css'
import './theme/global.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { getToken } from './api/client'

getToken() // pick up ?token= from the URL before anything fetches

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
