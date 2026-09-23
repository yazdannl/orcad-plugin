const OPENSCAD_OBJECTS = {
  gridfinity_bin: 'bin',
  gridfinity_baseplate: 'baseplate',
}

export function buildExportPayload({
  mode, command, primitive, params, code, format, tolerance, qualityProfile, filename, context = {},
}) {
  const exportFormat = command === 'plate' ? 'stl' : format
  if (mode === 'objects') {
    const object = OPENSCAD_OBJECTS[primitive]
    return {
      command, kind: 'generate', primitive, ...(object ? { object } : {}), params: { ...params }, format: exportFormat,
      tolerance, ...(qualityProfile ? { quality_profile: qualityProfile } : {}), filename, ...context,
    }
  }
  return { command, kind: 'run', code, format: exportFormat, tolerance, filename, ...context }
}

export function formatQuality(format) {
  if (format === 'step') return 'STEP: exact CAD geometry; tolerance is not used.'
  if (format === '3mf') return '3MF: mesh export; smaller tolerance gives finer tessellation.'
  return 'STL: mesh export; smaller tolerance gives finer tessellation.'
}
