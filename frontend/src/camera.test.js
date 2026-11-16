import assert from 'node:assert/strict'
import test from 'node:test'
import { savedCameras, validCamera } from './camera.js'

const pose = { position: [1, 2, 3], target: [0, 0, 0], up: [0, 0, 1] }

test('a valid pose round-trips', () => {
  assert.equal(validCamera(pose), true)
  assert.deepEqual(savedCameras({ box: pose }), { box: pose })
})

test('stored junk and out-of-range values are dropped', () => {
  assert.deepEqual(savedCameras(null), {})
  assert.deepEqual(savedCameras([]), {})
  assert.deepEqual(savedCameras({ box: pose, bad: null, other: { ...pose, up: [0, 0] },
    huge: { ...pose, position: [1e9, 0, 0] } }), { box: pose })
  assert.equal(validCamera({ ...pose, target: [NaN, 0, 0] }), false)
})
