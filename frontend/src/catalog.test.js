import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { activePreset, applyPreset, catalogError, catalogReady, categories, checkValue, creditFor, defaults, groups, isDisabled, isLoaded, objectKeys, objectMeta, objectMatches, presetList, presetMatches, presets, qualityKeys, restoreParams, resultCounts, searchObjects, setCatalog, setParam, setSpec } from './catalog.js'

const CATALOG = JSON.parse(readFileSync(new URL('../../openscad/catalog.json', import.meta.url), 'utf8'))

// The page only receives a compact index; the backend adds a credit table for
// the models that carry one. `install` mirrors that split so the tests exercise
// the same shape the plugin sends, with the real catalog behind it.
function install(catalog = CATALOG) {
  const objects = {}
  const credits = {}
  for (const [key, spec] of Object.entries(catalog.objects)) {
    const entry = {
      label: spec.label, short: spec.short, category: spec.category, description: spec.description,
      ...(spec.tags ? { tags: spec.tags } : {}),
    }
    const library = spec.source?.startsWith('vendor/')
      ? Object.entries(catalog.libraries).find(([, entry2]) => spec.source.startsWith(`${entry2.root}/`))
      : null
    if (spec.attribution || library) {
      const id = library ? library[0] : `object:${key}`
      credits[id] = {
        label: spec.attribution?.label || library[1].label,
        license: spec.attribution?.license || library[1].license || '',
        author: spec.attribution?.author || library[1].author || '',
        repository: spec.attribution?.repository || library[1].repository || '',
      }
      entry.credit = id
    }
    objects[key] = entry
  }
  setCatalog({ revision: catalog.source.revision, quality_profiles: catalog.quality_profiles, credits, objects })
  for (const [key, spec] of Object.entries(catalog.objects)) setSpec(key, spec)
}

install()

test('the index alone describes every model, before any parameters arrive', () => {
  const keys = objectKeys()
  assert.equal(keys.length, Object.keys(CATALOG.objects).length)
  assert.ok(keys.includes('gridfinity_bin'))
  assert.ok(keys.includes('gridfinity_vase_bin'))
  assert.ok(qualityKeys().includes('balanced'))
  assert.ok(categories().length >= 32)
  for (const key of keys) {
    const meta = objectMeta(key)
    assert.ok(meta.label && meta.category, key)
    assert.equal(typeof meta.description, 'string', key)
  }
})

test('a model is only loaded once its specification has arrived', () => {
  setCatalog({ revision: 'x', quality_profiles: { balanced: {} }, objects: { demo: { label: 'Demo', category: 'Basics' } } })
  assert.equal(catalogReady(), true)
  assert.equal(isLoaded('demo'), false)
  assert.deepEqual(groups('demo'), [])
  assert.deepEqual(defaults('demo'), {})
  assert.deepEqual(presets('demo'), [])
  setSpec('demo', { parameters: [{ variable: 'size', label: 'Size', type: 'number', default: 4, min: 1, max: 9, step: 1 }] })
  assert.equal(isLoaded('demo'), true)
  assert.deepEqual(defaults('demo'), { size: 4 })
  assert.equal(groups('demo')[0].name, 'Parameters')
  install()
})

test('catalogReady is false for an empty catalog', () => {
  setCatalog(undefined)
  assert.equal(catalogReady(), false)
  assert.deepEqual(objectKeys(), [])
  assert.deepEqual(categories(), [])
  install()
})

test('a boot error travels with the catalog so the page can explain itself', () => {
  setCatalog(undefined, 'OSError: could not unpack the embedded backend')
  assert.equal(catalogError(), 'OSError: could not unpack the embedded backend')
  setCatalog({ objects: { demo: { label: 'Demo' } } }, '')  // a healthy reload clears it
  assert.equal(catalogError(), '')
  install()
})

test('every default passes the client-side check', () => {
  for (const key of objectKeys()) {
    const values = defaults(key)
    for (const param of objectMeta(key).parameters) assert.equal(checkValue(param, values[param.variable]), null, `${key}.${param.variable}`)
    assert.ok(groups(key).length > 0)
  }
})

test('vendored models carry a credit, originals do not', () => {
  const vendored = objectKeys().find((key) => objectMeta(key).credit)
  assert.ok(vendored, 'expected at least one credited object')
  const credit = creditFor(vendored)
  assert.ok(credit.label && credit.license, vendored)
  assert.equal(creditFor('box'), null)
  assert.equal(creditFor('nope'), null)
  assert.equal(creditFor(null), null)
})

test('checkValue mirrors backend range, step, type and option rules', () => {
  const length = objectMeta('box').parameters.find((p) => p.variable === 'length')
  assert.match(checkValue(length, 0.5), /Minimum/)
  assert.match(checkValue(length, 301), /Maximum/)
  assert.match(checkValue(length, 20.25), /steps/)
  assert.match(checkValue(length, Number.NaN), /number/)
  const gridx = objectMeta('gridfinity_bin').parameters.find((p) => p.variable === 'gridx')
  assert.match(checkValue(gridx, 2.5), /whole/)
})

test('saved parameters are restored only when still valid', () => {
  const restored = restoreParams('box', { length: 42, width: -1, bogus: 3 })
  assert.equal(restored.length, 42)
  assert.equal(restored.width, defaults('box').width)
  assert.equal('bogus' in restored, false)
  assert.deepEqual(restoreParams('box', null), defaults('box'))
})

test('text parameters mirror the backend length, line and control rules', () => {
  const text = objectMeta('nameplate').parameters.find((p) => p.variable === 'text')
  assert.equal(text.type, 'text')
  assert.equal(text.default, 'OrcaCAD')
  assert.equal(checkValue(text, 'OrcaCAD'), null)
  assert.equal(checkValue(text, 'Two\nLines'), null)
  assert.match(checkValue(text, ''), /Enter text/)
  assert.match(checkValue(text, '   '), /Enter text/)
  assert.match(checkValue(text, 5), /Enter text/)
  assert.match(checkValue(text, 'x'.repeat(121)), /Maximum 120/)
  assert.match(checkValue(text, ['x', 'x', 'x', 'x', 'x', 'x', 'x'].join('\n')), /Maximum 6 lines/)
  assert.match(checkValue(text, 'tab\there'), /control/)
  assert.equal(restoreParams('nameplate', { text: 'Kept', bogus: 'x' }).text, 'Kept')
  assert.equal(restoreParams('nameplate', { text: 42 }).text, text.default, 'bad saved text falls back')
})

test('nameplate presets carry text and only legal parameters', () => {
  const list = presets('nameplate')
  assert.ok(list.length >= 4)
  const badge = list.find((p) => p.name === 'Two-colour badge')
  assert.equal(badge.params.text_style, 3)
  const values = applyPreset('nameplate', defaults('nameplate'), badge)
  assert.equal(values.text, badge.params.text)
  assert.equal(values.text_style, 3)
})

test('enabling an option switches off its conflicts; dependencies disable fields', () => {
  const bin = defaults('gridfinity_bin')
  assert.equal(bin.refined_holes, false)
  const refined = setParam('gridfinity_bin', bin, 'refined_holes', true)
  assert.equal(refined.refined_holes, true)
  const next = setParam('gridfinity_bin', refined, 'magnet_holes', true)
  assert.equal(next.magnet_holes, true)
  assert.equal(next.refined_holes, false)
  const crush = objectMeta('gridfinity_bin').parameters.find((p) => p.variable === 'crush_ribs')
  assert.equal(isDisabled(crush, bin), true)
  assert.equal(isDisabled(crush, next), false)
})

test('search matches labels, categories and descriptions', () => {
  const hits = searchObjects('gridfinity')
  assert.ok(hits.includes('gridfinity_bin'))
  assert.ok(hits.includes('gridfinity_baseplate'))
  assert.ok(hits.every((key) => /gridfinity/i.test(key)))
  assert.ok(searchObjects('hollow').includes('tube'))
  assert.deepEqual(searchObjects(''), objectKeys())
})

test('search also matches tags and tolerates case and stray whitespace', () => {
  const tagged = { label: 'Screw Tray', category: 'Workshop', description: 'Holds M3 screws.', tags: ['hardware', 'M3', 'organiser'] }
  assert.equal(objectMatches(tagged, 'screw_tray', 'm3'), true)
  assert.equal(objectMatches(tagged, 'screw_tray', '  HARDWARE  '), true)
  assert.equal(objectMatches(tagged, 'screw_tray', 'screw    tray'), true)
  assert.equal(objectMatches(tagged, 'screw_tray', 'tray'), true)
  assert.equal(objectMatches(tagged, 'screw_tray', 'plate'), false)
  assert.equal(objectMatches(tagged, 'screw_tray', ''), true)
  assert.equal(objectMatches({ label: 'Plain', category: 'Workshop' }, 'plain', 'hardware'), false)
})

test('a category chip narrows the list and combines with the query', () => {
  const all = categories()
  assert.ok(all.length > 0)
  assert.equal(new Set(all).size, all.length, 'no duplicate categories')
  assert.ok(objectKeys().every((key) => all.includes(objectMeta(key).category)))
  assert.deepEqual(searchObjects('', 'all'), objectKeys())
  assert.deepEqual(searchObjects('', 'ALL'), objectKeys()) // the chip label is compared case-insensitively
  const basics = searchObjects('', 'basics')
  assert.ok(basics.length > 0)
  assert.ok(basics.every((key) => objectMeta(key).category === 'Basics'))
  assert.deepEqual(searchObjects('', 'nope'), [])
  const tubes = searchObjects('tube', 'Basics')
  assert.ok(tubes.includes('tube'))
  assert.ok(tubes.every((key) => objectMeta(key).category === 'Basics'))
  assert.deepEqual(searchObjects('no-such-model', 'Basics'), [])
  assert.deepEqual(resultCounts('tube', 'Basics'), { shown: tubes.length, total: objectKeys().length })
  assert.deepEqual(resultCounts('', 'Basics'), { shown: basics.length, total: objectKeys().length })
  assert.deepEqual(resultCounts('gridfinity'), { shown: searchObjects('gridfinity').length, total: objectKeys().length })
})

test('presets are optional catalog data and malformed entries are dropped', () => {
  assert.deepEqual(presets('no-such-object'), [])
  for (const key of objectKeys()) assert.ok(Array.isArray(presets(key)))
  assert.deepEqual(presetList(undefined), [])
  assert.deepEqual(presetList([null, 'x', 7, { params: {} }, { name: '' }, { name: 'No params' }]), [])
  assert.deepEqual(presetList([{ name: 'Ok', params: { width: 10 } }, { name: 'Bad' }]).map((p) => p.name), ['Ok'])
})

test('a preset applies known, valid parameters only', () => {
  const key = 'box'
  setSpec(key, { ...CATALOG.objects[key], presets: [{ name: 'Long tray', description: 'Wide and shallow', params: { length: 120, width: 60, height: 20, nope: 5, corner_radius: -3 } }] })
  const [preset] = presets(key)
  assert.equal(preset.description, 'Wide and shallow')
  const values = applyPreset(key, defaults(key), preset)
  assert.equal(values.length, 120)
  assert.equal(values.width, 60)
  assert.equal(values.height, 20)
  assert.equal('nope' in values, false, 'unknown variables are ignored')
  assert.equal(values.corner_radius, defaults(key).corner_radius, 'invalid values are ignored')
  assert.deepEqual(applyPreset(key, values, null), values, 'no preset, no change')
  assert.deepEqual(applyPreset(key, values, { name: 'Empty', params: {} }), values)
  setSpec(key, CATALOG.objects[key])
})

test('a preset that turns on conflicting options lands on a legal state', () => {
  const key = 'gridfinity_bin'
  setSpec(key, { ...CATALOG.objects[key], presets: [{ name: 'Magnets', params: { refined_holes: true, magnet_holes: true } }] })
  const values = applyPreset(key, defaults(key), presets(key)[0])
  assert.equal(values.magnet_holes, true)
  assert.equal(values.refined_holes, false)
  assert.equal(checkValue(objectMeta(key).parameters.find((p) => p.variable === 'refined_holes'), values.refined_holes), null)
  setSpec(key, CATALOG.objects[key])
})

test('the active preset follows the current values and clears on a manual edit', () => {
  const key = 'box'
  setSpec(key, {
    ...CATALOG.objects[key],
    presets: [
      { name: 'Small', params: { length: 20, width: 20 } },
      { name: 'Long', params: { length: 120, width: 40 } },
    ],
  })
  const small = applyPreset(key, defaults(key), presets(key)[0])
  assert.equal(activePreset(key, small), 'Small')
  assert.equal(activePreset(key, applyPreset(key, defaults(key), presets(key)[1])), 'Long')
  assert.equal(activePreset(key, { ...small, length: 21 }), null, 'a manual edit clears the marker')
  assert.equal(activePreset(key, { ...small, corner_radius: 4 }), 'Small', 'parameters the preset does not set do not matter')
  assert.equal(presetMatches(small, { name: 'Empty', params: {} }), false)
  assert.equal(presetMatches(small, null), false)
  assert.equal(activePreset(key, defaults(key)), 'Small')
  setSpec(key, CATALOG.objects[key])
})
