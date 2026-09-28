import assert from 'node:assert/strict'
import test from 'node:test'
import { decodeMesh, meshStats } from './mesh.js'

const b64 = (typed) => Buffer.from(typed.buffer).toString('base64')
// Unit cube, outward-facing triangles.
const positions = new Float32Array([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1])
const faces = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7]

test('decodes base64 float32 positions with 16- and 32-bit indices', () => {
  for (const [type, Indices] of [['uint16', Uint16Array], ['uint32', Uint32Array]]) {
    const mesh = decodeMesh({ positions: b64(positions), indices: b64(new Indices(faces)), index_type: type })
    assert.deepEqual([...mesh.positions], [...positions])
    assert.deepEqual([...mesh.indices], faces)
  }
})

test('rejects malformed payloads and out-of-range indices', () => {
  assert.equal(decodeMesh(null), null)
  assert.equal(decodeMesh({ positions: 'AAAA', indices: '' }), null)
  assert.equal(decodeMesh({ positions: b64(positions), indices: b64(new Uint16Array([0, 1, 99])), index_type: 'uint16' }), null)
})

test('stats report size, volume and triangle count', () => {
  const stats = meshStats({ positions, indices: new Uint16Array(faces) })
  assert.deepEqual(stats.size, [1, 1, 1])
  assert.ok(Math.abs(stats.volume - 1) < 1e-9)
  assert.equal(stats.triangles, 12)
})
