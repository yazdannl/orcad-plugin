import assert from 'node:assert/strict'
import test from 'node:test'
import { aiReducer, initialAIState } from './ai.js'

const event = (id, value) => ({ type: 'event', id, event: value })

test('streams assistant text and thinking and folds tool phases into a collapsible row', () => {
  let state = aiReducer(initialAIState, { type: 'prompt', id: 'run-1', text: 'make a box', code: 'old' })
  state = aiReducer(state, event('run-1', { kind: 'text', delta: 'I will ' }))
  state = aiReducer(state, event('run-1', { kind: 'thinking', delta: 'check dimensions' }))
  state = aiReducer(state, event('run-1', { kind: 'text', delta: 'build it.' }))
  state = aiReducer(state, event('run-1', { kind: 'tool', call_id: 'tool-1', name: 'openscad', phase: 'start', summary: 'Rendering' }))
  state = aiReducer(state, event('run-1', { kind: 'tool', call_id: 'tool-1', name: 'openscad', phase: 'end', summary: 'Render passed' }))
  assert.equal(state.messages[1].text, 'I will build it.')
  assert.equal(state.messages[1].thinking, 'check dimensions')
  assert.deepEqual(state.messages[2], {
    role: 'tool', callId: 'tool-1', name: 'openscad', phase: 'end', summary: 'Render passed', isError: false,
  })
})

test('ignores events from old prompts and supports code-change revert state', () => {
  let state = aiReducer(initialAIState, { type: 'prompt', id: 'run-1', text: 'edit', code: 'before' })
  const unchanged = aiReducer(state, event('stale', { kind: 'text', delta: 'ignored' }))
  assert.equal(unchanged, state)
  state = aiReducer(state, { type: 'code', id: 'run-1', code: 'after' })
  assert.equal(state.changed, true)
  state = aiReducer(state, { type: 'done', id: 'run-1', ok: true, code: 'after' })
  assert.equal(state.activeId, null)
  assert.equal(state.changed, true)
  state = aiReducer(state, { type: 'revert' })
  assert.equal(state.changed, false)
  assert.equal(state.beforeCode, null)
})
