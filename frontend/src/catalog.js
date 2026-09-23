import rawCatalog from '../../openscad/catalog.json' with { type: 'json' }
import { PRIMS as legacyPrims } from './primitives.js'

const UI_KEYS = ['group', 'help']

function uiMetadata(parameter) {
  const ui = {}
  for (const key of UI_KEYS) {
    if (parameter[key] !== undefined) ui[key] = parameter[key]
  }
  if (Array.isArray(parameter.depends_on) && parameter.depends_on.length) {
    ui.dependsOn = parameter.depends_on[0]
  }
  if (Array.isArray(parameter.conflicts_with) && parameter.conflicts_with.length) {
    ui.exclusiveWith = parameter.conflicts_with[0]
  }
  return ui
}

function primitiveFromOpenScad(name, spec) {
  const params = spec.parameters.map((parameter) => {
    const type = parameter.type === 'boolean'
      ? 'bool'
      : parameter.type === 'integer' ? 'int' : 'number'
    const ui = uiMetadata(parameter)
    const tuple = [parameter.variable, parameter.label, parameter.unit || '', type, parameter.default]
    if (type !== 'bool') tuple.push(parameter.min, parameter.max, parameter.step)
    if (parameter.options) tuple.push(parameter.options)
    if (Object.keys(ui).length) tuple.push(ui)
    return tuple
  })
  return {
    label: spec.label,
    blurb: `Upstream Gridfinity Rebuilt ${name}.`,
    params,
    backendObject: name,
    warnings: spec.warnings || [],
  }
}

export const OPENSCAD_PRIMS = Object.fromEntries(
  Object.entries(rawCatalog.objects).map(([name, spec]) => [`gridfinity_${name}`, primitiveFromOpenScad(name, spec)]),
)

// The legacy objects remain available for simple build123d/code-mode examples;
// these two entries are replaced by the upstream OpenSCAD catalog above.
export const PRIMS = { ...legacyPrims, ...OPENSCAD_PRIMS }
export const OPENSCAD_QUALITY_PROFILES = rawCatalog.quality_profiles
export const OPENSCAD_SOURCE_REVISION = rawCatalog.source.revision
export const OPENSCAD_PRIMITIVE_KEYS = new Set(Object.keys(OPENSCAD_PRIMS))
