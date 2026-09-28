// window.orca is injected by OrcaSlicer before the page loads. onMessage()
// cannot unregister, so it is registered once and fanned out to listeners.
export function createBridge(host = globalThis) {
  const orca = host?.orca
  const available = Boolean(orca) && typeof orca.postMessage === 'function' && typeof orca.onMessage === 'function'
  const listeners = new Set()
  if (available) {
    orca.onMessage((data) => {
      let message = data
      if (typeof message === 'string') {
        try { message = JSON.parse(message) } catch { return }
      }
      if (message && typeof message === 'object' && typeof message.type === 'string') {
        for (const listener of listeners) listener(message)
      }
    })
  }
  return {
    available,
    send(message) {
      if (!available) return false
      try {
        orca.postMessage(message)
        return true
      } catch {
        return false
      }
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
