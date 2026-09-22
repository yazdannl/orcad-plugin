// Decode the compact preview wire format without coupling the viewport to the backend.
export function decodeMeshPayload(payload) {
  if (!payload || typeof payload !== 'object') return null
  const positions = Array.isArray(payload.tris) || ArrayBuffer.isView(payload.tris)
    ? payload.tris : payload.vertices
  if (!positions || positions.length < 9 || positions.length % 3) return null
  const vertices = Float32Array.from(positions, Number)
  if (!vertices.every(Number.isFinite)) return null
  const indices = payload.indices && (Array.isArray(payload.indices) || ArrayBuffer.isView(payload.indices))
    ? Uint32Array.from(payload.indices, Number) : null
  if (indices && (indices.length < 3 || indices.length % 3 || indices.some((value) => value < 0 || value >= vertices.length / 3))) return null
  return { vertices, indices, triangles: indices ? indices.length / 3 : vertices.length / 9 }
}

export function meshTriangleCount(payload) {
  return decodeMeshPayload(payload)?.triangles || 0
}
