export async function copyPath(path, host = globalThis) {
  if (!path) return false
  try {
    if (host.navigator?.clipboard?.writeText) {
      await host.navigator.clipboard.writeText(path)
      return true
    }
    const document = host.document
    if (!document?.createElement || !document.body || !document.execCommand) return false
    const area = document.createElement('textarea')
    area.value = path
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const copied = document.execCommand('copy')
    area.remove()
    return copied
  } catch {
    return false
  }
}

export function handoffMessage(message) {
  if (!message) return ''
  if (message.handoff_status === 'open_request_sent') return 'open request sent'
  if (message.handoff_status === 'open_request_failed') return 'export succeeded; open request failed'
  if (message.handoff_status === 'export_failed') return 'export failed'
  return message.message || ''
}
