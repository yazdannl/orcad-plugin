import assert from 'node:assert/strict'
import test from 'node:test'
import { decodeConfig, encodeConfig } from './share.js'

test('a configuration round-trips with its values, including non-ASCII text', () => {
  const params = { width: 42, engraving: 'København 🧊', flag: true }
  const text = encodeConfig('nameplate', params)
  assert.ok(text.startsWith('orcad1:'))
  assert.deepEqual(decodeConfig(text), { object: 'nameplate', params })
})

test('the payload is URL-safe and has no padding', () => {
  assert.doesNotMatch(encodeConfig('box', { x: 1 }), /[+/=]/)
})

test('bad input is rejected instead of being interpreted', () => {
  assert.equal(decodeConfig(''), null)
  assert.equal(decodeConfig('orcad1:not base64!'), null)
  assert.equal(decodeConfig('orcad1:' + Buffer.from('{"o":"box"}').toString('base64url')), null)
  assert.equal(decodeConfig('orcad1:' + Buffer.from('not json').toString('base64url')), null)
  assert.equal(decodeConfig('orcad1:x'.repeat(30000)), null)
  assert.equal(encodeConfig('', {}), '')
  assert.equal(encodeConfig('box', null), '')
})
