import test from 'node:test'
import assert from 'node:assert/strict'
import {
  bridgeReadiness, normalizeBridgeMessage, preserveOnFailure, recoverPostFailure,
} from './bridgeProtocol.js'

test('missing bridge is unavailable and a complete bridge is ready', () => {
  assert.equal(bridgeReadiness(undefined), 'unavailable')
  assert.equal(bridgeReadiness({ postMessage() {} }), 'unavailable')
  assert.equal(bridgeReadiness({ postMessage() {}, onMessage() {} }), 'ready')
})

test('malformed and unknown bridge messages are rejected without throwing', () => {
  const unreadable = new Proxy({ type: 'progress' }, {
    get(target, property) {
      if (property === 'request_id') throw new Error('unreadable context')
      return target[property]
    },
  })
  for (const message of [null, 7, [], '{bad json', { type: 'surprise' }, { type: 'preview' }, unreadable]) {
    assert.doesNotThrow(() => normalizeBridgeMessage(message))
    assert.equal(normalizeBridgeMessage(message).ok, false)
  }
  const valid = normalizeBridgeMessage({ type: 'progress', request_id: 2, revision_id: 3 })
  assert.equal(valid.ok, true)
  assert.equal(valid.type, 'progress')
})

test('synchronous post failures clear busy state and expose retry status', () => {
  assert.deepEqual(recoverPostFailure('preview'), { busy: false, status: 'preview unavailable; retry' })
  assert.deepEqual(recoverPostFailure('operation'), { busy: false, status: 'export unavailable; retry' })
  assert.deepEqual(recoverPostFailure('code'), { busy: false, status: 'code generation unavailable; retry' })
})

test('preview failures preserve the last successful mesh and retries can replace it', () => {
  const previous = { tris: [0, 1, 2] }
  assert.equal(preserveOnFailure(previous, null, false), previous)
  const next = { tris: [3, 4, 5] }
  assert.equal(preserveOnFailure(previous, next, true), next)
})

test('retry inputs remain available because failed replacement is not accepted', () => {
  const inputs = { primitive: 'box', params: { L: 10 } }
  assert.deepEqual(inputs, { primitive: 'box', params: { L: 10 } })
  assert.equal(preserveOnFailure('last successful export', null, false), 'last successful export')
})
