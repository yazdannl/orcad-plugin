function bytes(base64) {
  const binary = atob(base64)
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i)
  return out
}

// Decode the plugin's base64 little-endian float32 positions + uint16/32 indices.
export function decodeMesh(payload) {
  if (!payload || typeof payload.positions !== 'string' || typeof payload.indices !== 'string') return null
  const positionBytes = bytes(payload.positions)
  const indexBytes = bytes(payload.indices)
  const IndexArray = payload.index_type === 'uint16' ? Uint16Array : Uint32Array
  if (positionBytes.length % 12 || indexBytes.length % (IndexArray.BYTES_PER_ELEMENT * 3)) return null
  const positions = new Float32Array(positionBytes.buffer)
  const indices = new IndexArray(indexBytes.buffer)
  const vertexCount = positions.length / 3
  for (let i = 0; i < indices.length; i += 1) if (indices[i] >= vertexCount) return null
  return { positions, indices }
}

export function meshStats({ positions, indices }) {
  const min = [Infinity, Infinity, Infinity]
  const max = [-Infinity, -Infinity, -Infinity]
  for (let i = 0; i < positions.length; i += 3) {
    for (let axis = 0; axis < 3; axis += 1) {
      const v = positions[i + axis]
      if (v < min[axis]) min[axis] = v
      if (v > max[axis]) max[axis] = v
    }
  }
  let volume = 0
  for (let i = 0; i < indices.length; i += 3) {
    const a = indices[i] * 3, b = indices[i + 1] * 3, c = indices[i + 2] * 3
    const [ax, ay, az, bx, by, bz, cx, cy, cz] = [positions[a], positions[a + 1], positions[a + 2],
      positions[b], positions[b + 1], positions[b + 2], positions[c], positions[c + 1], positions[c + 2]]
    volume += (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz) + az * (bx * cy - by * cx)) / 6
  }
  return {
    min, max,
    size: max.map((v, axis) => v - min[axis]),
    volume: Math.abs(volume),
    triangles: indices.length / 3,
  }
}
