// Catalog access.
//
// The catalog is deliberately *not* part of the page bundle. OrcaSlicer loads a
// plugin page with SetPage(), which hands the HTML to the webview in one piece
// and stops working somewhere below 2 MB - the 1 MB catalog plus the 350 model
// previews pushed the page over that wall and left the tab blank. So the page
// ships with a compact index (labels, categories, credits) and asks the plugin
// for one full object specification when an object is selected.
//
// A specification is merged into the index entry, so every helper below reads
// one object record whether the parameters have arrived or not. `isLoaded()`
// tells them apart.

const EMPTY_INDEX = { objects: {}, credits: {}, quality_profiles: {}, revision: '' }

let INDEX = EMPTY_INDEX
// Why the library is empty, when the plugin could say (its backend would not
// unpack, so no catalog arrived). Shown to the user instead of a blank list.
let BOOT_ERROR = ''

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

/** Installs the compact index the plugin injected into the page. */
export function setCatalog(index, error = '') {
  const objects = {}
  for (const [key, meta] of Object.entries(index?.objects || {})) {
    if (isObject(meta)) objects[key] = { ...meta }
  }
  INDEX = {
    objects,
    credits: isObject(index?.credits) ? { ...index.credits } : {},
    quality_profiles: isObject(index?.quality_profiles) ? { ...index.quality_profiles } : {},
    revision: typeof index?.revision === 'string' ? index.revision : '',
  }
  BOOT_ERROR = typeof error === 'string' ? error : ''
}

/** The reason the library is unavailable, or an empty string. */
export function catalogError() {
  return BOOT_ERROR
}

export function catalogReady() {
  return Object.keys(INDEX.objects).length > 0
}

export function objectKeys() {
  return Object.keys(INDEX.objects)
}

export function objectMeta(key) {
  return INDEX.objects[key]
}

/** True once the object's full specification (parameters, presets) has arrived. */
export function isLoaded(key) {
  return Array.isArray(INDEX.objects[key]?.parameters)
}

/** Merges one object specification from the plugin into the index. */
export function setSpec(key, spec) {
  if (!isObject(spec)) return null
  const merged = { ...(INDEX.objects[key] || {}), ...spec }
  INDEX.objects[key] = merged
  return merged
}

export function qualityKeys() {
  return Object.keys(INDEX.quality_profiles)
}

export function revision() {
  return INDEX.revision
}

export function categories() {
  return [...new Set(Object.values(INDEX.objects).map((object) => object.category).filter(Boolean))]
}

/** The credit an object's card points at, or null for the plugin's own models. */
export function creditFor(key) {
  const id = INDEX.objects[key]?.credit
  return id ? INDEX.credits[id] || null : null
}

export function defaults(key) {
  return Object.fromEntries((INDEX.objects[key]?.parameters || []).map((p) => [p.variable, p.default]))
}

export function groups(key) {
  const result = new Map()
  for (const param of INDEX.objects[key]?.parameters || []) {
    const name = param.group || 'Parameters'
    if (!result.has(name)) result.set(name, [])
    result.get(name).push(param)
  }
  return [...result].map(([name, params]) => ({ name, params }))
}

// Keeps a group only if it still has parameters matching every word.
export function filterGroups(list, query = '') {
  const words = normalize(query).split(' ').filter(Boolean)
  if (!words.length) return list
  return list.map((group) => ({
    ...group,
    params: group.params.filter((param) => {
      const text = normalize([param.label, param.variable, param.group, param.description, param.unit].join(' '))
      return words.every((word) => text.includes(word))
    }),
  })).filter((group) => group.params.length > 0)
}

function normalize(text) {
  return String(text ?? '').trim().toLowerCase().replace(/\s+/g, ' ')
}

// Every word an object can be found by: key, label, category, description, tags.
export function objectMatches(object, key, query = '', category = 'all') {
  if (normalize(category) && normalize(category) !== 'all'
    && normalize(object.category) !== normalize(category)) return false
  const words = normalize(query).split(' ').filter(Boolean)
  if (!words.length) return true
  const tags = Array.isArray(object.tags) ? object.tags : []
  const fields = [key, object.label, object.category, object.description, ...tags].map(normalize)
  return words.every((word) => fields.some((text) => text.includes(word)))
}

export function searchObjects(query = '', category = 'all') {
  return objectKeys().filter((key) => objectMatches(INDEX.objects[key], key, query, category))
}

export function resultCounts(query = '', category = 'all') {
  return { shown: searchObjects(query, category).length, total: objectKeys().length }
}

// Mirrors the backend rules so an obviously bad value never costs a render.
export function checkValue(param, value) {
  if (param.type === 'boolean') return typeof value === 'boolean' ? null : 'Must be on or off'
  if (param.type === 'text') {
    if (typeof value !== 'string') return 'Enter text'
    if (!value.trim()) return 'Enter text'
    if (param.max_length && Array.from(value).length > param.max_length) return `Maximum ${param.max_length} characters`
    if (param.max_lines && value.split('\n').length > param.max_lines) return `Maximum ${param.max_lines} lines`
    if (/[\t\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) return 'Remove the control character'
    return null
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Enter a number'
  if (param.type === 'integer' && !Number.isInteger(value)) return 'Enter a whole number'
  const unit = param.unit ? ` ${param.unit}` : ''
  if (param.min !== undefined && value < param.min) return `Minimum is ${param.min}${unit}`
  if (param.max !== undefined && value > param.max) return `Maximum is ${param.max}${unit}`
  if (param.step) {
    const steps = (value - (param.min ?? 0)) / param.step
    if (Math.abs(steps - Math.round(steps)) > 1e-6) return `Use steps of ${param.step}`
  }
  if (param.options && !param.options.some((o) => o.value === value)) return 'Choose an option'
  return null
}

export function restoreParams(key, saved) {
  const result = defaults(key)
  if (!saved || typeof saved !== 'object') return result
  for (const param of INDEX.objects[key]?.parameters || []) {
    const value = saved[param.variable]
    if (value !== undefined && checkValue(param, value) === null) result[param.variable] = value
  }
  return result
}

// Presets are optional catalog data: [{ name, description?, params: { variable: value } }].
export function presetList(list) {
  return Array.isArray(list)
    ? list.filter((preset) => preset && typeof preset === 'object'
      && typeof preset.name === 'string' && preset.name && preset.params && typeof preset.params === 'object')
    : []
}

export function presets(key) {
  return presetList(INDEX.objects[key]?.presets)
}

// Applies known parameter names only; anything unknown or invalid for the parameter is ignored.
// Conflicts resolve the same way a manual edit does, so a preset can never land on an illegal pair.
export function applyPreset(key, values, preset) {
  let next = { ...values }
  if (!preset || typeof preset.params !== 'object') return next
  for (const param of INDEX.objects[key]?.parameters || []) {
    const value = preset.params[param.variable]
    if (value === undefined || checkValue(param, value) !== null) continue
    next = setParam(key, next, param.variable, value)
  }
  return next
}

export function presetMatches(values, preset) {
  const entries = Object.entries(preset?.params || {})
  return entries.length > 0 && entries.every(([name, value]) => values?.[name] === value)
}

export function activePreset(key, values) {
  return presets(key).find((preset) => presetMatches(values, preset))?.name || null
}

export function isDisabled(param, values) {
  return (param.depends_on || []).some((name) => !values[name])
}

// Turning one option on switches off the options it conflicts with.
export function setParam(key, values, name, value) {
  const next = { ...values, [name]: value }
  const param = INDEX.objects[key]?.parameters.find((p) => p.variable === name)
  if (value === true) for (const other of param?.conflicts_with || []) next[other] = false
  return next
}
