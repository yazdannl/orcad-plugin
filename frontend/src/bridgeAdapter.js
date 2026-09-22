import { bridgeReadiness, normalizeBridgeMessage } from './bridgeProtocol.js'

// Keep host-specific assumptions here; the React state machine only speaks in messages.
export function createBridge(host = globalThis) {
  const bridge = host?.orca
  return {
    available: () => bridgeReadiness(bridge) === 'ready',
    post(message) {
      if (!this.available()) return false
      bridge.postMessage(message)
      return true
    },
    subscribe(handler) {
      if (!this.available()) return () => {}
      const cleanup = bridge.onMessage((raw) => {
        const parsed = normalizeBridgeMessage(raw)
        if (parsed.ok) handler(parsed.message)
      })
      return typeof cleanup === 'function' ? cleanup : () => {}
    },
  }
}
