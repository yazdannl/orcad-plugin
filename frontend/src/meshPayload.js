// Decode the compact preview wire format without coupling the viewport to the backend.
export function decodeMeshPayload(payload) {
  if (!payload || typeof payload !== 'object') return null
  const positions = Array.isArray(payload.tris) || ArrayBuffer.isView(payload.tris)
    ? payload.tris : payload.vertices
  if (!positions || positions.length < 9 || positions.length % 3) return null
  const vertices = Float32Array.from(positions, Number)
  if (!vertices.every(Number.isFinite)) return null
  const rawIndices = payload.indices && (Array.isArray(payload.indices) || ArrayBuffer.isView(payload.indices))
    ? payload.indices : null
  if (rawIndices && (rawIndices.length < 3 || rawIndices.length % 3
      || Array.from(rawIndices, Number).some((value) => !Number.isSafeInteger(value) || value < 0 || value >= vertices.length / 3))) return null
  const indices = rawIndices ? Uint32Array.from(rawIndices, Number) : null
  return { vertices, indices, triangles: indices ? indices.length / 3 : vertices.length / 9 }
}

export function meshTriangleCount(payload) {
  return decodeMeshPayload(payload)?.triangles || 0
}
