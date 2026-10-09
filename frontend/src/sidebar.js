export const SIDEBAR_DEFAULT = 370
export const SIDEBAR_MIN = 260
export const SIDEBAR_MAX = 720

// Keep the sidebar usable next to the viewport and the output panel: it may take
// 60% of the window at most, and never less than SIDEBAR_MIN.
export function clampSidebarWidth(width, viewportWidth) {
  const room = Number.isFinite(viewportWidth) ? Math.round(viewportWidth * 0.6) : SIDEBAR_MAX
  const widest = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, room))
  const value = Number.isFinite(width) ? Math.round(width) : SIDEBAR_DEFAULT
  return Math.min(widest, Math.max(SIDEBAR_MIN, value))
}
