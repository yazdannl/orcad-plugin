import { HighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import { tags as t } from '@lezer/highlight'

const keywords = new Set([
  'assert', 'each', 'else', 'for', 'function', 'if', 'include', 'intersection_for', 'let', 'module', 'use',
])
const atoms = new Set(['false', 'true', 'undef'])
const builtins = new Set(`
  abs acos asin atan atan2 ceil cos exp floor ln log max min pow rands round sign sin sqrt tan
  len norm cross lookup search str chr ord concat version version_num parent_module
  cube sphere cylinder polyhedron square circle polygon text import import_dxf import_stl surface
  union difference intersection group hull minkowski linear_extrude rotate_extrude projection render color
  translate rotate scale resize mirror multmatrix offset children
`.trim().split(/\s+/))

const openScadParser = {
  startState: () => ({ blockComment: false }),
  token(stream, state) {
    if (state.blockComment) {
      if (stream.match(/.*?\*\//)) state.blockComment = false
      else stream.skipToEnd()
      return 'comment'
    }
    if (stream.eatSpace()) return null
    if (stream.match('//')) {
      stream.skipToEnd()
      return 'comment'
    }
    if (stream.match('/*')) {
      state.blockComment = !stream.match(/.*?\*\//)
      return 'comment'
    }
    if (stream.match(/"(?:\\.|[^"\\])*"?/)) return 'string'
    if (stream.match(/\$[A-Za-z_]\w*/)) return 'variable-2'
    if (stream.match(/(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/)) return 'number'
    if (stream.match(/[A-Za-z_]\w*/)) {
      const word = stream.current()
      return keywords.has(word) ? 'keyword'
        : atoms.has(word) ? 'atom'
          : builtins.has(word) ? 'builtin' : 'variable'
    }
    if (stream.match(/[#!%*]/)) return 'meta'
    if (stream.match(/(?:==|!=|<=|>=|&&|\|\||[+\-/<>=!?:])/)) return 'operator'
    if (stream.match(/[{}()\[\],.;]/)) return 'punctuation'
    stream.next()
    return null
  },
}

const openScadStyle = HighlightStyle.define([
  { tag: t.keyword, color: '#c792ea' },
  { tag: t.standard(t.variableName), color: '#82aaff' },
  { tag: t.atom, color: '#ffcb6b' },
  { tag: t.string, color: '#c3e88d' },
  { tag: t.number, color: '#f78c6c' },
  { tag: t.comment, color: '#74839a', fontStyle: 'italic' },
  { tag: t.operator, color: '#89ddff' },
  { tag: t.punctuation, color: '#a6accd' },
  { tag: t.special(t.variableName), color: '#f78c6c' },
])

export const openScadLanguage = StreamLanguage.define(openScadParser)
export const openScadHighlighting = syntaxHighlighting(openScadStyle)
