function numeric(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function compact(value) {
  return Number(value).toFixed(3).replace(/\.?(0+)$/, '')
}

function dimension(stats, name) {
  const named = stats?.dimensions_mm?.[name]
  return numeric(named) ?? numeric(stats?.[`${name}_mm`])
}

export function statsCards(stats = {}) {
  const cards = []
  const dimensions = ['width', 'depth', 'height'].map((name) => dimension(stats, name))
  if (dimensions.every((value) => value !== null)) {
    cards.push({
      key: 'dimensions',
      label: 'Width × Depth × Height',
      value: `${dimensions.map((value) => `${compact(value)} mm`).join(' × ')}`,
    })
  }
  const volume = numeric(stats.volume_mm3) ?? numeric(stats.volume_mm)
  if (volume !== null) cards.push({ key: 'volume', label: 'Volume', value: `${compact(volume)} mm³` })
  const area = numeric(stats.area_mm2) ?? numeric(stats.area_mm)
  if (area !== null) cards.push({ key: 'area', label: 'Area', value: `${compact(area)} mm²` })
  return cards
}

export function statsStatus(context, revisionId, kind = 'preview') {
  const title = kind === 'preview' ? 'preview' : 'final export'
  if (!context) return { state: 'none', label: `No ${title} stats` }
  if (context.revisionId === revisionId) {
    return { state: 'current', label: kind === 'preview' ? 'Current preview' : 'Final export · current parameters' }
  }
  return { state: 'stale', label: kind === 'preview'
    ? 'Stale preview · last successful result'
    : 'Stale export · last successful result' }
}

export function warningList(warnings) {
  return Array.isArray(warnings)
    ? warnings.filter((warning) => typeof warning === 'string' && warning.trim()).map((warning) => warning.trim())
    : []
}
