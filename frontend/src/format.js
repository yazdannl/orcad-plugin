export function mm(value) {
  return `${Number(value.toFixed(value >= 100 ? 1 : 2))}`
}

export function dimensions(size) {
  return `${size.map(mm).join(' × ')} mm`
}

export function volume(mm3) {
  return mm3 >= 1000 ? `${(mm3 / 1000).toFixed(1)} cm³` : `${mm3.toFixed(0)} mm³`
}

export function count(n) {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function bytesText(n) {
  return n >= 1_048_576 ? `${(n / 1_048_576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
}

export function seconds(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}
