import assert from 'node:assert/strict'
import test from 'node:test'
import { ensureSyntaxTree } from '@codemirror/language'
import { EditorState } from '@codemirror/state'
import { openScadLanguage } from './openscadLanguage.js'

test('OpenSCAD highlighting recognizes language tokens across multiline comments', () => {
  const doc = 'module box() {\n/* block\ncomment */ cube(2.5); // note\n$fn = 24; echo("ready");\n}'
  const state = EditorState.create({ doc, extensions: [openScadLanguage] })
  const tree = ensureSyntaxTree(state, state.doc.length, 1000)
  assert.ok(tree)
  const tokens = new Set()
  tree.iterate({ enter: (node) => tokens.add(node.name) })
  for (const token of ['keyword', 'comment', 'variableName.standard', 'number', 'string', 'variableName.special']) {
    assert.ok(tokens.has(token), `missing ${token} token`)
  }
})
