import assert from 'node:assert/strict'
import test from 'node:test'
import { PRIMS } from './primitives.js'

function parameter(primitive, key) {
  return PRIMS[primitive].params.find((param) => param[0] === key)
}

test('mode metadata keeps implementation values and friendly labels', () => {
  const height = parameter('gridfinity_bin', 'HMODE')
  assert.equal(height[4], 0)
  assert.deepEqual(height[8], [
    { value: 0, label: 'Grid units' },
    { value: 1, label: 'Interior height' },
    { value: 2, label: 'Exterior height' },
    { value: 3, label: 'Exterior height with lip' },
  ])

  const tabs = parameter('gridfinity_bin', 'TABSTYLE')
  assert.equal(tabs[4], 1)
  assert.deepEqual(tabs[8].map(({ value }) => value), [0, 1, 2, 3, 4, 5])
  assert.deepEqual(tabs[8].map(({ label }) => label), [
    'Full', 'Auto', 'Left', 'Center', 'Right', 'None',
  ])
})

test('baseplate mode labels are bundled with their selected defaults', () => {
  const style = parameter('gridfinity_baseplate', 'STYLE')
  const holeStyle = parameter('gridfinity_baseplate', 'HOLESTYLE')
  assert.equal(style[4], 0)
  assert.deepEqual(style[8].map(({ label }) => label), [
    'Plain', 'Weighted', 'Skeletonized', 'Screw-together', 'Screw-together minimal',
  ])
  assert.equal(holeStyle[4], 0)
  assert.deepEqual(holeStyle[8].map(({ value }) => value), [0, 1, 2])
})

test('ordinary numeric parameters keep their existing slider metadata', () => {
  const length = parameter('box', 'L')
  assert.equal(length[3], 'number')
  assert.equal(length[4], 20)
  assert.deepEqual(length.slice(5), [1, 300, 0.5])
  assert.equal(length[8], undefined)
})
