// Saved library state (favorites and recently used models). Keys are validated
// against the catalog because a saved model can disappear between versions.

export const MAX_RECENT = 12

export function savedKeys(value, exists) {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter((key) => typeof key === 'string' && exists(key)))]
}

export function toggleKey(keys, key) {
  return keys.includes(key) ? keys.filter((item) => item !== key) : [key, ...keys]
}

export function rememberKey(keys, key, limit = MAX_RECENT) {
  return [key, ...keys.filter((item) => item !== key)].slice(0, limit)
}
