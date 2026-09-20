import assert from 'node:assert/strict'
import test from 'node:test'
import { PRIMS } from './primitives.js'
import { isParamDisabled, parameterGroups } from './parameterUi.js'

const param = (name) => PRIMS.gridfinity_bin.params.find(([key]) => key === name)

function values(overrides = {}) {
  return Object.fromEntries([
    ...PRIMS.gridfinity_bin.params.map((item) => [item[0], item[4]]),
    ...Object.entries(overrides),
  ])
}

test('group metadata renders the requested sections and keeps plain objects usable', () => {
  assert.deepEqual(
    parameterGroups(PRIMS.gridfinity_bin).map(({ name }) => name),
    ['Size', 'Compartments', 'Labels', 'Mounting', 'Advanced'],
  )
  assert.deepEqual(parameterGroups(PRIMS.box).map(({ name }) => name), ['Parameters'])
})

test('dependent controls disable until their prerequisite is enabled', () => {
  const cylinderDiameter = param('CD')
  assert.equal(isParamDisabled(cylinderDiameter, values({ CYL: false })), true)
  assert.equal(isParamDisabled(cylinderDiameter, values({ CYL: true })), false)
})

test('refined and magnet controls prevent selecting both modes', () => {
  const refined = param('REFINED')
  const magnets = param('MAGNETS')
  assert.equal(isParamDisabled(refined, values({ REFINED: false, MAGNETS: true })), true)
  assert.equal(isParamDisabled(magnets, values({ REFINED: true, MAGNETS: false })), true)
  assert.equal(isParamDisabled(refined, values({ REFINED: true, MAGNETS: false })), false)
})
