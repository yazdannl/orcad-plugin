import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createDraftState, markDraftEdited, receiveGeneratedCode, replaceDraft,
} from './codeDraft.js'

test('edited draft survives mode changes and a newer generated response', () => {
  const state = createDraftState()
  assert.equal(receiveGeneratedCode(state, 1, 1, 'generated one'), true)
  markDraftEdited(state, 'hand edited code')

  let mode = 'objects'
  mode = 'code'
  assert.equal(mode, 'code')
  assert.equal(receiveGeneratedCode(state, 2, 2, 'generated two'), true)

  assert.equal(state.codeDraft, 'hand edited code')
  assert.equal(state.generatedCode, 'generated two')
  assert.equal(state.dirty, true)
})

test('stale generated responses cannot replace the draft or generated code', () => {
  const state = createDraftState()
  receiveGeneratedCode(state, 2, 2, 'latest generated')
  markDraftEdited(state, 'hand edited code')

  assert.equal(receiveGeneratedCode(state, 1, 2, 'stale generated'), false)
  assert.equal(state.codeDraft, 'hand edited code')
  assert.equal(state.generatedCode, 'latest generated')
  assert.equal(state.dirty, true)
})

test('generated code is replaced only by the explicit replace action', () => {
  const state = createDraftState()
  receiveGeneratedCode(state, 1, 1, 'generated code')
  markDraftEdited(state, 'hand edited code')

  assert.equal(state.codeDraft, 'hand edited code')
  assert.equal(replaceDraft(state), true)
  assert.equal(state.codeDraft, 'generated code')
  assert.equal(state.dirty, false)
})
