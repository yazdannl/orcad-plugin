import catalog from '../../openscad/catalog.json' with { type: 'json' }

export const QUALITY = Object.keys(catalog.quality_profiles)
export const OBJECTS = catalog.objects
export const OBJECT_KEYS = Object.keys(OBJECTS)
export const LIBRARY_REVISION = catalog.source.revision

export function defaults(key) {
  return Object.fromEntries((OBJECTS[key]?.parameters || []).map((p) => [p.variable, p.default]))
}

export function groups(key) {
  const result = new Map()
  for (const param of OBJECTS[key]?.parameters || []) {
    const name = param.group || 'Parameters'
    if (!result.has(name)) result.set(name, [])
    result.get(name).push(param)
  }
  return [...result].map(([name, params]) => ({ name, params }))
}

function normalize(text) {
  return String(text ?? '').trim().toLowerCase().replace(/\s+/g, ' ')
}

export function categories() {
  return [...new Set(OBJECT_KEYS.map((key) => OBJECTS[key].category).filter(Boolean))]
}

// Every word an object can be found by: key, label, category, description, tags.
export function objectMatches(object, key, query = '', category = 'all') {
  if (normalize(category) && normalize(category) !== 'all'
    && normalize(object.category) !== normalize(category)) return false
  const needle = normalize(query)
  if (!needle) return true
  const tags = Array.isArray(object.tags) ? object.tags : []
  return [key, object.label, object.category, object.description, ...tags]
    .some((text) => normalize(text).includes(needle))
}

export function searchObjects(query = '', category = 'all') {
  return OBJECT_KEYS.filter((key) => objectMatches(OBJECTS[key], key, query, category))
}

export function resultCounts(query = '', category = 'all') {
  return { shown: searchObjects(query, category).length, total: OBJECT_KEYS.length }
}

// Mirrors the backend rules so an obviously bad value never costs a render.
export function checkValue(param, value) {
  if (param.type === 'boolean') return typeof value === 'boolean' ? null : 'Must be on or off'
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
  for (const param of OBJECTS[key]?.parameters || []) {
    const value = saved[param.variable]
    if (value !== undefined && checkValue(param, value) === null) result[param.variable] = value
  }
  return result
}

// Presets are optional catalog data: [{ name, description?, params: { variable: value } }].
export function presetList(list) {
  if (!Array.isArray(list)) return []
  return list.filter((preset) => preset && typeof preset === 'object'
    && typeof preset.name === 'string' && preset.name && preset.params && typeof preset.params === 'object')
}

export function presets(key) {
  return presetList(OBJECTS[key]?.presets)
}

// Applies known parameter names only; anything unknown or invalid for the parameter is ignored.
// Conflicts resolve the same way a manual edit does, so a preset can never land on an illegal pair.
export function applyPreset(key, values, preset) {
  let next = { ...values }
  if (!preset || typeof preset.params !== 'object') return next
  for (const param of OBJECTS[key]?.parameters || []) {
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
  const param = OBJECTS[key]?.parameters.find((p) => p.variable === name)
  if (value === true) for (const other of param?.conflicts_with || []) next[other] = false
  return next
}
