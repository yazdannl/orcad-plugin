const GROUP_ORDER = ['Size', 'Compartments', 'Labels', 'Mounting', 'Advanced']

export function paramUi(param) {
  const value = param[param.length - 1]
  return value && !Array.isArray(value) && typeof value === 'object' ? value : {}
}

export function parameterGroups(primitive) {
  const groups = new Map()
  for (const param of primitive?.params || []) {
    const name = paramUi(param).group || 'Parameters'
    if (!groups.has(name)) groups.set(name, [])
    groups.get(name).push(param)
  }
  return [...groups]
    .map(([name, params]) => ({ name, params }))
    .sort((a, b) => (GROUP_ORDER.indexOf(a.name) + 1 || 99) - (GROUP_ORDER.indexOf(b.name) + 1 || 99))
}

export function isParamDisabled(param, values) {
  const ui = paramUi(param)
  if (ui.dependsOn && !values[ui.dependsOn]) return true
  return Boolean(ui.exclusiveWith && values[ui.exclusiveWith] && !values[param[0]])
}
