import test from 'node:test'
import assert from 'node:assert/strict'
import { statsCards, statsStatus, warningList } from './stats.js'

test('stats cards use named dimensions and explicit units', () => {
  assert.deepEqual(statsCards({
    dimensions_mm: { width: 20, depth: 30, height: 40 },
    volume_mm3: 24000,
    area_mm2: 5200,
  }), [
    { key: 'dimensions', label: 'Width × Depth × Height', value: '20 mm × 30 mm × 40 mm' },
    { key: 'volume', label: 'Volume', value: '24000 mm³' },
    { key: 'area', label: 'Area', value: '5200 mm²' },
  ])
})

test('stats parsing keeps legacy numeric fields usable', () => {
  assert.deepEqual(statsCards({ width_mm: 1.25, depth_mm: 2, height_mm: 3, volume_mm: 4, area_mm: 5 }), [
    { key: 'dimensions', label: 'Width × Depth × Height', value: '1.25 mm × 2 mm × 3 mm' },
    { key: 'volume', label: 'Volume', value: '4 mm³' },
    { key: 'area', label: 'Area', value: '5 mm²' },
  ])
})

test('stats association distinguishes current and stale results', () => {
  assert.equal(statsStatus({ revisionId: 4 }, 4, 'preview').label, 'Current preview')
  assert.equal(statsStatus({ revisionId: 3 }, 4, 'preview').label, 'Stale preview · last successful result')
  assert.equal(statsStatus({ revisionId: 3 }, 4, 'export').state, 'stale')
  assert.equal(statsStatus(null, 4, 'export').state, 'none')
})

test('warnings are propagated only as concise strings', () => {
  assert.deepEqual(warningList(['plain hole', '  clearance hole  ', '', 3]), ['plain hole', 'clearance hole'])
  assert.deepEqual(warningList(undefined), [])
})
