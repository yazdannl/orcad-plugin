import test from 'node:test'
import assert from 'node:assert/strict'
import { fitCameraDistance, initialSpinEnabled, viewCameraState, zoomCameraDistance } from './viewerState.js'

test('preview spin is opt-in and disabled for reduced motion', () => {
  assert.equal(initialSpinEnabled(undefined, false), false)
  assert.equal(initialSpinEnabled(true, false), true)
  assert.equal(initialSpinEnabled(true, true), false)
})

test('standard views preserve build123d Z-up orientation', () => {
  assert.deepEqual(viewCameraState('front', 4), {
    position: [0, -4, 0], up: [0, 0, 1], target: [0, 0, 0],
  })
  assert.deepEqual(viewCameraState('top', 4), {
    position: [0, 0, 4], up: [0, 1, 0], target: [0, 0, 0],
  })
  assert.deepEqual(viewCameraState('side', 4), {
    position: [4, 0, 0], up: [0, 0, 1], target: [0, 0, 0],
  })
})

test('fit distance accounts for the limiting camera aspect ratio', () => {
  const wide = fitCameraDistance(10, 2)
  const narrow = fitCameraDistance(10, 0.5)
  assert.ok(narrow > wide)
  assert.equal(fitCameraDistance(0, 1), fitCameraDistance(1, 1))
})

test('zoom distance preserves the current zoom until a bound is reached', () => {
  assert.equal(zoomCameraDistance(5, 10, 1), 5)
  assert.equal(zoomCameraDistance(5, 10, 0.5), 3.5)
  assert.equal(zoomCameraDistance(50, 10, 2), 80)
  assert.equal(zoomCameraDistance(5, 10, 0.1), 3.5)
})
