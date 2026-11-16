// Copy/paste a model and its parameter values between OrcaCAD sessions. The
// payload is opaque base64url so it survives chat, email and clipboard handling;
// it is never executed, and the catalog validates every value when loaded.

const PREFIX = 'orcad1:'
const MAX_LENGTH = 20000

function toBase64(text) {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function fromBase64(value) {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(base64)
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
}

export function encodeConfig(objectKey, params) {
  if (typeof objectKey !== 'string' || !objectKey || !params || typeof params !== 'object' || Array.isArray(params)) return ''
  const payload = toBase64(JSON.stringify({ o: objectKey, p: params }))
  return PREFIX + payload
}

export function decodeConfig(text) {
  const value = String(text ?? '').trim()
  if (!value.startsWith(PREFIX) || value.length > MAX_LENGTH) return null
  try {
    const data = JSON.parse(fromBase64(value.slice(PREFIX.length)))
    if (!data || typeof data.o !== 'string' || !data.o) return null
    if (!data.p || typeof data.p !== 'object' || Array.isArray(data.p)) return null
    return { object: data.o, params: data.p }
  } catch {
    return null
  }
}
