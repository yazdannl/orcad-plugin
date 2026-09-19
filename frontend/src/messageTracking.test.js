import assert from 'node:assert/strict'
import test from 'node:test'
import { responseMatches } from './messageTracking.js'

const preview = (request_id, revision_id, seq) => ({ request_id, revision_id, seq })
const exportResult = (request_id, revision_id) => ({ request_id, revision_id, type: 'result' })

test('out-of-order preview responses only match the newest request', () => {
  const latest = { requestId: 2, revisionId: 4, seq: 2 }
  assert.equal(responseMatches(preview(1, 4, 1), latest, true), false)
  assert.equal(responseMatches(preview(2, 4, 2), latest, true), true)
})

test('out-of-order export responses cannot finish a newer operation', () => {
  const latest = { requestId: 8, revisionId: 6 }
  assert.equal(responseMatches(exportResult(7, 6), latest), false)
  assert.equal(responseMatches(exportResult(8, 6), latest), true)
})

test('mode switches invalidate responses from the previous revision', () => {
  const codeRequest = { requestId: 3, revisionId: 10 }
  assert.equal(responseMatches({ request_id: 3, revision_id: 9 }, codeRequest), false)
  assert.equal(responseMatches({ request_id: 3, revision_id: 10 }, codeRequest), true)
})
