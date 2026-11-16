import assert from 'node:assert/strict'
import test from 'node:test'
import { MAX_RECENT, rememberKey, savedKeys, toggleKey } from './library.js'

test('saved keys drop unknown and duplicate entries', () => {
  const exists = (key) => ['bin', 'box'].includes(key)
  assert.deepEqual(savedKeys(['box', 'gone', 'box', 3, null], exists), ['box'])
  assert.deepEqual(savedKeys('box', exists), [])
})

test('a star toggles one model and keeps the others', () => {
  assert.deepEqual(toggleKey(['a', 'b'], 'b'), ['a'])
  assert.deepEqual(toggleKey(['a'], 'b'), ['b', 'a'])
})

test('recents are newest-first, duplicate-free and bounded', () => {
  const keys = rememberKey(['a', 'b', 'c'], 'b')
  assert.deepEqual(keys, ['b', 'a', 'c'])
  assert.deepEqual(rememberKey(Array.from({ length: 20 }, (_, i) => `m${i}`), 'new').length, MAX_RECENT)
  assert.equal(rememberKey(['old'], 'new')[0], 'new')
})
