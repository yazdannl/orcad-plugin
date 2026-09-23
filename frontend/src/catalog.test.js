import assert from 'node:assert/strict'
import test from 'node:test'
import { OPENSCAD_PRIMITIVE_KEYS, PRIMS } from './catalog.js'

test('canonical OpenSCAD catalog replaces the legacy Gridfinity entries', () => {
  assert.deepEqual([...OPENSCAD_PRIMITIVE_KEYS].sort(), ['gridfinity_baseplate', 'gridfinity_bin'])
  assert.equal(PRIMS.gridfinity_bin.backendObject, 'bin')
  assert.equal(PRIMS.gridfinity_baseplate.backendObject, 'baseplate')
  assert.equal(PRIMS.gridfinity_bin.params.find(([key]) => key === 'gridx')[4], 3)
})

test('catalog metadata preserves options and dependencies for the UI', () => {
  const bin = PRIMS.gridfinity_bin
  const heightMode = bin.params.find(([key]) => key === 'gridz_define')
  const cylinderDiameter = bin.params.find(([key]) => key === 'cd')
  assert.equal(heightMode[8][1].label, 'Internal mm (excludes base and lip)')
  assert.equal(cylinderDiameter.at(-1).dependsOn, 'cut_cylinders')
})
