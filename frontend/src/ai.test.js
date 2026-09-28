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

test('tracks transient provider-auth dialogs and custom-provider results without storing responses', () => {
  let state = aiReducer(initialAIState, { type: 'auth-notice', event: { type: 'device_code', userCode: 'DEMO' } })
  state = aiReducer(state, { type: 'auth-prompt', id: 'dialog-1', prompt: { type: 'secret', message: 'API key' } })
  assert.equal(state.authPrompt.id, 'dialog-1')
  assert.equal(state.authNotices[0].userCode, 'DEMO')
  assert.equal(Object.hasOwn(state.authPrompt, 'value'), false)
  state = aiReducer(state, { type: 'auth-done', result: { ok: true } })
  assert.equal(state.authPrompt, null)
  assert.deepEqual(state.authNotices, [])
  state = aiReducer(state, { type: 'provider-detect-start', id: 'detect-1' })
  state = aiReducer(state, { type: 'provider-detect-result', result: { id: 'detect-1', ok: true, models: [{ id: 'local' }] } })
  assert.equal(state.providerDetect.models[0].id, 'local')
  state = aiReducer(state, { type: 'provider-result', result: { action: 'save', ok: true } })
  assert.equal(state.providerResult.ok, true)
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
