import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { bridge } from './bridge.js'
import { aiReducer, initialAIState } from './ai.js'
import { activePreset, applyPreset, catalogError, catalogReady, categories, creditFor, defaults, filterGroups, groups, isDisabled, isLoaded, objectKeys, objectMatches, objectMeta, presets, qualityKeys, restoreParams, setParam, setSpec } from './catalog.js'
import { loadSettings, saveSettings } from './storage.js'
import { rememberKey, savedKeys, toggleKey } from './library.js'
import { decodeConfig, encodeConfig } from './share.js'
import { savedCameras } from './camera.js'
import { SIDEBAR_MAX, SIDEBAR_MIN, clampSidebarWidth } from './sidebar.js'
import { decodeMesh, meshStats } from './mesh.js'
import { DEFAULT_EXAMPLE, EXAMPLES } from './examples.js'
import { copyText } from './clipboard.js'
import * as fmt from './format.js'
import { viewportRuntime } from './viewport.js'
import { Thumb } from './components/Thumb.jsx'
import { Viewport } from './components/Viewport.jsx'
import { ParamField } from './components/ParamField.jsx'
import { CodeEditor } from './components/CodeEditor.jsx'
import { AiPanel } from './components/AiPanel.jsx'
import { Icon, Logo } from './components/Icons.jsx'

const QUALITY_LABELS = { draft: 'Draft', balanced: 'Balanced', final: 'Fine' }
const QUALITY_HINTS = {
  draft: 'Coarse curves, fastest renders',
  balanced: 'Good curves for most prints',
  final: 'Smoothest curves, larger files',
}
const PREVIEW_DELAY = 250

// The library is empty when the page runs outside OrcaSlicer, or when the
// plugin's backend could not be unpacked. In the second case the plugin says
// why, and that is far more useful than "unavailable".
function missingLibrary() {
  const reason = catalogError()
  return reason
    ? `The model library is unavailable: ${reason}`
    : 'The model library is unavailable. Open the OrcaCAD tab inside OrcaSlicer.'
}
const ALL = 'all'
const FAVORITES = 'favorites'
const RECENT = 'recent'
const AUTO_RENDER_DELAY = 900
const VIEW_BUTTONS = [['iso', 'Isometric'], ['front', 'Front'], ['right', 'Right'], ['top', 'Top']]

function exportName(key, params) {
  const label = objectMeta(key)?.label || key
  if (params.gridx === undefined) return label
  return `${label} ${params.gridx}x${params.gridy}${params.gridz !== undefined ? `x${params.gridz}` : ''}`
}

function fieldErrors(message) {
  return Object.fromEntries((message.fields || []).map((field) => [field, message.error]))
}

function EnginePill({ engine, onDetails }) {
  const { state } = engine
  const pct = typeof engine.progress === 'number' ? ` ${Math.round(engine.progress * 100)}%` : ''
  const [tone, text] = state === 'ready' ? ['ok', engine.version ? `OpenSCAD ${engine.version}` : 'OpenSCAD ready']
    : state === 'failed' ? ['bad', 'OpenSCAD unavailable']
      : state === 'offline' ? ['idle', 'Not connected']
        : state === 'connecting' ? ['busy', 'Connecting…']
          : state === 'idle' ? ['idle', 'OpenSCAD on first render']
            : ['busy', `Setting up OpenSCAD${pct}`]
  const title = state === 'failed' ? engine.error : state === 'offline'
    ? 'Open this page from the OrcaCAD tab in OrcaSlicer to render models.'
    : state === 'idle'
      ? 'OpenSCAD is set up once, when you render your first model. OrcaSlicer then asks whether OrcaCAD may start it: choose Yes once and it will not ask again.'
      : text
  return (
    <button type="button" className={`pill pill-${tone}`} title={title} onClick={onDetails}>
      <span className="pill-dot" />
      <span className="pill-text">{text}</span>
    </button>
  )
}

export default function App() {
  const saved = useMemo(() => loadSettings(), [])
  const savedParams = useRef(saved.params && typeof saved.params === 'object' ? saved.params : {})
  const [mode, setMode] = useState(saved.mode === 'code' ? 'code' : 'library')
  const [sidebarWidth, setSidebarWidth] = useState(() => clampSidebarWidth(saved.sidebarWidth, globalThis.innerWidth))
  const [objectKey, setObjectKey] = useState(() => (objectMeta(saved.objectKey) ? saved.objectKey : objectKeys()[0] || null))
  // Values are materialised per object, when its specification arrives, instead of
  // building defaults for all 350 models while the tab is opening.
  const [paramsByObject, setParamsByObject] = useState({})
  const [catalogTick, setCatalogTick] = useState(0)
  const [query, setQuery] = useState('')
  const [paramQuery, setParamQuery] = useState('')
  const [category, setCategory] = useState(ALL)
  const [favorites, setFavorites] = useState(() => savedKeys(saved.favorites, objectMeta))
  const [recent, setRecent] = useState(() => savedKeys(saved.recent, objectMeta))
  const [shareOpen, setShareOpen] = useState(false)
  const [shareText, setShareText] = useState('')
  const [quality, setQuality] = useState(() => (qualityKeys().includes(saved.quality) ? saved.quality : qualityKeys()[0] || 'balanced'))
  const [format, setFormat] = useState(saved.format === '3mf' ? '3mf' : 'stl')
  const [code, setCode] = useState(typeof saved.code === 'string' && saved.code.trim() ? saved.code : EXAMPLES[DEFAULT_EXAMPLE].code)
  const [aiState, dispatchAI] = useReducer(aiReducer, initialAIState)
  const [aiOpen, setAiOpen] = useState(saved.aiOpen !== false)
  const [aiConfigChoice, setAiConfigChoice] = useState(saved.aiConfig || null)
  const [autoRender, setAutoRender] = useState(saved.autoRender === true)
  const [pendingExample, setPendingExample] = useState(null)
  const [view, setView] = useState({ wireframe: false, edges: saved.edges !== false, grid: saved.grid !== false, measure: false })
  const [cameras, setCameras] = useState(() => savedCameras(saved.cameras))
  const [preview, setPreview] = useState(null)
  const [pending, setPending] = useState(null)
  const [ops, setOps] = useState({})
  const [error, setError] = useState(null)
  const [log, setLog] = useState([])
  const [exports, setExports] = useState([])
  const [engine, setEngine] = useState({ state: bridge.available ? 'connecting' : 'offline' })
  const [toasts, setToasts] = useState([])
  const [attrib, setAttrib] = useState(null) // { key, rect } of the card whose credit popover is open
  const viewport = useRef(null)
  const searchRef = useRef(null)
  const nextId = useRef(1)
  const latestPreview = useRef(0)
  const opsRef = useRef({})

  const object = objectKey ? objectMeta(objectKey) : null
  const params = objectKey ? paramsByObject[objectKey] ?? null : null
  const loaded = Boolean(objectKey && isLoaded(objectKey))
  // The panel waits for the merged values, not just the spec: isLoaded() turns
  // true the moment the reply lands, one render before the effect below has
  // materialised the values.
  const ready = params !== null
  const busyOps = Object.values(ops)
  // Favorites and recents are list modes: they keep catalog order unless the
  // user types a query, and ignore the category chips.
  const listMode = category === FAVORITES || category === RECENT
  const sourceKeys = category === FAVORITES ? favorites : category === RECENT ? recent : objectKeys()
  const visibleObjects = sourceKeys.filter((key) => objectMatches(objectMeta(key), key, query, listMode ? ALL : category))
  const counts = { shown: visibleObjects.length, total: objectKeys().length }
  const objectPresets = presets(objectKey)
  const visibleGroups = filterGroups(groups(objectKey), paramQuery)
  const activeName = params ? activePreset(objectKey, params) : null
  const chips = [ALL, FAVORITES, RECENT, ...categories()]

  // The parameters of the selected model, fetched once per object, and its values
  // seeded from the saved settings - keeping only values still valid for it.
  useEffect(() => {
    if (!objectKey || loaded || !bridge.available) return
    bridge.send({ type: 'spec', id: `spec:${objectKey}`, key: objectKey })
  }, [objectKey, loaded, catalogTick])

  useEffect(() => {
    if (!objectKey || !loaded) return
    setParamsByObject((all) => (all[objectKey] !== undefined ? all
      : { ...all, [objectKey]: restoreParams(objectKey, savedParams.current[objectKey]) }))
  }, [objectKey, loaded, catalogTick])

  // A credit popover closes on Escape, on a click outside itself and on scroll
  // (the icon's viewport rect would go stale), and when the list is filtered.
  useEffect(() => {
    if (!attrib) return undefined
    const close = (event) => {
      if (event.type === 'keydown') { if (event.key === 'Escape') setAttrib(null); return }
      if (event.type === 'scroll') { setAttrib(null); return }
      const node = event.target instanceof Element ? event.target : null
      if (node?.closest('.object-attrib') || node?.closest('.attrib-popover')) return
      setAttrib(null)
    }
    window.addEventListener('keydown', close)
    window.addEventListener('mousedown', close)
    window.addEventListener('scroll', close, true)
    return () => {
      window.removeEventListener('keydown', close)
      window.removeEventListener('mousedown', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [attrib])

  useEffect(() => { setAttrib(null) }, [query, category])
  useEffect(() => { setParamQuery('') }, [objectKey])

  // / focuses the model search, and the arrow keys walk its results without
  // taking focus away, so a card can be opened from the keyboard.
  useEffect(() => {
    const onKey = (event) => {
      const target = event.target
      const typing = target instanceof HTMLElement
        && (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable)
      if (event.key === '/' && !typing && mode === 'library') {
        event.preventDefault()
        searchRef.current?.focus()
        return
      }
      if (target !== searchRef.current) return
      if (event.key === 'Escape') { setQuery(''); return }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        if (!visibleObjects.length) return
        event.preventDefault()
        const current = visibleObjects.indexOf(objectKey)
        const next = event.key === 'ArrowDown'
          ? current < 0 ? 0 : Math.min(current + 1, visibleObjects.length - 1)
          : current < 0 ? visibleObjects.length - 1 : Math.max(current - 1, 0)
        const key = visibleObjects[next]
        setObjectKey(key)
        setRecent((list) => rememberKey(list, key))
        requestAnimationFrame(() => document.querySelector('.object-card.is-active')?.scrollIntoView({ block: 'nearest' }))
      } else if (event.key === 'Enter' && visibleObjects[0]) {
        event.preventDefault()
        setObjectKey(visibleObjects[Math.max(0, visibleObjects.indexOf(objectKey))])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode, visibleObjects, objectKey])

  const notify = useCallback((kind, message) => {
    const id = nextId.current++
    setToasts((list) => [...list.slice(-3), { id, kind, message }])
    setTimeout(() => setToasts((list) => list.filter((toast) => toast.id !== id)), kind === 'error' ? 9000 : 5000)
  }, [])

  // ---- bridge messages --------------------------------------------------
  useEffect(() => bridge.subscribe((msg) => {
    if (msg.type === 'hello' || msg.type === 'engine') {
      setEngine(msg.type === 'hello' ? msg.engine : msg)
    } else if (msg.type === 'progress') {
      if (typeof msg.progress === 'number') setEngine({ state: 'starting', progress: msg.progress })
      if (msg.id === latestPreview.current) setPending((p) => (p?.id === msg.id ? { ...p, message: msg.message, progress: msg.progress } : p))
      else if (opsRef.current[msg.id]) setOps((o) => ({ ...o, [msg.id]: { ...o[msg.id], message: msg.message } }))
    } else if (msg.type === 'result' && msg.purpose === 'preview') {
      if (msg.id !== latestPreview.current) return
      setPending(null)
      setLog(Array.isArray(msg.log) ? msg.log : [])
      if (msg.engine) setEngine({ state: 'ready', version: msg.engine })
      const mesh = msg.ok ? decodeMesh(msg.mesh) : null
      if (mesh) {
        setPreview({ mesh, stats: meshStats(mesh), duration: msg.duration_ms, cached: msg.cached })
        setError(null)
      } else {
        setError({ message: msg.ok ? 'The preview mesh could not be decoded.' : msg.error, fields: fieldErrors(msg), line: msg.line })
        if (String(msg.error_code).startsWith('openscad_')) setEngine({ state: 'failed', error: msg.error })
      }
    } else if (msg.type === 'result') {
      const op = opsRef.current[msg.id]
      if (!op) return
      delete opsRef.current[msg.id]
      setOps({ ...opsRef.current })
      if (!msg.ok) {
        notify('error', `${op.purpose === 'plate' ? 'Send to plate' : 'Export'} failed: ${msg.error}`)
        return
      }
      setExports((list) => [{ ...msg, time: new Date() }, ...list].slice(0, 12))
      if (op.purpose === 'plate') notify(msg.handoff?.ok ? 'success' : 'error', msg.handoff?.message || 'Exported.')
      else notify('success', `Saved ${msg.filename}`)
    } else if (msg.type === 'spec') {
      if (setSpec(msg.key, msg.spec)) setCatalogTick((n) => n + 1)
    } else if (msg.type === 'ai_status') {
      dispatchAI({ type: 'status', status: msg })
    } else if (msg.type === 'ai_event') {
      dispatchAI({ type: 'event', id: msg.id, event: msg })
    } else if (msg.type === 'ai_code') {
      dispatchAI({ type: 'code', id: msg.id, code: msg.code })
      if (typeof msg.code === 'string') setCode(msg.code)
    } else if (msg.type === 'ai_done') {
      dispatchAI({ type: 'done', id: msg.id, ok: msg.ok, error: msg.error, code: msg.code })
      if (typeof msg.code === 'string') setCode(msg.code)
      if (msg.ok) setRenderTick((n) => n + 1)
    } else if (msg.type === 'ai_auth_prompt') {
      dispatchAI({ type: 'auth-prompt', id: msg.id, prompt: msg.prompt })
    } else if (msg.type === 'ai_auth_notice') {
      dispatchAI({ type: 'auth-notice', event: msg.event })
    } else if (msg.type === 'ai_auth_done') {
      dispatchAI({ type: 'auth-done', result: msg })
    } else if (msg.type === 'ai_provider_result') {
      dispatchAI({ type: 'provider-result', result: msg })
    } else if (msg.type === 'ai_provider_detect_result') {
      dispatchAI({ type: 'provider-detect-result', result: msg })
    } else if (msg.type === 'notice') {
      notify(msg.ok ? 'info' : 'error', msg.message)
    } else if (msg.type === 'error') {
      notify('error', msg.message)
    }
  }), [notify])

  useEffect(() => {
    bridge.send({ type: 'hello' })
    bridge.send({ type: 'ai_status' })
  }, [])
  useEffect(() => {
    if (!['connecting', 'idle', 'starting'].includes(engine.state)) return undefined
    const timer = setInterval(() => bridge.send({ type: engine.state === 'connecting' ? 'hello' : 'engine' }), 1500)
    return () => clearInterval(timer)
  }, [engine.state])

  // ---- persistence --------------------------------------------------------
  useEffect(() => {
    saveSettings({ mode, objectKey, params: paramsByObject, quality, format, code, autoRender, edges: view.edges, grid: view.grid, aiOpen, aiConfig: aiConfigChoice, sidebarWidth, favorites, recent, cameras })
  }, [mode, objectKey, paramsByObject, quality, format, code, autoRender, view.edges, view.grid, aiOpen, aiConfigChoice, sidebarWidth, favorites, recent, cameras])

  // ---- resizable sidebar ---------------------------------------------------
  const dragSidebar = useCallback((event) => {
    if (event.button !== undefined && event.button !== 0) return
    event.preventDefault()
    const handle = event.currentTarget
    const startX = event.clientX
    const startWidth = sidebarWidth
    const move = (moveEvent) => setSidebarWidth(clampSidebarWidth(startWidth + moveEvent.clientX - startX, globalThis.innerWidth))
    const stop = () => {
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', stop)
      handle.removeEventListener('pointercancel', stop)
    }
    try {
      handle.setPointerCapture?.(event.pointerId) // keep tracking when the pointer leaves the narrow strip
    } catch {
      // a pointer that is already gone (synthetic events, lost capture) still drags
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', stop)
    handle.addEventListener('pointercancel', stop)
  }, [sidebarWidth])

  const nudgeSidebar = useCallback((event) => {
    const step = event.shiftKey ? 64 : 16
    const next = event.key === 'ArrowLeft' ? sidebarWidth - step
      : event.key === 'ArrowRight' ? sidebarWidth + step
        : event.key === 'Home' ? SIDEBAR_MIN : event.key === 'End' ? SIDEBAR_MAX : null
    if (next === null) return
    event.preventDefault()
    setSidebarWidth(clampSidebarWidth(next, globalThis.innerWidth))
  }, [sidebarWidth])

  // ---- rendering ------------------------------------------------------------
  const source = useCallback(() => (mode === 'library'
    ? (params ? { object: objectKey, params, name: exportName(objectKey, params) } : null)
    : { code, name: 'openscad-model' }), [mode, objectKey, params, code])

  const requestPreview = useCallback(() => {
    const job = source()
    if (!bridge.available || !job) return
    const id = nextId.current++
    latestPreview.current = id
    setPending({ id, message: 'Rendering…' })
    // The 3D viewer is only needed once something is being rendered, so it loads
    // while OpenSCAD runs instead of holding up the page.
    viewportRuntime().catch(() => {})
    if (!bridge.send({ type: 'render', id, purpose: 'preview', quality, ...job })) {
      setPending(null)
      setError({ message: 'Could not reach the plugin. Reopen the OrcaCAD tab and try again.', fields: {} })
    }
  }, [quality, source])

  // These effects are keyed on the render inputs on purpose (not on requestPreview's identity).
  useEffect(() => {
    if (mode !== 'library' || !params) return undefined
    const timer = setTimeout(requestPreview, PREVIEW_DELAY)
    return () => clearTimeout(timer)
  }, [mode, objectKey, params, quality])

  useEffect(() => {
    if (mode === 'code') requestPreview()
  }, [mode, quality])

  useEffect(() => {
    if (mode !== 'code' || !autoRender) return undefined
    const timer = setTimeout(requestPreview, AUTO_RENDER_DELAY)
    return () => clearTimeout(timer)
  }, [code])

  const runOperation = (purpose) => {
    const job = source()
    if (!bridge.available || !job) {
      notify('error', 'Open this page from the OrcaCAD tab in OrcaSlicer to export models.')
      return
    }
    const id = nextId.current++
    opsRef.current[id] = { purpose, message: purpose === 'plate' ? 'Sending to plate…' : 'Exporting…' }
    setOps({ ...opsRef.current })
    if (!bridge.send({ type: 'render', id, purpose, quality, format, ...job })) {
      delete opsRef.current[id]
      setOps({ ...opsRef.current })
      notify('error', 'Open this page from the OrcaCAD tab in OrcaSlicer to export models.')
    }
  }

  // ---- editing --------------------------------------------------------------
  const changeParam = (name, value) => {
    setParamsByObject((all) => ({ ...all, [objectKey]: setParam(objectKey, all[objectKey], name, value) }))
  }
  const usePreset = (preset) => {
    setParamsByObject((all) => ({ ...all, [objectKey]: applyPreset(objectKey, all[objectKey], preset) }))
  }
  const resetParams = () => setParamsByObject((all) => ({ ...all, [objectKey]: defaults(objectKey) }))
  const resetGroup = (name) => {
    const group = groups(objectKey).find((entry) => entry.name === name)
    if (!group) return
    const values = defaults(objectKey)
    setParamsByObject((all) => {
      const next = { ...(all[objectKey] || {}) }
      for (const param of group.params) next[param.variable] = values[param.variable]
      return { ...all, [objectKey]: next }
    })
  }
  const [renderTick, setRenderTick] = useState(0)
  useEffect(() => { if (renderTick) requestPreview() }, [renderTick])
  const loadExample = (key) => {
    setCode(EXAMPLES[key].code)
    setPendingExample(null)
    setRenderTick((n) => n + 1) // render once the new code has landed
  }
  const chooseExample = (key) => {
    if (!key) return
    const untouched = Object.values(EXAMPLES).some((example) => example.code === code)
    if (untouched) loadExample(key)
    else setPendingExample(key)
  }
  const saveSnapshot = () => {
    const data = viewport.current?.snapshot()
    if (!data) {
      notify('error', 'The 3D view is not ready yet.')
      return
    }
    const id = nextId.current++
    opsRef.current[id] = { purpose: 'snapshot', message: 'Saving PNG…' }
    setOps({ ...opsRef.current })
    const name = mode === 'library' ? exportName(objectKey, params) : 'openscad-model'
    if (!bridge.send({ type: 'save_png', id, name, data })) {
      delete opsRef.current[id]
      setOps({ ...opsRef.current })
      notify('error', 'Open this page from the OrcaCAD tab in OrcaSlicer to save a snapshot.')
    }
  }
  const copyPath = async (file) => {
    const copied = await copyText(file)
    notify(copied ? 'info' : 'error', copied ? 'Path copied to the clipboard.' : 'Could not access the clipboard.')
  }
  const copyConfig = async () => {
    if (!params) return
    const copied = await copyText(encodeConfig(objectKey, params))
    notify(copied ? 'success' : 'error', copied
      ? 'Configuration copied. Paste it into OrcaCAD to load these settings.'
      : 'Could not access the clipboard.')
  }
  const loadConfig = () => {
    const config = decodeConfig(shareText)
    if (!config || !objectMeta(config.object)) {
      notify('error', 'That is not a configuration for a model in this version of OrcaCAD.')
      return
    }
    const key = config.object
    // Seed the saved values too: the full specification may only arrive after
    // the selection changes, and the loading effect reads them at that point.
    savedParams.current = { ...savedParams.current, [key]: config.params }
    setObjectKey(key)
    setParamsByObject((all) => (all[key] === undefined ? all : { ...all, [key]: restoreParams(key, config.params) }))
    setRecent((list) => rememberKey(list, key))
    setQuery('')
    setCategory(ALL)
    setShareOpen(false)
    setShareText('')
    notify('success', `Loaded settings for ${objectMeta(key).label}.`)
  }

  const renderBlocked = Boolean(error?.fields && Object.keys(error.fields).length) && mode === 'library'
  const exportDisabled = !bridge.available || renderBlocked || (mode === 'library' && !params)
  const fitKey = mode === 'library' ? objectKey : 'code'
  const stats = preview?.stats
  const paramLabels = Object.fromEntries((object?.parameters || []).map((p) => [p.variable, p.label]))

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Logo />
          <div className="brand-text"><strong>OrcaCAD</strong><span>Parametric CAD</span></div>
        </div>
        <div className="segmented mode-switch" role="tablist" aria-label="Workspace">
          {[['library', 'Library'], ['code', 'Code']].map(([key, label]) => (
            <button key={key} type="button" role="tab" aria-selected={mode === key}
              className={mode === key ? 'is-active' : ''} onClick={() => setMode(key)}>
              <Icon name={key} size={16} /><span>{label}</span>
            </button>
          ))}
        </div>
        <div className="topbar-fill" />
        <EnginePill engine={engine} onDetails={() => engine.error && notify('error', engine.error)} />
        <div className="topbar-actions">
          <div className="segmented quality" role="radiogroup" aria-label="Quality">
            {qualityKeys().map((key) => (
              <button key={key} type="button" role="radio" aria-checked={quality === key} title={QUALITY_HINTS[key]}
                className={quality === key ? 'is-active' : ''} onClick={() => setQuality(key)}>{QUALITY_LABELS[key] || key}</button>
            ))}
          </div>
          <div className="export-group">
            <div className="select-wrap select-compact">
              <select aria-label="Export format" value={format} onChange={(e) => setFormat(e.target.value)}>
                <option value="stl">STL</option>
                <option value="3mf">3MF</option>
              </select>
            </div>
            <button type="button" className="btn" disabled={exportDisabled} onClick={() => runOperation('export')}
              title={`Save a ${format.toUpperCase()} file to the exports folder`}>
              <Icon name="download" /><span>Export</span>
            </button>
          </div>
          <button type="button" className="btn btn-primary" disabled={exportDisabled} onClick={() => runOperation('plate')}
            title="Export an STL and load it onto OrcaSlicer's build plate">
            <Icon name="plate" /><span>Send to plate</span>
          </button>
        </div>
      </header>

      <main className={`workspace workspace-${mode}`} style={mode === 'library' ? { '--side-w': `${sidebarWidth}px` } : undefined}>
        <aside className={`panel sidebar sidebar-${mode}`} aria-label={mode === 'library' ? 'Model library' : 'Code editor'}>
          {mode === 'library' ? (
            <>
              <div className="sidebar-block library">
                <div className="library-head">
                  <label className="search">
                    <Icon name="search" size={16} />
                    <input ref={searchRef} type="search" placeholder="Search models (/)" value={query}
                      onChange={(e) => setQuery(e.target.value)} aria-label="Search models" />
                  </label>
                  <div className="category-chips" role="group" aria-label="Filter models by category">
                    {chips.map((name) => (
                      <button key={name} type="button" className={`chip${category === name ? ' is-active' : ''}`}
                        aria-pressed={category === name} onClick={() => setCategory(name)}>
                        {name === FAVORITES ? '★ Favorites' : name === RECENT ? 'Recent' : name === ALL ? 'All' : name}
                      </button>
                    ))}
                  </div>
                  <p className="result-count muted" role="status">{counts.shown} of {counts.total} models</p>
                </div>
                <div className="object-scroll" role="group" aria-label="Models" tabIndex={0}>
                  <div className="object-list">
                    {visibleObjects.map((key) => {
                      const meta = objectMeta(key)
                      const credit = creditFor(key)
                      return (
                        <div key={key} className="object-cell">
                          <button type="button" className={`object-card${key === objectKey ? ' is-active' : ''}`}
                            aria-pressed={key === objectKey}
                            onClick={() => { setObjectKey(key); setRecent((list) => rememberKey(list, key)); setAttrib(null) }}
                            title={`${meta.label} - ${meta.description}`}>
                            <Thumb name={key} />
                            <span className="object-body">
                              <span className="object-name">{meta.short || meta.label}</span>
                              <span className="object-cat">{meta.category}</span>
                              {meta.description && <span className="object-desc">{meta.description}</span>}
                            </span>
                          </button>
                          <button type="button" className="object-star"
                            aria-pressed={favorites.includes(key)}
                            aria-label={`${favorites.includes(key) ? 'Remove' : 'Add'} ${meta.label} ${favorites.includes(key) ? 'from' : 'to'} favorites`}
                            title={favorites.includes(key) ? 'Remove from favorites' : 'Add to favorites'}
                            onClick={() => setFavorites((list) => toggleKey(list, key))}>
                            <Icon name="star" size={13} />
                          </button>
                          {credit && (
                            <button type="button" className="object-attrib"
                              aria-expanded={attrib?.key === key}
                              aria-label={`Credits for ${meta.label}`}
                              title={`Credit: ${credit.label}${credit.license ? ` (${credit.license})` : ''}`}
                              onClick={(event) => {
                                event.stopPropagation()
                                const rect = event.currentTarget.getBoundingClientRect()
                                setAttrib((open) => (open && open.key === key ? null : { key, rect }))
                              }}>
                              <Icon name="info" size={13} />
                            </button>
                          )}
                        </div>
                      )
                    })}
                    {!visibleObjects.length && (
                      <p className="muted empty-search">{!catalogReady() ? missingLibrary()
                        : category === FAVORITES && !query.trim() ? 'Star a model to keep it here.'
                          : `No models match${query.trim() ? ` “${query.trim()}”` : ` the “${category}” category`}.`}</p>
                    )}
                  </div>
                </div>
              </div>
              <div className="sidebar-block params">
                <div className="section-head">
                  <div>
                    <h2>{object?.label || 'No model selected'}</h2>
                    <p className="muted">{object?.description}</p>
                  </div>
                  <div className="head-actions">
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShareOpen((open) => !open)}
                      aria-expanded={shareOpen} title="Copy or load model settings">
                      <Icon name="copy" size={15} /><span>Share</span>
                    </button>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={resetParams} title="Restore default values"
                      disabled={!ready}>
                      <Icon name="reset" size={15} /><span>Reset</span>
                    </button>
                  </div>
                </div>
                {shareOpen && (
                  <section className="share-block" aria-label="Share configuration">
                    <button type="button" className="btn btn-sm" onClick={copyConfig} disabled={!params}>
                      <Icon name="copy" size={14} /><span>Copy this configuration</span>
                    </button>
                    <label htmlFor="share-input" className="muted">Paste a configuration to load its model and settings</label>
                    <textarea id="share-input" value={shareText} onChange={(event) => setShareText(event.target.value)}
                      placeholder="orcad1:…" rows={3} spellCheck={false} />
                    <div className="share-actions">
                      <button type="button" className="btn btn-primary btn-sm" onClick={loadConfig} disabled={!shareText.trim()}>Load</button>
                      <button type="button" className="btn btn-ghost btn-sm"
                        onClick={() => { setShareOpen(false); setShareText('') }}>Cancel</button>
                    </div>
                  </section>
                )}
                {!ready && (
                  <p className="muted empty-search">{catalogReady()
                    ? (bridge.available ? 'Loading this model\u2019s parameters…' : 'Reconnect from OrcaSlicer to load this model\u2019s parameters.')
                    : missingLibrary()}</p>
                )}
                {ready && (
                  <label className="param-search">
                    <Icon name="search" size={14} />
                    <input type="search" placeholder="Find a parameter" value={paramQuery}
                      onChange={(e) => setParamQuery(e.target.value)} aria-label="Find a parameter" />
                  </label>
                )}
                {ready && !!objectPresets.length && !paramQuery.trim() && (
                  <section className="preset-block" aria-label="Presets">
                    <h3 className="preset-title">Presets</h3>
                    <div className="preset-row">
                      {objectPresets.map((preset) => (
                        <button key={preset.name} type="button" className={`preset${activeName === preset.name ? ' is-active' : ''}`}
                          aria-pressed={activeName === preset.name} onClick={() => usePreset(preset)}
                          title={preset.description || preset.name}
                          aria-label={preset.description ? `${preset.name}: ${preset.description}` : preset.name}>
                          {preset.name}
                        </button>
                      ))}
                    </div>
                  </section>
                )}
                {ready && visibleGroups.map((group, index) => (
                  <details key={`${objectKey}-${group.name}`} className="param-group" open={index < 3 || Boolean(paramQuery.trim())}>
                    <summary>
                      <span>{group.name}</span>
                      <span className="group-head-actions">
                        <button type="button" className="group-reset" title={`Reset ${group.name} to defaults`}
                          onClick={(event) => { event.preventDefault(); event.stopPropagation(); resetGroup(group.name) }}>Reset</button>
                        <Icon name="chevron" size={16} />
                      </span>
                    </summary>
                    <div className="param-list">
                      {group.params.map((param) => (
                        <ParamField key={param.variable} param={param} value={params[param.variable]}
                          disabled={isDisabled(param, params)}
                          disabledHint={`Turn on “${(param.depends_on || []).map((d) => paramLabels[d]).join(', ')}” to use this.`}
                          serverError={error?.fields?.[param.variable]}
                          onChange={(value) => changeParam(param.variable, value)} />
                      ))}
                    </div>
                  </details>
                ))}
                {ready && !visibleGroups.length && (
                  <p className="muted empty-search">No parameters match “{paramQuery.trim()}”.</p>
                )}
              </div>
            </>
          ) : (
            <div className="code-panel">
              <div className="code-toolbar">
                <div className="select-wrap">
                  <select aria-label="Load an example" value="" onChange={(e) => chooseExample(e.target.value)}>
                    <option value="">Load example…</option>
                    {Object.entries(EXAMPLES).map(([key, example]) => <option key={key} value={key}>{example.label}</option>)}
                  </select>
                </div>
                <label className="check" title="Render automatically shortly after you stop typing">
                  <input type="checkbox" checked={autoRender} onChange={(e) => setAutoRender(e.target.checked)} />
                  <span>Auto</span>
                </label>
                <button type="button" className="btn btn-primary btn-sm" onClick={requestPreview} disabled={!bridge.available}
                  title="Render the code (Ctrl+Enter)">
                  <Icon name="play" size={15} /><span>Render</span>
                </button>
              </div>
              {pendingExample && (
                <div className="confirm" role="alert">
                  <span>Replace your code with “{EXAMPLES[pendingExample].label}”?</span>
                  <button type="button" className="btn btn-sm btn-danger" onClick={() => loadExample(pendingExample)}>Replace</button>
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => setPendingExample(null)}>Keep mine</button>
                </div>
              )}
              <CodeEditor value={code} onChange={setCode} onRun={requestPreview} errorLine={error?.line} />
              <div className="ai-toggle-row">
                <p className="code-hint">
                  <kbd>Ctrl</kbd>+<kbd>Enter</kbd> renders. <code>include &lt;src/…&gt;</code> loads the bundled Gridfinity library.
                </p>
                <button type="button" className="btn btn-sm btn-accent" aria-expanded={aiOpen}
                  aria-controls="ai-panel" onClick={() => setAiOpen((open) => !open)}>
                  <Icon name="sparkles" size={15} /><span>{aiOpen ? 'Hide AI' : 'AI'}</span>
                </button>
              </div>
              {aiOpen && <AiPanel state={aiState} dispatch={dispatchAI} code={code} configChoice={aiConfigChoice}
                onConfigChoice={setAiConfigChoice} onCodeChange={setCode} onRender={() => setRenderTick((n) => n + 1)} sendMessage={bridge.send} />}
            </div>
          )}
        </aside>

        {mode === 'library' && (
          <div className="sidebar-resizer" role="separator" aria-orientation="vertical" tabIndex={0}
            aria-label="Resize model list" title="Drag to resize"
            aria-valuenow={sidebarWidth} aria-valuemin={SIDEBAR_MIN} aria-valuemax={SIDEBAR_MAX}
            onPointerDown={dragSidebar} onKeyDown={nudgeSidebar} />
        )}

        <section className="stage" aria-label="Preview">
          {preview?.mesh ? (
            <Viewport ref={viewport} mesh={preview.mesh} fitKey={fitKey} camera={cameras[fitKey]}
              wireframe={view.wireframe} edges={view.edges} grid={view.grid} measure={view.measure}
              onCamera={(pose) => setCameras((all) => ({ ...all, [fitKey]: pose }))}
              dimmed={Boolean(error)} />
          ) : null}
          <div className="stage-toolbar glass" role="toolbar" aria-label="View">
            {VIEW_BUTTONS.map(([key, label]) => (
              <button key={key} type="button" className="icon-btn" title={`${label} view`} aria-label={`${label} view`}
                onClick={() => viewport.current?.setView(key)}><Icon name={key} /></button>
            ))}
            <button type="button" className="icon-btn" title="Fit model" aria-label="Fit model" onClick={() => viewport.current?.fit()}>
              <Icon name="fit" />
            </button>
            <span className="toolbar-sep" />
            <button type="button" className="icon-btn" title="Save a PNG snapshot" aria-label="Save a PNG snapshot"
              onClick={saveSnapshot}><Icon name="camera" /></button>
            <button type="button" className={`icon-btn${view.measure ? ' is-on' : ''}`} title="Measure two points on the model"
              aria-label="Measure two points on the model" aria-pressed={view.measure}
              onClick={() => setView((v) => ({ ...v, measure: !v.measure }))}><Icon name="ruler" /></button>
            <span className="toolbar-sep" />
            {[['wireframe', 'Wireframe'], ['edges', 'Edges'], ['grid', 'Build plate grid']].map(([key, label]) => (
              <button key={key} type="button" className={`icon-btn${view[key] ? ' is-on' : ''}`} title={label}
                aria-label={label} aria-pressed={view[key]} onClick={() => setView((v) => ({ ...v, [key]: !v[key] }))}>
                <Icon name={key} />
              </button>
            ))}
          </div>

          {stats && (
            <div className="stage-stats glass">
              <span><b>{fmt.dimensions(stats.size)}</b></span>
              <span>{fmt.count(stats.triangles)} triangles</span>
              {preview.duration !== undefined && <span>{preview.cached ? 'cached' : fmt.seconds(preview.duration)}</span>}
            </div>
          )}

          {(pending || busyOps.length > 0) && (
            <div className="stage-progress" role="status">
              <div className="progress-bar"><span style={pending?.progress ? { width: `${pending.progress * 100}%` } : undefined}
                className={pending?.progress ? '' : 'is-indeterminate'} /></div>
              <span className="progress-text glass">{pending?.message || busyOps[0]?.message}</span>
            </div>
          )}

          {error && (
            <div className="stage-error glass" role="alert">
              <Icon name="alert" />
              <div>
                <strong>{preview ? 'Render failed, showing the last good model' : 'Render failed'}</strong>
                <p>{error.message}</p>
              </div>
              <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => setError(null)}><Icon name="close" size={16} /></button>
            </div>
          )}

          {!preview && !pending && !error && (
            <div className="stage-empty">
              <div className="empty-art"><Icon name="cube" size={44} /></div>
              {bridge.available ? <p>Adjust a parameter or press Render to build a model.</p> : (
                <>
                  <h3>Not connected to OrcaSlicer</h3>
                  <p>Open the <b>OrcaCAD</b> tab inside OrcaSlicer to render and export models.</p>
                </>
              )}
            </div>
          )}
        </section>

        <aside className="panel output" aria-label="Details">
          <section className="card">
            <h3 className="card-title">Model</h3>
            {stats ? (
              <dl className="stats">
                <div><dt>Width</dt><dd>{fmt.mm(stats.size[0])} mm</dd></div>
                <div><dt>Depth</dt><dd>{fmt.mm(stats.size[1])} mm</dd></div>
                <div><dt>Height</dt><dd>{fmt.mm(stats.size[2])} mm</dd></div>
                <div><dt>Volume</dt><dd>{fmt.volume(stats.volume)}</dd></div>
                <div><dt>Triangles</dt><dd>{fmt.count(stats.triangles)}</dd></div>
                <div><dt>Render</dt><dd>{preview.cached ? 'cached' : fmt.seconds(preview.duration)}</dd></div>
              </dl>
            ) : <p className="muted">No model yet.</p>}
          </section>

          <section className="card">
            <div className="card-head">
              <h3 className="card-title">Exports</h3>
              <button type="button" className="btn btn-ghost btn-sm" disabled={!bridge.available}
                onClick={() => bridge.send({ type: 'open_exports' })}><Icon name="folder" size={15} /><span>Open folder</span></button>
            </div>
            {exports.length ? (
              <ul className="export-list">
                {exports.map((item) => (
                  <li key={item.id}>
                    <span className={`badge badge-${item.format}`}>{item.format.toUpperCase()}</span>
                    <div className="export-meta">
                      <span className="export-name" title={item.file}>{item.filename}</span>
                      <span className="muted">{fmt.bytesText(item.size_bytes)} · {item.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        {item.purpose === 'plate' ? ' · sent to plate' : ''}</span>
                    </div>
                    <button type="button" className="icon-btn" title="Copy file path" aria-label={`Copy path of ${item.filename}`}
                      onClick={() => copyPath(item.file)}><Icon name="copy" size={16} /></button>
                  </li>
                ))}
              </ul>
            ) : <p className="muted">Exports and plate sends appear here. If a model does not show up on the plate, drag the file from the exports folder onto OrcaSlicer.</p>}
          </section>

          <section className="card card-console">
            <h3 className="card-title"><Icon name="terminal" size={15} /> Console</h3>
            {log.length ? (
              <pre className="console">{log.map((line, i) => (
                <span key={i} className={line.startsWith('ERROR') ? 'is-error' : line.startsWith('WARNING') ? 'is-warn' : line.startsWith('ECHO') ? 'is-echo' : ''}>{line}{'\n'}</span>
              ))}</pre>
            ) : <p className="muted">OpenSCAD messages (echo, warnings, errors) show up here.</p>}
          </section>
        </aside>
      </main>

      {attrib && (() => {
        const spec = objectMeta(attrib.key)
        const credit = spec && creditFor(attrib.key)
        if (!credit) return null
        const width = Math.min(280, globalThis.innerWidth - 16)
        const left = Math.max(8, Math.min(attrib.rect.left, globalThis.innerWidth - width - 8))
        const top = Math.min(attrib.rect.bottom + 6, globalThis.innerHeight - 130)
        return (
          <div className="attrib-popover" role="dialog" aria-label={`Credits for ${spec.label}`} style={{ left, top, width }}>
            <p className="attrib-title">{spec.label}</p>
            <p className="attrib-line">{credit.label}{credit.author ? ` · ${credit.author}` : ''}</p>
            {credit.license && <p className="attrib-line">License: {credit.license}</p>}
            {credit.repository && (
              <a className="attrib-link" href={credit.repository} target="_blank" rel="noreferrer noopener">{credit.repository}</a>
            )}
            <button type="button" className="icon-btn attrib-close" aria-label="Close credits" onClick={() => setAttrib(null)}>
              <Icon name="close" size={14} />
            </button>
          </div>
        )
      })()}

      <div className="toasts" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.kind}`}>
            <Icon name={toast.kind === 'error' ? 'alert' : 'check'} size={16} />
            <span>{toast.message}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
