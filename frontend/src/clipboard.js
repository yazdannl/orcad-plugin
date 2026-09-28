// navigator.clipboard needs a secure context that embedded webviews may lack.
export async function copyText(text, host = globalThis) {
  if (!text) return false
  try {
    await host.navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const area = host.document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.cssText = 'position:fixed;opacity:0'
      host.document.body.appendChild(area)
      area.select()
      const ok = host.document.execCommand('copy')
      area.remove()
      return ok
    } catch {
      return false
    }
  }
}
