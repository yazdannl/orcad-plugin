import { useEffect, useId, useState } from 'react'
import { checkValue } from '../catalog.js'

function NumberField({ param, value, disabled, onChange, id }) {
  const [text, setText] = useState(String(value))
  const [error, setError] = useState(null)
  useEffect(() => { setText(String(value)); setError(null) }, [value])

  const commit = (raw) => {
    setText(raw)
    const parsed = raw.trim() === '' ? NaN : Number(raw)
    const problem = checkValue(param, parsed)
    setError(problem)
    if (!problem && parsed !== value) onChange(parsed)
  }
  const span = param.max - param.min
  const pct = span > 0 ? ((value - param.min) / span) * 100 : 0
  return (
    <>
      <div className="number-row">
        <input className="slider" type="range" min={param.min} max={param.max} step={param.step} value={value}
          disabled={disabled} aria-label={`${param.label} slider`} style={{ '--pct': `${pct}%` }}
          onChange={(e) => commit(e.target.value)} />
        <div className={`number-box${error ? ' has-error' : ''}`}>
          <input id={id} type="number" inputMode="decimal" min={param.min} max={param.max} step={param.step}
            value={text} disabled={disabled} aria-invalid={Boolean(error)}
            onChange={(e) => commit(e.target.value)} onBlur={() => { if (error) { setText(String(value)); setError(null) } }} />
          {param.unit && <span className="unit">{param.unit}</span>}
        </div>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
    </>
  )
}

export function ParamField({ param, value, disabled, disabledHint, serverError, onChange }) {
  const id = useId()
  const hint = disabled ? disabledHint : param.help
  if (param.type === 'boolean') {
    return (
      <div className={`field field-switch${disabled ? ' is-disabled' : ''}${serverError ? ' is-invalid' : ''}`}>
        <div className="field-text">
          <label htmlFor={id}>{param.label}</label>
          {hint && <p className="field-help">{hint}</p>}
          {serverError && <p className="field-error" role="alert">{serverError}</p>}
        </div>
        <button id={id} type="button" role="switch" aria-checked={value} className="switch" disabled={disabled}
          onClick={() => onChange(!value)}><span /></button>
      </div>
    )
  }
  const options = param.options
  const segmented = options && options.length <= 3 && options.every((o) => o.label.length <= 12)
  return (
    <div className={`field${disabled ? ' is-disabled' : ''}${serverError ? ' is-invalid' : ''}`}>
      <div className="field-head">
        <label htmlFor={id}>{param.label}</label>
        {hint && <span className="field-hint" tabIndex={0} data-tip={hint} aria-label={hint}>?</span>}
      </div>
      {segmented ? (
        <div className="segmented segmented-sm" role="radiogroup" aria-label={param.label} id={id}>
          {options.map((o) => (
            <button key={o.value} type="button" role="radio" aria-checked={value === o.value} disabled={disabled}
              className={value === o.value ? 'is-active' : ''} onClick={() => onChange(o.value)}>{o.label}</button>
          ))}
        </div>
      ) : options ? (
        <div className="select-wrap">
          <select id={id} value={value} disabled={disabled} onChange={(e) => onChange(Number(e.target.value))}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      ) : (
        <NumberField id={id} param={param} value={value} disabled={disabled} onChange={onChange} />
      )}
      {serverError && <p className="field-error" role="alert">{serverError}</p>}
    </div>
  )
}
