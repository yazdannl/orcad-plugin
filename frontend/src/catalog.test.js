import assert from 'node:assert/strict'
import test from 'node:test'
import { OBJECTS, OBJECT_KEYS, checkValue, defaults, groups, isDisabled, restoreParams, searchObjects, setParam } from './catalog.js'

test('every default passes the client-side check', () => {
  for (const key of OBJECT_KEYS) {
    const values = defaults(key)
    for (const param of OBJECTS[key].parameters) assert.equal(checkValue(param, values[param.variable]), null, `${key}.${param.variable}`)
    assert.ok(groups(key).length > 0)
  }
})

test('checkValue mirrors backend range, step, type and option rules', () => {
  const length = OBJECTS.box.parameters.find((p) => p.variable === 'length')
  assert.match(checkValue(length, 0.5), /Minimum/)
  assert.match(checkValue(length, 301), /Maximum/)
  assert.match(checkValue(length, 20.25), /steps/)
  assert.match(checkValue(length, Number.NaN), /number/)
  const gridx = OBJECTS.gridfinity_bin.parameters.find((p) => p.variable === 'gridx')
  assert.match(checkValue(gridx, 2.5), /whole/)
})

test('saved parameters are restored only when still valid', () => {
  const restored = restoreParams('box', { length: 42, width: -1, bogus: 3 })
  assert.equal(restored.length, 42)
  assert.equal(restored.width, defaults('box').width)
  assert.equal('bogus' in restored, false)
  assert.deepEqual(restoreParams('box', null), defaults('box'))
})

test('enabling an option switches off its conflicts; dependencies disable fields', () => {
  const bin = defaults('gridfinity_bin')
  assert.equal(bin.refined_holes, false)
  const refined = setParam('gridfinity_bin', bin, 'refined_holes', true)
  assert.equal(refined.refined_holes, true)
  const next = setParam('gridfinity_bin', refined, 'magnet_holes', true)
  assert.equal(next.magnet_holes, true)
  assert.equal(next.refined_holes, false)
  const crush = OBJECTS.gridfinity_bin.parameters.find((p) => p.variable === 'crush_ribs')
  assert.equal(isDisabled(crush, bin), true)
  assert.equal(isDisabled(crush, next), false)
})

test('search matches labels, categories and descriptions', () => {
  assert.deepEqual(searchObjects('gridfinity'), ['gridfinity_bin', 'gridfinity_baseplate'])
  assert.ok(searchObjects('hollow').includes('tube'))
  assert.equal(searchObjects('').length, OBJECT_KEYS.length)
})
