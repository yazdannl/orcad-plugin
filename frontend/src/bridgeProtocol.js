export const BRIDGE_MESSAGE_TYPES = new Set([
  'progress', 'cancelled', 'code', 'preview', 'folder_result', 'plate_result', 'result', 'error',
])

const CONTEXT_TYPES = new Set([
  'progress', 'cancelled', 'code', 'preview', 'folder_result', 'plate_result', 'result', 'error',
])

export function bridgeReadiness(bridge) {
  if (!bridge || typeof bridge.postMessage !== 'function' || typeof bridge.onMessage !== 'function') {
    return 'unavailable'
  }
  return 'ready'
}

export function preserveOnFailure(current, next, ok) {
  return ok && next ? next : current
}

export function recoverPostFailure(kind) {
  if (kind === 'preview') return { busy: false, status: 'preview unavailable; retry' }
  if (kind === 'operation') return { busy: false, status: 'export unavailable; retry' }
  return { busy: false, status: 'code generation unavailable; retry' }
}

export function normalizeBridgeMessage(message) {
  if (typeof message === 'string') {
    try {
      message = JSON.parse(message)
    } catch {
      return { ok: false, detail: 'expected a JSON object' }
    }
  }
  if (!message || typeof message !== 'object' || Array.isArray(message)) {
    return { ok: false, detail: 'expected an object response' }
  }
  let type
  try {
    type = message.type
  } catch {
    return { ok: false, detail: 'could not read the response type' }
  }
  if (typeof type !== 'string' || !type) return { ok: false, detail: 'response type is missing' }
  if (!BRIDGE_MESSAGE_TYPES.has(type)) return { ok: false, detail: `unknown response type: ${type}` }
  try {
    if (CONTEXT_TYPES.has(type)
        && (typeof message.request_id !== 'number' || typeof message.revision_id !== 'number')) {
      return { ok: false, detail: `${type} response is missing request context` }
    }
    if (type === 'preview' && typeof message.seq !== 'number') {
      return { ok: false, detail: 'preview response is missing its sequence number' }
    }
  } catch {
    return { ok: false, detail: 'could not read the response context' }
  }
  return { ok: true, message, type }
}

export function bridgeTechnicalDetail(message) {
  if (!message || typeof message !== 'object') return 'No structured response was received.'
  try {
    const keys = Object.keys(message).sort()
    return keys.length ? `Response fields: ${keys.join(', ')}` : 'The response object was empty.'
  } catch {
    return 'The response fields could not be inspected.'
  }
}

export function errorTechnicalDetail(error) {
  try {
    const name = error?.name || 'Error'
    const message = error?.message || String(error)
    return `${name}: ${String(message).slice(0, 500)}`
  } catch {
    return 'The bridge reported an unreadable error.'
  }
}
