export const SETUP_GUIDANCE = Object.freeze({
  firstRun: 'First Code-mode CAD run may install build123d and OCP via Orca’s bundled uv. It can download hundreds of MB, needs network and write access, and may take time. Gridfinity object mode separately requires OpenSCAD 2023 or newer.',
  recovery: 'If setup or a build fails, check network and write access, reopen the Plugins dialog or restart OrcaSlicer, then retry. The error may be setup, OpenSCAD, or code.',
  readiness: 'Bridge ready only means the host messaging API is available. CAD/model readiness is checked by a preview or export; object mode probes OpenSCAD and Code mode checks the Python CAD runtime.',
  trust: 'Editable Python/build123d code is trusted and runs in-process with no security sandbox. Import checks used for predefined objects are validation/UX only, not a security boundary.',
  result: 'Assign the final solid to result, for example: result = Box(20, 20, 20).',
  codeRecovery: 'If code fails, fix the error and Run again; the last successful result remains unchanged.',
})

export function cadReadinessStatus(state) {
  if (state === 'ready') return {
    label: 'CAD/model ready',
    detail: 'A recent preview or export completed successfully.',
  }
  if (state === 'attention') return {
    label: 'CAD/model needs attention',
    detail: 'The last CAD operation failed; check its error for setup or code recovery.',
  }
  return {
    label: 'CAD/model not checked',
    detail: 'A preview or export will check CAD setup; bridge readiness does not check dependencies.',
  }
}
