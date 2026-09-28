import { useEffect, useRef, useState } from 'react'
import { Icon } from './Icons.jsx'
const THINKING_LEVELS = ['off', 'low', 'medium', 'high']

function configFrom(status) {
  const config = status?.config || {}
  const models = Array.isArray(status?.models) ? status.models : []
  const provider = config.provider || models[0]?.provider || ''
  return {
    source: config.source === 'key' ? 'key' : 'pi',
    provider,
    model: config.model || models.find((model) => model.provider === provider)?.id || '',
    thinking: config.thinking || 'medium',
  }
}

function MessageText({ children }) {
  const segments = String(children || '').split(/(`[^`]+`)/g)
  return segments.map((part, index) => part.startsWith('`') && part.endsWith('`')
    ? <code key={index}>{part.slice(1, -1)}</code> : part)
}

function Settings({ status, configChoice, onConfigChoice, sendMessage }) {
  const [choice, setChoice] = useState(() => configChoice || configFrom(status))
  const [apiKey, setApiKey] = useState('')
  const [notice, setNotice] = useState('')
  const models = Array.isArray(status?.models) ? status.models : []
  const providers = [...new Set(models.map((model) => model.provider).filter(Boolean))]
  const providerOptions = [...new Set([...providers, ...(choice.provider ? [choice.provider] : [])])]
  const availableModels = models.filter((model) => !choice.provider || model.provider === choice.provider)
  const modelOptions = [...new Map([
    ...availableModels,
    ...(choice.model && !availableModels.some((model) => model.id === choice.model)
      ? [{ id: choice.model, name: choice.model, provider: choice.provider }] : []),
  ].map((model) => [model.id, model])).values()]

  useEffect(() => {
    if (configChoice || !status?.config) return
    const next = configFrom(status)
    setChoice(next)
    onConfigChoice(next)
  }, [configChoice, status?.config, onConfigChoice])

  const updateChoice = (patch) => {
    const next = { ...choice, ...patch }
    setChoice(next)
    onConfigChoice(next)
  }

  const saveConfig = () => {
    const message = { type: 'ai_config', ...choice }
    if (apiKey.trim()) message.api_key = apiKey
    setApiKey('')
    setNotice(sendMessage(message) ? 'Settings sent.' : 'Could not reach the plugin.')
  }

  return (
    <details className="ai-settings">
      <summary><Icon name="settings" size={15} /> Settings</summary>
      <div className="ai-settings-body">
        <label className="ai-field">
          <span>Credential source</span>
          <select value={choice.source} onChange={(event) => updateChoice({ source: event.target.value })}>
            <option value="pi">Use pi login</option>
            <option value="key">Use API key</option>
          </select>
        </label>
        <label className="ai-field">
          <span>Provider</span>
          <select aria-label="AI provider" value={choice.provider} onChange={(event) => {
            const provider = event.target.value
            const model = models.find((item) => item.provider === provider)?.id || ''
            updateChoice({ provider, model })
          }}>
            {!providerOptions.length && <option value="">No providers available</option>}
            {providerOptions.map((provider) => <option key={provider} value={provider}>{provider}</option>)}
          </select>
        </label>
        <label className="ai-field">
          <span>Model</span>
          <select aria-label="AI model" value={choice.model} onChange={(event) => updateChoice({ model: event.target.value })}>
            {!modelOptions.length && <option value="">No models available</option>}
            {modelOptions.map((model) => <option key={model.id} value={model.id}>{model.name || model.id}</option>)}
          </select>
        </label>
        <label className="ai-field">
          <span>Thinking level</span>
          <select value={choice.thinking} onChange={(event) => updateChoice({ thinking: event.target.value })}>
            {THINKING_LEVELS.map((level) => <option key={level} value={level}>{level[0].toUpperCase() + level.slice(1)}</option>)}
          </select>
        </label>
        {choice.source === 'key' && (
          <label className="ai-field">
            <span>API key {status?.config?.has_key ? '(saved)' : '(write-only)'}</span>
            <input type="password" value={apiKey} autoComplete="new-password" aria-label="API key (write-only)"
              placeholder={status?.config?.has_key ? 'Saved key — leave blank to keep' : 'Enter API key'}
              onChange={(event) => setApiKey(event.target.value)} />
            <small>Stored by the plugin; it is never shown or saved in this page.</small>
          </label>
        )}
        <div className="ai-settings-actions">
          <span role="status" aria-live="polite">{notice}</span>
          <button type="button" className="btn btn-sm btn-primary" onClick={saveConfig}>Save settings</button>
        </div>
      </div>
    </details>
  )
}

export function AiPanel({ state, dispatch, code, configChoice, onConfigChoice, onCodeChange, onRender, sendMessage }) {
  const [prompt, setPrompt] = useState('')
  const nextId = useRef(1)
  const transcript = useRef(null)
  const status = state.status
  const busy = Boolean(state.activeId) || Boolean(status?.busy)
  const setupState = status?.state
  const ready = setupState === 'ready'

  useEffect(() => {
    if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight
  }, [state.messages])

  const sendPrompt = () => {
    const text = prompt.trim()
    if (!text || !ready || busy) return
    const id = `orcad-ai-${Date.now()}-${nextId.current++}`
    setPrompt('')
    dispatch({ type: 'prompt', id, text, code })
    if (!sendMessage({ type: 'ai_prompt', id, text, code })) {
      dispatch({ type: 'done', id, ok: false, error: 'Could not reach the plugin.' })
    }
  }

  const startSetup = () => sendMessage({ type: 'ai_setup' })
  const stop = () => {
    if (state.activeId) sendMessage({ type: 'ai_abort', id: state.activeId })
  }
  const newChat = () => {
    sendMessage({ type: 'ai_reset' })
    dispatch({ type: 'new-chat' })
  }
  const revert = () => {
    if (typeof state.beforeCode !== 'string') return
    onCodeChange(state.beforeCode)
    dispatch({ type: 'revert' })
    onRender()
  }

  return (
    <section className="ai-panel" id="ai-panel" aria-label="AI OpenSCAD assistant">
      <div className="ai-panel-head">
        <div className="ai-title"><Icon name="sparkles" size={16} /><h2>AI assistant</h2></div>
        <div className="ai-actions">
          {state.changed && <button type="button" className="btn btn-sm btn-ghost" onClick={revert}>Revert AI changes</button>}
          {busy
            ? <button type="button" className="btn btn-sm btn-danger" onClick={stop} disabled={!state.activeId}>Stop</button>
            : <button type="button" className="btn btn-sm btn-ghost" onClick={newChat}>New chat</button>}
          <Settings status={status} configChoice={configChoice} onConfigChoice={onConfigChoice} sendMessage={sendMessage} />
        </div>
      </div>

      {(setupState === 'missing' || setupState === 'installing' || setupState === 'error' || !status) && (
        <div className={`ai-setup${setupState === 'error' ? ' is-error' : ''}`}>
          <div className="ai-setup-copy">
            <strong>{setupState === 'installing' ? 'Installing pi…' : setupState === 'error' ? 'AI setup failed' : !status ? 'Checking AI setup…' : 'Set up AI coding'}</strong>
            <span>{status?.message || 'Install the pi coding agent to generate and edit OpenSCAD code.'}</span>
          </div>
          {typeof status?.progress === 'number' && <div className="ai-progress" role="progressbar" aria-label="AI installation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(status.progress * 100)}>
            <span style={{ width: `${Math.max(0, Math.min(1, status.progress)) * 100}%` }} />
          </div>}
          <button type="button" className="btn btn-sm btn-primary" disabled={!status || setupState === 'installing'} onClick={startSetup}>
            {setupState === 'installing' ? 'Installing…' : setupState === 'error' ? 'Retry install' : 'Install pi'}
          </button>
        </div>
      )}

      <div className="ai-transcript" ref={transcript} role="log" aria-label="AI conversation" aria-live="off">
        {!state.messages.length && <p className="ai-empty">Describe a model or ask for a change to your OpenSCAD code.</p>}
        {state.messages.map((message, index) => (
          <article key={`${message.role}-${message.id || message.callId || index}`} className={`ai-message ai-message-${message.role}`}>
            {message.role === 'user' && <><strong>You</strong><p><MessageText>{message.text}</MessageText></p></>}
            {message.role === 'assistant' && <>
              <strong>Pi</strong>
              {message.thinking && <details className="ai-thinking"><summary>Thinking</summary><p><MessageText>{message.thinking}</MessageText></p></details>}
              {message.text && <p><MessageText>{message.text}</MessageText></p>}
            </>}
            {message.role === 'tool' && <details className={`ai-tool${message.isError ? ' is-error' : ''}`}>
              <summary><span>{message.isError ? 'Tool failed' : 'Tool'}: {message.name} · {message.phase}</span><span>{message.summary}</span></summary>
            </details>}
            {message.role === 'error' && <p role="alert">{message.text}</p>}
          </article>
        ))}
      </div>
      <div className="ai-live-status" role="status" aria-live="polite">
        {busy ? 'AI is working…' : ready ? 'AI is ready.' : setupState === 'error' ? 'AI setup needs attention.' : ''}
      </div>
      <form className="ai-prompt" onSubmit={(event) => { event.preventDefault(); sendPrompt() }}>
        <label className="sr-only" htmlFor="ai-prompt-input">Message the AI assistant</label>
        <textarea id="ai-prompt-input" value={prompt} rows="2" placeholder="Ask for an OpenSCAD change…"
          onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendPrompt() }
          }} disabled={!ready || busy} />
        <button type="submit" className="btn btn-sm btn-primary" disabled={!ready || busy || !prompt.trim()}>Send</button>
      </form>
      <p className="ai-footnote">AI-generated code can be unsafe. Review it before rendering.</p>
    </section>
  )
}
