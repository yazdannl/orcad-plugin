export function createDraftState() {
  return {
    generatedCode: '',
    codeDraft: '',
    dirty: false,
    initialized: false,
  }
}

export function markDraftEdited(state, code) {
  state.codeDraft = code
  state.dirty = true
  state.initialized = true
}

export function receiveGeneratedCode(state, requestId, latestRequestId, code) {
  if (requestId !== latestRequestId) return false
  state.generatedCode = code
  if (!state.initialized) {
    state.codeDraft = code
    state.initialized = true
  }
  return true
}

export function replaceDraft(state) {
  if (!state.generatedCode) return false
  state.codeDraft = state.generatedCode
  state.dirty = false
  state.initialized = true
  return true
}

export function replaceDraftWith(state, code) {
  state.codeDraft = code
  state.dirty = false
  state.initialized = true
}
