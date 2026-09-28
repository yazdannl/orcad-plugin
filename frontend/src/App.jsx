import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { createBridge } from './bridge.js'
import { aiReducer, initialAIState } from './ai.js'
import { OBJECTS, QUALITY, groups, isDisabled, restoreParams, searchObjects, setParam, defaults } from './catalog.js'
import { loadSettings, saveSettings } from './storage.js'
import { decodeMesh, meshStats } from './mesh.js'
import { DEFAULT_EXAMPLE, EXAMPLES } from './examples.js'
import { copyText } from './clipboard.js'
import * as fmt from './format.js'
import { Viewport } from './components/Viewport.jsx'
import { ParamField } from './components/ParamField.jsx'
import { CodeEditor } from './components/CodeEditor.jsx'
import { AiPanel } from './components/AiPanel.jsx'
import { Icon, Logo } from './components/Icons.jsx'

const bridge = createBridge()
const QUALITY_LABELS = { draft: 'Draft', balanced: 'Balanced', final: 'Fine' }
const QUALITY_HINTS = {
  draft: 'Coarse curves, fastest renders',
  balanced: 'Good curves for most prints',
  final: 'Smoothest curves, larger files',
}
const PREVIEW_DELAY = 250
const AUTO_RENDER_DELAY = 900
const VIEW_BUTTONS = [['iso', 'Isometric'], ['front', 'Front'], ['right', 'Right'], ['top', 'Top']]

function exportName(key, params) {
  const label = OBJECTS[key].label
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
        : state === 'connecting' ? ['busy', 'Connecting…'] : ['busy', `Setting up OpenSCAD${pct}`]
  const title = state === 'failed' ? engine.error : state === 'offline'
    ? 'Open this page from the orcad tab in OrcaSlicer to render models.' : text
  return (
    <button type="button" className={`pill pill-${tone}`} title={title} onClick={onDetails}>
      <span className="pill-dot" />
      <span className="pill-text">{text}</span>
    </button>
  )
}

export default function App() {
  const saved = useMemo(() => loadSettings(), [])
  const [mode, setMode] = useState(saved.mode === 'code' ? 'code' : 'library')
  const [objectKey, setObjectKey] = useState(OBJECTS[saved.objectKey] ? saved.objectKey : 'gridfinity_bin')
  const [paramsByObject, setParamsByObject] = useState(() => Object.fromEntries(
    Object.keys(OBJECTS).map((key) => [key, restoreParams(key, saved.params?.[key])])))
  const [query, setQuery] = useState('')
  const [quality, setQuality] = useState(QUALITY.includes(saved.quality) ? saved.quality : 'balanced')
  const [format, setFormat] = useState(saved.format === '3mf' ? '3mf' : 'stl')
  const [code, setCode] = useState(typeof saved.code === 'string' && saved.code.trim() ? saved.code : EXAMPLES[DEFAULT_EXAMPLE].code)
  const [aiState, dispatchAI] = useReducer(aiReducer, initialAIState)
  const [aiOpen, setAiOpen] = useState(saved.aiOpen !== false)
  const [aiConfigChoice, setAiConfigChoice] = useState(saved.aiConfig || null)
  const [autoRender, setAutoRender] = useState(saved.autoRender === true)
  const [pendingExample, setPendingExample] = useState(null)
  const [view, setView] = useState({ wireframe: false, edges: saved.edges !== false, grid: saved.grid !== false })
  const [preview, setPreview] = useState(null)
  const [pending, setPending] = useState(null)
  const [ops, setOps] = useState({})
  const [error, setError] = useState(null)
  const [log, setLog] = useState([])
  const [exports, setExports] = useState([])
  const [engine, setEngine] = useState({ state: bridge.available ? 'connecting' : 'offline' })
  const [toasts, setToasts] = useState([])
  const viewport = useRef(null)
  const nextId = useRef(1)
  const latestPreview = useRef(0)
  const opsRef = useRef({})

  const object = OBJECTS[objectKey]
  const params = paramsByObject[objectKey]
  const busyOps = Object.values(ops)
  const visibleObjects = searchObjects(query)

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
    saveSettings({ mode, objectKey, params: paramsByObject, quality, format, code, autoRender, edges: view.edges, grid: view.grid, aiOpen, aiConfig: aiConfigChoice })
  }, [mode, objectKey, paramsByObject, quality, format, code, autoRender, view.edges, view.grid, aiOpen, aiConfigChoice])

  // ---- rendering ------------------------------------------------------------
  const source = useCallback(() => (mode === 'library'
    ? { object: objectKey, params, name: exportName(objectKey, params) }
    : { code, name: 'openscad-model' }), [mode, objectKey, params, code])

  const requestPreview = useCallback(() => {
    if (!bridge.available) return
    const id = nextId.current++
    latestPreview.current = id
    setPending({ id, message: 'Rendering…' })
    if (!bridge.send({ type: 'render', id, purpose: 'preview', quality, ...source() })) {
      setPending(null)
      setError({ message: 'Could not reach the plugin. Reopen the orcad tab and try again.', fields: {} })
    }
  }, [quality, source])

  // These effects are keyed on the render inputs on purpose (not on requestPreview's identity).
  useEffect(() => {
    if (mode !== 'library') return undefined
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
    const id = nextId.current++
    opsRef.current[id] = { purpose, message: purpose === 'plate' ? 'Sending to plate…' : 'Exporting…' }
    setOps({ ...opsRef.current })
    if (!bridge.send({ type: 'render', id, purpose, quality, format, ...source() })) {
      delete opsRef.current[id]
      setOps({ ...opsRef.current })
      notify('error', 'Open this page from the orcad tab in OrcaSlicer to export models.')
    }
  }

  // ---- editing --------------------------------------------------------------
  const changeParam = (name, value) => {
    setParamsByObject((all) => ({ ...all, [objectKey]: setParam(objectKey, all[objectKey], name, value) }))
  }
  const resetParams = () => setParamsByObject((all) => ({ ...all, [objectKey]: defaults(objectKey) }))
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
  const copyPath = async (file) => {
    const copied = await copyText(file)
    notify(copied ? 'info' : 'error', copied ? 'Path copied to the clipboard.' : 'Could not access the clipboard.')
  }

  const renderBlocked = Boolean(error?.fields && Object.keys(error.fields).length) && mode === 'library'
  const exportDisabled = !bridge.available || renderBlocked
  const fitKey = mode === 'library' ? objectKey : 'code'
  const stats = preview?.stats
  const paramLabels = Object.fromEntries(object.parameters.map((p) => [p.variable, p.label]))

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Logo />
          <div className="brand-text"><strong>orcad</strong><span>Parametric CAD</span></div>
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
            {QUALITY.map((key) => (
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

      <main className={`workspace workspace-${mode}`}>
        <aside className={`panel sidebar sidebar-${mode}`} aria-label={mode === 'library' ? 'Model library' : 'Code editor'}>
          {mode === 'library' ? (
            <>
              <div className="sidebar-block">
                <label className="search">
                  <Icon name="search" size={16} />
                  <input type="search" placeholder="Search models" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search models" />
                </label>
                <div className="object-grid">
                  {visibleObjects.map((key) => (
                    <button key={key} type="button" className={`object-card${key === objectKey ? ' is-active' : ''}`}
                      aria-pressed={key === objectKey} onClick={() => setObjectKey(key)} title={OBJECTS[key].description}>
                      <span className="object-icon"><Icon name={OBJECTS[key].icon} size={22} /></span>
                      <span className="object-name">{OBJECTS[key].label}</span>
                      <span className="object-cat">{OBJECTS[key].category}</span>
                    </button>
                  ))}
                  {!visibleObjects.length && <p className="muted empty-search">No models match “{query}”.</p>}
                </div>
              </div>
              <div className="sidebar-block params">
                <div className="section-head">
                  <div>
                    <h2>{object.label}</h2>
                    <p className="muted">{object.description}</p>
                  </div>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={resetParams} title="Restore default values">
                    <Icon name="reset" size={15} /><span>Reset</span>
                  </button>
                </div>
                {groups(objectKey).map((group, index) => (
                  <details key={`${objectKey}-${group.name}`} className="param-group" open={index < 3}>
                    <summary><span>{group.name}</span><Icon name="chevron" size={16} /></summary>
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

        <section className="stage" aria-label="Preview">
          <Viewport ref={viewport} mesh={preview?.mesh || null} fitKey={fitKey} wireframe={view.wireframe}
            edges={view.edges} grid={view.grid} dimmed={Boolean(error && preview)} />
          <div className="stage-toolbar glass" role="toolbar" aria-label="View">
            {VIEW_BUTTONS.map(([key, label]) => (
              <button key={key} type="button" className="icon-btn" title={`${label} view`} aria-label={`${label} view`}
                onClick={() => viewport.current?.setView(key)}><Icon name={key} /></button>
            ))}
            <button type="button" className="icon-btn" title="Fit model" aria-label="Fit model" onClick={() => viewport.current?.fit()}>
              <Icon name="fit" />
            </button>
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
                  <p>Open the <b>orcad</b> tab inside OrcaSlicer to render and export models.</p>
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
