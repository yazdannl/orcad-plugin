// Remembered camera poses, keyed by model (or by "code" for the editor).
// Values come from localStorage, so they are checked before reaching three.js.

function triple(value) {
  return Array.isArray(value) && value.length === 3
    && value.every((n) => typeof n === 'number' && Number.isFinite(n) && Math.abs(n) < 1e6)
}

export function validCamera(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && triple(value.position) && triple(value.target) && triple(value.up)
}

export function savedCameras(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result = {}
  for (const [key, camera] of Object.entries(value).slice(0, 400)) {
    if (typeof key === 'string' && key.length < 200 && validCamera(camera)) result[key] = camera
  }
  return result
}
