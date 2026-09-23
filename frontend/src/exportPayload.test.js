import assert from 'node:assert/strict'
import test from 'node:test'
import { buildExportPayload, formatQuality } from './exportPayload.js'

const formats = ['stl', 'step', '3mf']

for (const format of formats) {
  test(`Objects export routes ${format} and tolerance`, () => {
    const payload = buildExportPayload({
      mode: 'objects', command: 'generate', primitive: 'box', params: { L: 20 },
      format, tolerance: 0.02, filename: 'box', context: { request_id: 1 },
    })
    assert.equal(payload.format, format)
    assert.equal(payload.tolerance, 0.02)
    assert.equal(payload.kind, 'generate')
    assert.deepEqual(payload.params, { L: 20 })
  })

  test(`Code export routes ${format} and tolerance`, () => {
    const payload = buildExportPayload({
      mode: 'code', command: 'run', code: 'result = Box(1, 1, 1)',
      format, tolerance: 0.03, filename: 'model', context: { request_id: 2 },
    })
    assert.equal(payload.format, format)
    assert.equal(payload.tolerance, 0.03)
    assert.equal(payload.kind, 'run')
    assert.equal(payload.code, 'result = Box(1, 1, 1)')
  })
}

test('Gridfinity objects opt into the upstream OpenSCAD backend', () => {
  const payload = buildExportPayload({
    mode: 'objects', command: 'generate', primitive: 'gridfinity_bin', params: { gridx: 2 },
    format: 'stl', tolerance: 0.02, qualityProfile: 'draft', filename: 'bin',
  })
  assert.equal(payload.object, 'bin')
  assert.equal(payload.quality_profile, 'draft')
  assert.equal(payload.primitive, 'gridfinity_bin')
})

test('Send to plate always routes STL', () => {
  const payload = buildExportPayload({
    mode: 'objects', command: 'plate', primitive: 'box', params: {},
    format: '3mf', tolerance: 0.04, filename: 'box',
  })
  assert.equal(payload.format, 'stl')
  assert.equal(payload.tolerance, 0.04)
})

test('quality help distinguishes exact and tessellated formats', () => {
  assert.match(formatQuality('stl'), /mesh export/)
  assert.match(formatQuality('3mf'), /finer tessellation/)
  assert.match(formatQuality('step'), /tolerance is not used/)
})
