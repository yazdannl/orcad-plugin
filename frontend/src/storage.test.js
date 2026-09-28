import assert from 'node:assert/strict'
import test from 'node:test'
import { loadSettings, saveSettings } from './storage.js'

test('settings round-trip through localStorage', () => {
  const data = new Map()
  const host = { localStorage: { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) } }
  assert.deepEqual(loadSettings(host), {})
  assert.equal(saveSettings({ mode: 'code' }, host), true)
  assert.deepEqual(loadSettings(host), { mode: 'code' })
})

test('a webview that refuses storage falls back to memory', () => {
  const host = { get localStorage() { throw new Error('SecurityError') } }
  assert.equal(saveSettings({ quality: 'draft' }, host), true)
  assert.deepEqual(loadSettings(host), { quality: 'draft' })
})

test('corrupt stored JSON is ignored', () => {
  const host = { localStorage: { getItem: () => '[1,2', setItem: () => {} } }
  assert.deepEqual(loadSettings(host), {})
})
