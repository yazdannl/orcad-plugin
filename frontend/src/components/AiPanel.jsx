import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './Icons.jsx'
const THINKING_LEVELS = ['off', 'low', 'medium', 'high']

function configFrom(status) {
  const config = status?.config || {}
  const models = Array.isArray(status?.models) ? status.models : []
  const provider = config.provider || models[0]?.provider || ''
  return {
    source: config.source === 'pi' ? 'pi' : 'managed',
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

function AuthNotice({ event }) {
  if (event.type === 'device_code') return (
    <div className="ai-auth-notice">
      <strong>Device sign-in</strong>
      <p>Enter this code at <a href={event.verificationUri} target="_blank" rel="noreferrer">{event.verificationUri}</a></p>
      <code className="ai-device-code">{event.userCode}</code>
      {event.expiresInSeconds && <small>Expires in about {Math.ceil(event.expiresInSeconds / 60)} minutes.</small>}
    </div>
  )
  if (event.type === 'auth_url') return (
    <div className="ai-auth-notice"><strong>Continue in your browser</strong>
      {event.instructions && <p>{event.instructions}</p>}
      <a href={event.url} target="_blank" rel="noreferrer">Open sign-in page</a>
    </div>
  )
  return <div className="ai-auth-notice"><p>{event.message}</p>{event.links?.map((link) =>
    <a key={link.url} href={link.url} target="_blank" rel="noreferrer">{link.label || link.url}</a>)}</div>
}

function AuthPromptDialog({ prompt, notices, onSubmit, onCancel }) {
  const [value, setValue] = useState('')
  const [selected, setSelected] = useState(prompt.options?.[0]?.id || '')
  const select = prompt.type === 'select'
  const secret = prompt.type === 'secret'
  return createPortal(
    <div className="ai-modal-scrim" role="presentation">
      <section className="ai-modal" role="dialog" aria-modal="true" aria-labelledby="ai-auth-title">
        <h3 id="ai-auth-title">Provider sign-in</h3>
        {notices.map((notice, index) => <AuthNotice key={`${notice.type}-${index}`} event={notice} />)}
        <p className="ai-modal-copy">{prompt.message}</p>
        {select ? <label className="ai-field"><span>Choose an option</span>
          <select value={selected} onChange={(event) => setSelected(event.target.value)}>
            {(prompt.options || []).map((option) => <option key={option.id} value={option.id}>{option.label}{option.description ? ` — ${option.description}` : ''}</option>)}
          </select></label>
          : <label className="ai-field"><span>{prompt.type === 'manual_code' ? 'Authorization code or redirect URL' : 'Response'}</span>
            <input autoFocus type={secret ? 'password' : 'text'} value={value} autoComplete="off" spellCheck="false"
              placeholder={prompt.placeholder || ''} onChange={(event) => setValue(event.target.value)} />
          </label>}
        <div className="ai-modal-actions">
          <button type="button" className="btn btn-sm" onClick={onCancel}>Cancel</button>
          <button type="button" className="btn btn-sm btn-primary" disabled={select ? !selected : !value.trim()}
            onClick={() => onSubmit(select ? selected : value)}>Continue</button>
        </div>
      </section>
    </div>, document.body)
}

function CustomProviderDialog({ provider, result, detection, sendMessage, dispatch, onClose }) {
  const [form, setForm] = useState(() => ({
    id: provider?.id || '', name: provider?.name || '', baseUrl: provider?.baseUrl || '',
    api: provider?.api || 'openai-completions', models: (provider?.models || []).map((model) => model.id).join('\n'),
    api_key: '', remove_key: false,
  }))
  const [detectId, setDetectId] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(false)

  useEffect(() => {
    if (detection?.id !== detectId || !detectId) return
    setDetecting(false)
    if (detection.ok) {
      setForm((current) => ({ ...current, models: detection.models.map((model) => model.id).join('\n') }))
      setNotice(`Found ${detection.models.length} model${detection.models.length === 1 ? '' : 's'}.`)
    } else setNotice(detection.error || 'Model discovery failed.')
    dispatch({ type: 'clear-provider-detect' })
  }, [detection, detectId, dispatch])

  useEffect(() => {
    if (!result) return
    if (result.ok) onClose()
    else setNotice(result.error || 'Could not save the provider.')
    dispatch({ type: 'clear-provider-result' })
  }, [result, dispatch, onClose])

  const update = (patch) => setForm((current) => ({ ...current, ...patch }))
  const detect = () => {
    const id = `detect-${Date.now()}`
    setDetectId(id)
    setDetecting(true)
    setNotice('Checking the endpoint…')
    dispatch({ type: 'clear-provider-detect' })
    if (!sendMessage({ type: 'ai_provider_detect', id, baseUrl: form.baseUrl,
      ...(form.api_key ? { api_key: form.api_key } : {}) })) {
      setDetecting(false)
      setNotice('Could not reach the plugin.')
    }
  }
  const save = () => {
    const models = form.models.split(/[\n,]/).map((id) => id.trim()).filter(Boolean).map((id) => ({ id, name: id }))
    const record = { name: form.name, baseUrl: form.baseUrl, api: form.api, models }
    if (form.id) record.id = form.id
    dispatch({ type: 'clear-provider-result' })
    if (!sendMessage({ type: 'ai_provider_save', provider: record,
      ...(form.api_key ? { api_key: form.api_key } : {}), remove_key: form.remove_key })) {
      setNotice('Could not reach the plugin.')
    } else setNotice('Saving provider…')
    update({ api_key: '' })
  }
  const remove = () => {
    if (!confirmRemove) { setConfirmRemove(true); return }
    dispatch({ type: 'clear-provider-result' })
    if (sendMessage({ type: 'ai_provider_remove', id: form.id })) setNotice('Removing provider…')
    else setNotice('Could not reach the plugin.')
  }

  return createPortal(
    <div className="ai-modal-scrim" role="presentation">
      <section className="ai-modal ai-custom-modal" role="dialog" aria-modal="true" aria-labelledby="ai-custom-title">
        <h3 id="ai-custom-title">{form.id ? 'Edit custom endpoint' : 'Add custom endpoint'}</h3>
        <label className="ai-field"><span>Name</span><input value={form.name} maxLength={80} onChange={(event) => update({ name: event.target.value })} /></label>
        <label className="ai-field"><span>API base URL</span><input value={form.baseUrl} placeholder="http://localhost:11434/v1"
          autoComplete="url" onChange={(event) => update({ baseUrl: event.target.value })} /></label>
        <label className="ai-field"><span>API type</span><select value={form.api} onChange={(event) => update({ api: event.target.value })}>
          <option value="openai-completions">OpenAI Completions (Ollama, LM Studio, vLLM)</option>
          <option value="openai-responses">OpenAI Responses</option>
          <option value="anthropic-messages">Anthropic Messages</option>
          <option value="google-generative-ai">Google Generative AI</option>
        </select></label>
        <label className="ai-field"><span>Model IDs (one per line)</span><textarea rows="4" value={form.models}
          onChange={(event) => update({ models: event.target.value })} /></label>
        <label className="ai-field"><span>API key (optional)</span><input type="password" value={form.api_key} autoComplete="new-password"
          placeholder={provider?.has_key ? 'Saved key — leave blank to keep' : 'Leave blank for local/keyless endpoints'}
          onChange={(event) => update({ api_key: event.target.value, remove_key: false })} /></label>
        {provider?.has_key && !form.api_key && <label className="ai-check"><input type="checkbox" checked={form.remove_key}
          onChange={(event) => update({ remove_key: event.target.checked })} /> Remove saved key</label>}
        <div className="ai-custom-tools">
          <button type="button" className="btn btn-sm" onClick={detect} disabled={detecting || !form.baseUrl}>Detect models</button>
          <span role="status" aria-live="polite">{notice}</span>
        </div>
        {confirmRemove && <p className="ai-warning" role="alert">Remove this endpoint and its saved key? Click Remove again to confirm.</p>}
        <div className="ai-modal-actions">
          <button type="button" className="btn btn-sm" onClick={onClose}>Cancel</button>
          {form.id && <button type="button" className="btn btn-sm btn-danger" onClick={remove}>Remove</button>}
          <button type="button" className="btn btn-sm btn-primary" onClick={save} disabled={detecting || !form.name.trim() || !form.baseUrl || !form.models.trim()}>Save</button>
        </div>
      </section>
    </div>, document.body)
}

function Settings({ status, configChoice, onConfigChoice, sendMessage, state, dispatch }) {
  const [choice, setChoice] = useState(() => {
    const initial = configChoice || configFrom(status)
    return { ...initial, source: initial.source === 'pi' ? 'pi' : 'managed' }
  })
  const [notice, setNotice] = useState('')
  const [authType, setAuthType] = useState('')
  const [authProvider, setAuthProvider] = useState('')
  const [custom, setCustom] = useState(undefined)
  const models = Array.isArray(status?.models) ? status.models : []
  const providers = Array.isArray(status?.providers) ? status.providers : []
  const customProviders = Array.isArray(status?.custom_providers) ? status.custom_providers : []
  const authProviders = providers.filter((provider) => provider.methods?.includes(authType))
  const configuredProviders = providers.filter((provider) => provider.status !== 'not configured')
  const providerOptions = [...new Set([...models.map((model) => model.provider).filter(Boolean), ...(choice.provider ? [choice.provider] : [])])]
  const availableModels = models.filter((model) => !choice.provider || model.provider === choice.provider)
  const modelValue = `${choice.provider}::${choice.model}`
  const authPrompt = state.authPrompt
  const authNotices = state.authNotices || []
  const customResult = state.providerResult
  const detection = state.providerDetect

  useEffect(() => {
    if (configChoice || !status?.config) return
    const next = configFrom(status)
    setChoice(next)
    onConfigChoice(next)
  }, [configChoice, status?.config, onConfigChoice])

  useEffect(() => {
    if (!state.authResult) return
    setNotice(state.authResult.ok ? 'Provider sign-in complete.' : state.authResult.message || 'Provider sign-in did not complete.')
    dispatch({ type: 'clear-auth-result' })
  }, [state.authResult, dispatch])

  const closeCustom = () => setCustom(undefined)
  const updateChoice = (patch) => {
    const next = { ...choice, ...patch }
    setChoice(next)
    onConfigChoice(next)
  }
  const saveConfig = () => {
    dispatch({ type: 'clear-auth-result' })
    setNotice(sendMessage({ type: 'ai_config', ...choice }) ? 'Settings applied.' : 'Could not reach the plugin.')
  }
  const startAuth = () => {
    if (!authProvider || !authType) return
    dispatch({ type: 'clear-auth-result' })
    setNotice('Starting provider sign-in…')
    if (!sendMessage({ type: 'ai_auth', action: 'login', provider: authProvider, auth_type: authType })) {
      setNotice('Could not reach the plugin.')
    }
  }
  const signOut = (provider) => {
    dispatch({ type: 'clear-auth-result' })
    sendMessage({ type: 'ai_auth', action: 'logout', provider: provider.id })
  }
  const submitAuth = (value) => {
    sendMessage({ type: 'ai_auth_response', id: authPrompt.id, value })
    dispatch({ type: 'clear-auth-prompt' })
  }
  const cancelAuth = () => {
    if (authPrompt) sendMessage({ type: 'ai_auth_response', id: authPrompt.id, cancelled: true })
    sendMessage({ type: 'ai_auth_cancel' })
    dispatch({ type: 'clear-auth-prompt' })
  }

  return (
    <>
      <details className="ai-settings">
        <summary><Icon name="settings" size={15} /> Settings</summary>
        <div className="ai-settings-body">
          <h3 className="ai-settings-title">Provider and model</h3>
          <label className="ai-field"><span>Mode</span>
            <select value={choice.source} onChange={(event) => updateChoice({ source: event.target.value })}>
              <option value="pi">Use my pi setup</option><option value="managed">orcad-managed</option>
            </select>
          </label>
          <p className="ai-settings-help">{choice.source === 'pi'
            ? 'Uses your existing ~/.pi/agent setup. Provider credentials stay there.'
            : 'Sign-ins and custom providers are isolated in orcad’s private pi directory.'}</p>
          <label className="ai-field"><span>Provider</span>
            <select aria-label="AI provider" value={choice.provider} onChange={(event) => {
              const provider = event.target.value
              updateChoice({ provider, model: models.find((item) => item.provider === provider)?.id || '' })
            }}>
              {!providerOptions.length && <option value="">No providers available</option>}
              {providerOptions.map((provider) => <option key={provider} value={provider}>{provider}</option>)}
            </select>
          </label>
          <label className="ai-field"><span>Model</span>
            <select aria-label="AI model" value={modelValue} onChange={(event) => {
              const [provider, ...parts] = event.target.value.split('::')
              updateChoice({ provider, model: parts.join('::') })
            }}>
              {!availableModels.length && <option value="">No models available</option>}
              {availableModels.map((model) => <option key={`${model.provider}::${model.id}`} value={`${model.provider}::${model.id}`}>
                {model.name || model.id}
              </option>)}
            </select>
          </label>
          <label className="ai-field"><span>Thinking level</span>
            <select value={choice.thinking} onChange={(event) => updateChoice({ thinking: event.target.value })}>
              {THINKING_LEVELS.map((level) => <option key={level} value={level}>{level[0].toUpperCase() + level.slice(1)}</option>)}
            </select>
          </label>
          <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy)} onClick={saveConfig}>Apply mode and model</button>

          <div className="ai-provider-section">
            <div className="ai-section-head"><strong>Configured providers</strong>
              <span>{choice.source === 'managed' ? 'orcad-managed' : 'pi setup'}</span></div>
            {!configuredProviders.length && !customProviders.length && <p className="ai-settings-help">No providers configured yet.</p>}
            {configuredProviders.map((provider) => <div className="ai-provider-row" key={provider.id}>
              <span className="ai-provider-name">{provider.name}</span><span className="ai-provider-status">{provider.status}</span>
              {choice.source === 'managed'
                ? <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy)} onClick={() => signOut(provider)}>Sign out</button>
                : <span className="ai-provider-status">Pi setup</span>}
            </div>)}
            {choice.source === 'managed' ? <>
              {customProviders.map((provider) => <div className="ai-provider-row" key={`custom-${provider.id}`}>
                <span className="ai-provider-name">{provider.name}</span>
                <span className="ai-provider-status">Custom{provider.has_key ? ' · key set' : ''}</span>
                <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy)} onClick={() => setCustom(provider)}>Edit</button>
              </div>)}
              <div className="ai-provider-actions">
                <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy) || !providers.some((p) => p.methods?.includes('oauth'))}
                  onClick={() => { setAuthType('oauth'); setAuthProvider(providers.find((p) => p.methods?.includes('oauth'))?.id || '') }}>Sign in</button>
                <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy) || !providers.some((p) => p.methods?.includes('api_key'))}
                  onClick={() => { setAuthType('api_key'); setAuthProvider(providers.find((p) => p.methods?.includes('api_key'))?.id || '') }}>API key</button>
                <button type="button" className="btn btn-sm" disabled={Boolean(status?.busy)} onClick={() => setCustom(null)}>Custom endpoint</button>
              </div>
              {authType && <div className="ai-auth-picker">
                <label className="ai-field"><span>{authType === 'oauth' ? 'Subscription sign-in' : 'Provider API key'}</span>
                  <select value={authProvider} onChange={(event) => setAuthProvider(event.target.value)}>
                    {authProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}{provider.subscription ? ' (subscription)' : ''}</option>)}
                  </select>
                </label>
                <div className="ai-provider-actions">
                  <button type="button" className="btn btn-sm btn-primary" disabled={Boolean(status?.busy) || !authProvider} onClick={startAuth}>Continue</button>
                  <button type="button" className="btn btn-sm" onClick={() => setAuthType('')}>Cancel</button>
                </div>
              </div>}
            </> : <p className="ai-settings-help">Sign in and sign out are managed from your existing pi setup.</p>}
            {notice && <p className="ai-settings-notice" role="status" aria-live="polite">{notice}</p>}
          </div>
        </div>
      </details>
      {status?.auth_busy && !authPrompt && createPortal(<div className="ai-modal-scrim" role="presentation">
        <section className="ai-modal" role="dialog" aria-modal="true" aria-labelledby="ai-auth-wait-title">
          <h3 id="ai-auth-wait-title">Provider sign-in</h3>
          {authNotices.map((event, index) => <AuthNotice key={`${event.type}-${index}`} event={event} />)}
          <p className="ai-modal-copy">Waiting for provider…</p>
          <div className="ai-modal-actions"><button type="button" className="btn btn-sm" onClick={cancelAuth}>Cancel</button></div>
        </section>
      </div>, document.body)}
      {authPrompt && <AuthPromptDialog key={authPrompt.id} prompt={authPrompt.prompt} notices={authNotices}
        onSubmit={submitAuth} onCancel={cancelAuth} />}
      {custom !== undefined && <CustomProviderDialog provider={custom} result={customResult} detection={detection}
        sendMessage={sendMessage} dispatch={dispatch} onClose={closeCustom} />}
    </>
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
          {state.changed && <button type="button" className="btn btn-sm" onClick={revert}>Revert AI changes</button>}
          {state.activeId
            ? <button type="button" className="btn btn-sm btn-danger" onClick={stop}>Stop</button>
            : busy ? <span className="ai-operation-status">{status?.auth_busy ? 'Signing in…' : 'Applying…'}</span>
              : <button type="button" className="btn btn-sm" onClick={newChat}>New chat</button>}
          <Settings status={status} configChoice={configChoice} onConfigChoice={onConfigChoice} sendMessage={sendMessage}
            state={state} dispatch={dispatch} />
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
