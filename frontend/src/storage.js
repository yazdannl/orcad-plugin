const KEY = 'orcad.settings.v2'
const memory = new Map()

function store(host) {
  try {
    const storage = host?.localStorage
    storage.getItem(KEY)
    return storage
  } catch {
    // Embedded webviews may expose localStorage but refuse to use it.
    return { getItem: (k) => memory.get(k) ?? null, setItem: (k, v) => memory.set(k, v) }
  }
}

export function loadSettings(host = globalThis) {
  try {
    const value = JSON.parse(store(host).getItem(KEY) || '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch {
    return {}
  }
}

export function saveSettings(settings, host = globalThis) {
  try {
    store(host).setItem(KEY, JSON.stringify(settings))
    return true
  } catch {
    return false
  }
}
