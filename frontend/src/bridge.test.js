import assert from 'node:assert/strict'
import test from 'node:test'
import { createBridge } from './bridge.js'

test('without window.orca the bridge is unavailable and send fails softly', () => {
  const bridge = createBridge({})
  assert.equal(bridge.available, false)
  assert.equal(bridge.send({ type: 'hello' }), false)
})

test('messages are parsed, validated and fanned out to every listener', () => {
  let deliver
  const sent = []
  const bridge = createBridge({ orca: { postMessage: (m) => sent.push(m), onMessage: (cb) => { deliver = cb } } })
  const a = [], b = []
  bridge.subscribe((m) => a.push(m))
  const off = bridge.subscribe((m) => b.push(m))
  deliver({ type: 'engine', state: 'ready' })
  deliver('{"type":"notice"}')
  deliver('not json')
  deliver({ nope: true })
  off()
  deliver({ type: 'late' })
  assert.deepEqual(a.map((m) => m.type), ['engine', 'notice', 'late'])
  assert.deepEqual(b.map((m) => m.type), ['engine', 'notice'])
  assert.equal(bridge.send({ type: 'hello' }), true)
  assert.deepEqual(sent, [{ type: 'hello' }])
})

test('a throwing host postMessage reports failure', () => {
  const bridge = createBridge({ orca: { postMessage: () => { throw new Error('gone') }, onMessage: () => {} } })
  assert.equal(bridge.send({ type: 'hello' }), false)
})
