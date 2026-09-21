import assert from 'node:assert/strict'
import test from 'node:test'
import {
  defaultParams, filterPrimitiveEntries, loadSettings, persistedParams,
  restoreParams, saveObjectParams, saveSettings, selectPrimitive,
} from './objectState.js'

const primitives = {
  box: { label: 'Box', params: [['L', 'Length', 'mm', 'number', 20, 1, 100, 1]] },
  cylinder: { label: 'Cylinder', params: [['R', 'Radius', 'mm', 'number', 10, 1, 100, 1]] },
}

function hostWithStorage() {
  const values = new Map()
  return {
    orca: {
      storage: {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
      },
    },
  }
}

test('typing a search only filters objects; explicit selection changes the active object', () => {
  const active = 'box'
  assert.deepEqual(filterPrimitiveEntries(primitives, 'cyl').map(([key]) => key), ['cylinder'])
  assert.equal(active, 'box')
  assert.equal(selectPrimitive(active, 'cylinder', primitives), 'cylinder')
})

test('empty search results are clearable without changing the active object', () => {
  const active = 'box'
  assert.deepEqual(filterPrimitiveEntries(primitives, 'does-not-exist'), [])
  assert.deepEqual(filterPrimitiveEntries(primitives, '').map(([key]) => key), ['box', 'cylinder'])
  assert.equal(active, 'box')
})

test('object settings restore independently and reset only the active object', () => {
  const store = {}
  saveObjectParams(store, 'box', primitives.box, { L: 42 })
  saveObjectParams(store, 'cylinder', primitives.cylinder, { R: 18 })
  assert.deepEqual(restoreParams(primitives.box, store.box), { L: 42 })
  assert.deepEqual(restoreParams(primitives.cylinder, store.cylinder), { R: 18 })

  saveObjectParams(store, 'box', primitives.box, defaultParams(primitives.box))
  assert.deepEqual(restoreParams(primitives.box, store.box), { L: 20 })
  assert.deepEqual(restoreParams(primitives.cylinder, store.cylinder), { R: 18 })
})

test('invalid or stale saved values fall back to defaults', () => {
  assert.deepEqual(restoreParams(primitives.box, { L: 1000, unknown: 1 }), { L: 20 })
})

test('host storage is preferred and settings remain limited to UI state', () => {
  const host = hostWithStorage()
  saveSettings({ selected: 'box', query: 'box', paramsByObject: { box: { L: 42 } }, code: 'do not save' }, host)
  const saved = loadSettings(host)
  assert.deepEqual(saved.paramsByObject, { box: { L: 42 } })
  assert.equal(saved.code, undefined)
})

test('persisted parameters include defaults only for known objects', () => {
  assert.deepEqual(persistedParams(primitives, { box: { L: 42 }, other: { X: 1 } }), {
    box: { L: 42 },
  })
})
