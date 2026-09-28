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

export function searchObjects(query) {
  const needle = String(query || '').trim().toLowerCase()
  return OBJECT_KEYS.filter((key) => {
    const o = OBJECTS[key]
    return !needle || [key, o.label, o.category, o.description].some((text) => String(text).toLowerCase().includes(needle))
  })
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
