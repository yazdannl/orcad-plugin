import test from 'node:test'
import assert from 'node:assert/strict'
import { decodeMeshPayload, meshTriangleCount } from './meshPayload.js'

test('decodes compact triangle payloads and indexed backend meshes', () => {
  assert.equal(meshTriangleCount({ tris: [0, 0, 0, 1, 0, 0, 0, 1, 0] }), 1)
  const mesh = decodeMeshPayload({ vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2] })
  assert.equal(mesh.triangles, 1); assert.equal(mesh.indices[2], 2)
})
test('rejects malformed or non-finite mesh data', () => {
  assert.equal(decodeMeshPayload({ tris: [0, 0, 0] }), null)
  assert.equal(decodeMeshPayload({ tris: [0, 0, 0, 1, 0, 0, NaN, 1, 0] }), null)
  assert.equal(decodeMeshPayload({ vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 4] }), null)
})
