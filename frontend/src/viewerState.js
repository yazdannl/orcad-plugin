const VIEW_DIRECTIONS = Object.freeze({
  iso: Object.freeze({ direction: [1, 1, 1], up: [0, 0, 1] }),
  front: Object.freeze({ direction: [0, -1, 0], up: [0, 0, 1] }),
  top: Object.freeze({ direction: [0, 0, 1], up: [0, 1, 0] }),
  side: Object.freeze({ direction: [1, 0, 0], up: [0, 0, 1] }),
})

export function viewCameraState(view, distance = 1) {
  const definition = VIEW_DIRECTIONS[view] || VIEW_DIRECTIONS.iso
  const length = Math.hypot(...definition.direction) || 1
  const radius = Number.isFinite(distance) && distance > 0 ? distance : 1
  return {
    position: definition.direction.map((value) => value / length * radius),
    up: [...definition.up],
    target: [0, 0, 0],
  }
}

export function fitCameraDistance(modelSize, aspect = 1, fov = 40, padding = 1.2) {
  const size = Number.isFinite(modelSize) && modelSize > 0 ? modelSize : 1
  const ratio = Number.isFinite(aspect) && aspect > 0 ? aspect : 1
  const verticalHalfFov = (Number.isFinite(fov) && fov > 0 ? fov : 40) * Math.PI / 360
  const horizontalHalfFov = Math.atan(Math.tan(verticalHalfFov) * ratio)
  const limitingHalfFov = Math.min(verticalHalfFov, horizontalHalfFov)
  const radius = size * Math.sqrt(3) / 2
  return radius / Math.tan(limitingHalfFov) * (Number.isFinite(padding) && padding > 1 ? padding : 1.2)
}

export function zoomCameraDistance(distance, modelSize, factor) {
  const size = Number.isFinite(modelSize) && modelSize > 0 ? modelSize : 1
  const current = Number.isFinite(distance) && distance > 0 ? distance : size
  const scale = Number.isFinite(factor) && factor > 0 ? factor : 1
  return Math.max(size * 0.35, Math.min(size * 8, current * scale))
}
