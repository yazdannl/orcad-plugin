const SETTINGS_KEY = 'orcad.ui.settings.v1'
const memoryStorage = new Map()

function storageAdapter(host) {
  const orca = host?.orca
  const hostStorage = orca?.storage
  if (hostStorage && typeof hostStorage.getItem === 'function' && typeof hostStorage.setItem === 'function') {
    return hostStorage
  }
  if (typeof orca?.getStorageItem === 'function' && typeof orca?.setStorageItem === 'function') {
    return {
      getItem: (key) => orca.getStorageItem(key),
      setItem: (key, value) => orca.setStorageItem(key, value),
    }
  }
  try {
    if (host?.localStorage && typeof host.localStorage.getItem === 'function') return host.localStorage
  } catch {
    // Some embedded webviews expose localStorage but reject access to it.
  }
  return {
    getItem: (key) => memoryStorage.get(key) ?? null,
    setItem: (key, value) => memoryStorage.set(key, value),
  }
}

export function loadSettings(host = globalThis) {
  try {
    const value = storageAdapter(host).getItem(SETTINGS_KEY)
    if (typeof value !== 'string') return {}
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

export function saveSettings(settings, host = globalThis) {
  const state = {
    selected: typeof settings.selected === 'string' ? settings.selected : undefined,
    query: typeof settings.query === 'string' ? settings.query : undefined,
    paramsByObject: settings.paramsByObject && typeof settings.paramsByObject === 'object'
      ? settings.paramsByObject
      : {},
    format: typeof settings.format === 'string' ? settings.format : undefined,
    tolerance: Number.isFinite(settings.tolerance) ? settings.tolerance : undefined,
    wireframe: Boolean(settings.wireframe),
    spinning: Boolean(settings.spinning),
  }
  try {
    storageAdapter(host).setItem(SETTINGS_KEY, JSON.stringify(state))
    return true
  } catch {
    return false
  }
}

export function filterPrimitiveEntries(primitives, query = '') {
  const needle = String(query).trim().toLowerCase()
  return Object.entries(primitives).filter(([key, primitive]) => (
    !needle || key.toLowerCase().includes(needle) || primitive.label.toLowerCase().includes(needle)
  ))
}

export function selectPrimitive(current, next, primitives) {
  return Object.prototype.hasOwnProperty.call(primitives, next) ? next : current
}

export function defaultParams(primitive) {
  return Object.fromEntries((primitive?.params || []).map((param) => [param[0], param[4]]))
}

function isStoredValueValid(param, value) {
  const [, , , type, , min, max] = param
  if (type === 'bool') return typeof value === 'boolean'
  if (typeof value !== 'number' || !Number.isFinite(value)) return false
  if (type === 'int' && !Number.isInteger(value)) return false
  if (value < min || value > max) return false
  const options = Array.isArray(param[8]) ? param[8] : []
  return !options.length || options.some((option) => option.value === value)
}

export function restoreParams(primitive, saved = {}) {
  const restored = defaultParams(primitive)
  if (!saved || typeof saved !== 'object') return restored
  for (const param of primitive?.params || []) {
    if (Object.prototype.hasOwnProperty.call(saved, param[0]) && isStoredValueValid(param, saved[param[0]])) {
      restored[param[0]] = saved[param[0]]
    }
  }
  return restored
}

export function saveObjectParams(store, key, primitive, values) {
  const saved = {}
  for (const param of primitive?.params || []) {
    if (Object.prototype.hasOwnProperty.call(values, param[0]) && isStoredValueValid(param, values[param[0]])) {
      saved[param[0]] = values[param[0]]
    }
  }
  store[key] = saved
  return saved
}

export function persistedParams(primitives, store) {
  return Object.fromEntries(Object.entries(primitives)
    .filter(([key]) => store?.[key])
    .map(([key, primitive]) => [key, restoreParams(primitive, store[key])]))
}
