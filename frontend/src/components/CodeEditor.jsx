import { useMemo, useRef } from 'react'

export function CodeEditor({ value, onChange, onRun, errorLine }) {
  const gutter = useRef(null)
  const lines = useMemo(() => value.split('\n').length, [value])

  const keyDown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault()
      onRun()
    } else if (event.key === 'Tab' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault()
      // insertText keeps the native undo history; fall back to a manual splice.
      if (document.execCommand?.('insertText', false, '  ')) return
      const el = event.currentTarget
      const { selectionStart: start, selectionEnd: end } = el
      onChange(`${value.slice(0, start)}  ${value.slice(end)}`)
      requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = start + 2 })
    }
  }

  return (
    <div className="editor">
      <div className="editor-gutter" ref={gutter} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <div key={i} className={i + 1 === Math.min(errorLine, lines) ? 'is-error' : ''}>{i + 1}</div>
        ))}
      </div>
      <textarea className="editor-input" value={value} spellCheck={false} autoCapitalize="off"
        autoComplete="off" autoCorrect="off" wrap="off" aria-label="OpenSCAD code"
        onChange={(e) => onChange(e.target.value)} onKeyDown={keyDown}
        onScroll={(e) => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop }} />
    </div>
  )
}
