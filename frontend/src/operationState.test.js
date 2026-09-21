import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createOperation, elapsedText, operationMatches, stageText, updateOperation,
} from './operationState.js'

test('busy operation accepts queued and stage transitions for its request only', () => {
  const operation = createOperation('operation', { request_id: 7, revision_id: 3 }, 1000)
  assert.equal(operation.stage, 'queued')
  assert.equal(updateOperation(operation, {
    request_id: 7, revision_id: 3, stage: 'building', message: 'Building model…',
  }), true)
  assert.equal(operation.stage, 'building')
  assert.equal(updateOperation(operation, {
    request_id: 8, revision_id: 3, stage: 'exporting',
  }), false)
  assert.equal(operation.stage, 'building')
})

test('preview sequence keeps canceled or stale responses out', () => {
  const operation = createOperation('preview', { request_id: 4, revision_id: 2, seq: 9 }, 0)
  assert.equal(operationMatches({ request_id: 4, revision_id: 2, seq: 8 }, operation, true), false)
  assert.equal(operationMatches({ request_id: 4, revision_id: 2, seq: 9 }, operation, true), true)
})

test('busy labels expose truthful stages and elapsed time without percentages', () => {
  assert.equal(stageText('tessellating'), 'tessellating')
  assert.equal(stageText('open-request'), 'sending to plate')
  assert.equal(elapsedText(1234), '1.2s')
  assert.equal(elapsedText(0), '0.0s')
})
