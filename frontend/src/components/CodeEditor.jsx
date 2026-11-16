import { useEffect, useMemo, useRef } from 'react'
import { EditorState } from '@codemirror/state'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { bracketMatching, indentUnit } from '@codemirror/language'
import { EditorView, drawSelection, highlightActiveLine, keymap } from '@codemirror/view'
import { openScadHighlighting, openScadLanguage } from '../openscadLanguage.js'

const editorTheme = EditorView.theme({
  '&': { height: '100%', backgroundColor: 'transparent', color: 'var(--text)' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { overflow: 'auto', fontFamily: 'var(--mono)', lineHeight: '1.6' },
  '.cm-content': { padding: '10px 12px', caretColor: '#22d3ee' },
  '.cm-cursor': { borderLeftColor: '#22d3ee' },
  '.cm-activeLine': { backgroundColor: 'rgba(148, 163, 184, .07)' },
}, { dark: true })

export function CodeEditor({ value, onChange, onRun, errorLine }) {
  const host = useRef(null)
  const gutter = useRef(null)
  const view = useRef(null)
  const callbacks = useRef({ onChange, onRun })
  const lines = useMemo(() => value.split('\n').length, [value])
  callbacks.current = { onChange, onRun }

  useEffect(() => {
    const editor = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          openScadLanguage,
          openScadHighlighting,
          history(),
          bracketMatching(),
          drawSelection(),
          highlightActiveLine(),
          EditorState.tabSize.of(2),
          indentUnit.of('  '),
          EditorView.contentAttributes.of({
            'aria-label': 'OpenSCAD code', spellcheck: 'false', autocapitalize: 'off',
            autocomplete: 'off', autocorrect: 'off',
          }),
          keymap.of([
            { key: 'Mod-Enter', run: () => { callbacks.current.onRun(); return true } },
            { key: 'Tab', run: (editorView) => {
              const { from, to } = editorView.state.selection.main
              editorView.dispatch({ changes: { from, to, insert: '  ' }, selection: { anchor: from + 2 } })
              return true
            } },
            ...defaultKeymap,
            ...historyKeymap,
          ]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) callbacks.current.onChange(update.state.doc.toString())
          }),
          editorTheme,
        ],
      }),
      parent: host.current,
    })
    view.current = editor
    const syncGutter = () => {
      if (gutter.current) gutter.current.scrollTop = editor.scrollDOM.scrollTop
    }
    editor.scrollDOM.addEventListener('scroll', syncGutter, { passive: true })
    syncGutter()

    return () => {
      editor.scrollDOM.removeEventListener('scroll', syncGutter)
      editor.destroy()
      view.current = null
    }
  }, [])

  useEffect(() => {
    const editor = view.current
    if (editor && editor.state.doc.toString() !== value) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } })
    }
  }, [value])

  return (
    <div className="editor">
      <div className="editor-gutter" ref={gutter} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <div key={i} className={i + 1 === Math.min(errorLine, lines) ? 'is-error' : ''}>{i + 1}</div>
        ))}
      </div>
      <div className="editor-input" ref={host} />
    </div>
  )
}
