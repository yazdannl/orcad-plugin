import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './style.css'

// OrcaSlicer stamps data-orca-theme (and updates it live); outside the host,
// follow the OS preference instead.
const root = document.documentElement
if (!root.hasAttribute('data-orca-theme')) {
  const query = window.matchMedia?.('(prefers-color-scheme: light)')
  const apply = () => root.setAttribute('data-orca-theme', query?.matches ? 'light' : 'dark')
  apply()
  query?.addEventListener?.('change', apply)
}

createRoot(document.getElementById('app')).render(<StrictMode><App /></StrictMode>)
