import assert from 'node:assert/strict'
import test from 'node:test'
import { bridgeReadiness, normalizeBridgeMessage, recoverPostFailure } from './bridgeProtocol.js'
import {
  createDraftState, markDraftEdited, receiveGeneratedCode,
} from './codeDraft.js'
import { buildExportPayload } from './exportPayload.js'
import { responseMatches } from './messageTracking.js'
import {
  defaultParams, filterPrimitiveEntries, restoreParams, saveObjectParams, selectPrimitive,
} from './objectState.js'
import { validationMessages } from './parameterUi.js'
import { PRIMS } from './primitives.js'

// Lightweight React state harness: it uses the same extracted routing helpers and a
// synchronous mock of the Orca bridge, without importing Vue, WebGL, or OrcaSlicer.
class MockOrcaBridge {
  constructor() {
    this.messages = []
    this.listener = null
    this.failNextPost = false
  }

  onMessage(listener) {
    this.listener = listener
    return () => {
      if (this.listener === listener) this.listener = null
    }
  }

  postMessage(message) {
    if (this.failNextPost) {
      this.failNextPost = false
      throw new Error('mock bridge post failed')
    }
    this.messages.push({ ...message })
  }

  deliver(message) {
    this.listener?.(message)
  }
}

function createAppHarness(bridge = new MockOrcaBridge()) {
  const state = {
    mode: 'objects',
    selected: 'box',
    query: '',
    paramsByObject: {},
    params: defaultParams(PRIMS.box),
    format: 'stl',
    revision: 0,
    busy: false,
    bridgeState: 'initializing',
    preview: null,
    previewStatus: 'waiting for a model',
    previewError: null,
    validationErrors: {},
    draft: createDraftState(),
    activePreview: null,
    activeOperation: null,
  }
  let requestId = 0
  let previewSequence = 0
  let latestCodeRequest = null
  let bridgeCleanup = null

  const requestContext = () => ({ request_id: ++requestId, revision_id: state.revision })

  function registerBridge() {
    bridgeCleanup?.()
    bridgeCleanup = bridge.onMessage(handleMessage)
    state.bridgeState = 'ready'
  }

  function failPost(kind) {
    state.bridgeState = 'unavailable'
    state.busy = false
    if (kind === 'preview') {
      state.activePreview = null
      state.previewError = recoverPostFailure('preview')
      state.previewStatus = state.previewError.status
    } else {
      state.activeOperation = null
    }
  }

  function post(message, kind) {
    if (state.bridgeState !== 'ready' || bridgeReadiness(bridge) !== 'ready') {
      failPost(kind)
      return false
    }
    try {
      bridge.postMessage(message)
      state.bridgeState = 'ready'
      return true
    } catch {
      failPost(kind)
      return false
    }
  }

  function invalidateRevision() {
    state.revision += 1
    state.activePreview = null
    state.activeOperation = null
    state.busy = false
    state.validationErrors = {}
  }

  function requestCode() {
    const context = requestContext()
    latestCodeRequest = { requestId: context.request_id, revisionId: context.revision_id }
    const message = { command: 'code', kind: 'generate', primitive: state.selected,
      params: { ...state.params }, ...context }
    post(message, 'code')
    return message
  }

  function requestPreview() {
    const context = requestContext()
    const expected = { requestId: context.request_id, revisionId: context.revision_id,
      seq: ++previewSequence }
    state.activePreview = expected
    state.busy = true
    state.previewStatus = 'building preview…'
    const message = buildExportPayload({
      mode: 'objects', command: 'preview', primitive: state.selected, params: state.params,
      format: state.format, tolerance: 0.001, filename: state.selected, context,
    })
    message.seq = expected.seq
    post(message, 'preview')
    return message
  }

  function requestExport() {
    const context = requestContext()
    state.activeOperation = { requestId: context.request_id, revisionId: context.revision_id }
    state.busy = true
    const message = buildExportPayload({
      mode: state.mode, command: 'generate', primitive: state.selected, params: state.params,
      format: state.format, tolerance: 0.001, filename: state.selected, context,
    })
    post(message, 'operation')
    return message
  }

  function setMode(next) {
    if (state.mode === next) return
    invalidateRevision()
    state.mode = next
    if (next === 'code') requestCode()
    else requestPreview()
  }

  function setQuery(query) {
    state.query = query
  }

  function selectObject(next) {
    saveObjectParams(state.paramsByObject, state.selected, PRIMS[state.selected], state.params)
    state.selected = selectPrimitive(state.selected, next, PRIMS)
    state.params = restoreParams(PRIMS[state.selected], state.paramsByObject[state.selected])
  }

  function setParameter(key, value) {
    state.params[key] = value
    saveObjectParams(state.paramsByObject, state.selected, PRIMS[state.selected], state.params)
  }

  function editDraft(code) {
    invalidateRevision()
    markDraftEdited(state.draft, code)
  }

  function handleMessage(rawMessage) {
    const parsed = normalizeBridgeMessage(rawMessage)
    if (!parsed.ok) return
    const message = parsed.message
    state.bridgeState = 'ready'
    if (message.type === 'code') {
      if (!responseMatches(message, latestCodeRequest)) return
      if (message.ok) receiveGeneratedCode(
        state.draft, message.request_id, latestCodeRequest.requestId, message.code,
      )
      return
    }
    if (message.type === 'preview') {
      if (!responseMatches(message, state.activePreview, true)) return
      state.activePreview = null
      state.busy = false
      if (message.ok) {
        state.preview = message.preview
        state.previewError = null
        state.validationErrors = {}
        state.previewStatus = 'preview ready'
      } else {
        state.previewError = { message: message.error }
        state.validationErrors = validationMessages(message.errors)
        state.previewStatus = 'preview failed; showing last preview'
      }
      return
    }
    if (message.type === 'result' && responseMatches(message, state.activeOperation)) {
      state.activeOperation = null
      state.busy = false
    }
  }

  registerBridge()
  return {
    bridge,
    state,
    handleMessage,
    requestCode,
    requestPreview,
    requestExport,
    setMode,
    setQuery,
    selectObject,
    setParameter,
    editDraft,
    retryPreview() {
      registerBridge()
      return requestPreview()
    },
    filteredObjects() {
      return filterPrimitiveEntries(PRIMS, state.query).map(([key]) => key)
    },
  }
}

test('edited drafts survive an Objects/Code mode change and later code response', () => {
  const harness = createAppHarness()
  const firstCode = harness.requestCode()
  harness.bridge.deliver({
    type: 'code', request_id: firstCode.request_id, revision_id: firstCode.revision_id,
    ok: true, code: 'generated one',
  })
  harness.editDraft('hand edited code')

  harness.setMode('code')
  const newerCode = harness.bridge.messages.at(-1)
  harness.bridge.deliver({
    type: 'code', request_id: newerCode.request_id, revision_id: newerCode.revision_id,
    ok: true, code: 'generated two',
  })

  assert.equal(harness.state.mode, 'code')
  assert.equal(harness.state.draft.codeDraft, 'hand edited code')
  assert.equal(harness.state.draft.generatedCode, 'generated two')
  assert.equal(harness.state.draft.dirty, true)
})

test('an Objects export sends the selected format through the mocked bridge', () => {
  const harness = createAppHarness()
  harness.state.format = '3mf'
  const request = harness.requestExport()

  assert.equal(request.command, 'generate')
  assert.equal(request.format, '3mf')
  assert.equal(harness.bridge.messages.at(-1).format, '3mf')
})

test('validation errors end preview busy state and remain visible on the failed request', () => {
  const harness = createAppHarness()
  const request = harness.requestPreview()
  harness.bridge.deliver({
    type: 'preview', request_id: request.request_id, revision_id: request.revision_id,
    seq: request.seq, ok: false, error: 'invalid dimensions',
    errors: [{ field: 'L', message: 'Length is too short' }],
  })

  assert.equal(harness.state.busy, false)
  assert.equal(harness.state.activePreview, null)
  assert.equal(harness.state.previewError.message, 'invalid dimensions')
  assert.deepEqual(harness.state.validationErrors, { L: 'Length is too short' })
  assert.equal(harness.state.previewStatus, 'preview failed; showing last preview')
})

test('a stale preview response cannot replace the newest preview', () => {
  const harness = createAppHarness()
  const older = harness.requestPreview()
  const newer = harness.requestPreview()

  harness.bridge.deliver({
    type: 'preview', request_id: older.request_id, revision_id: older.revision_id,
    seq: older.seq, ok: true, preview: { id: 'old' },
  })
  assert.equal(harness.state.preview, null)
  assert.equal(harness.state.busy, true)

  harness.bridge.deliver({
    type: 'preview', request_id: newer.request_id, revision_id: newer.revision_id,
    seq: newer.seq, ok: true, preview: { id: 'new' },
  })
  assert.deepEqual(harness.state.preview, { id: 'new' })
  assert.equal(harness.state.busy, false)
})

test('a synchronous bridge failure clears preview busy state and retry recovers', () => {
  const harness = createAppHarness()
  harness.bridge.failNextPost = true
  harness.requestPreview()

  assert.equal(harness.state.busy, false)
  assert.equal(harness.state.bridgeState, 'unavailable')
  assert.equal(harness.state.previewStatus, 'preview unavailable; retry')

  const retry = harness.retryPreview()
  harness.bridge.deliver({
    type: 'preview', request_id: retry.request_id, revision_id: retry.revision_id,
    seq: retry.seq, ok: true, preview: { id: 'recovered' },
  })
  assert.equal(harness.state.bridgeState, 'ready')
  assert.equal(harness.state.busy, false)
  assert.deepEqual(harness.state.preview, { id: 'recovered' })
})

test('search filters the list without discarding settings for either object', () => {
  const harness = createAppHarness()
  harness.setParameter('L', 42)
  harness.setQuery('cylinder')
  assert.deepEqual(harness.filteredObjects(), ['cylinder'])
  assert.equal(harness.state.selected, 'box')

  harness.selectObject('cylinder')
  harness.setParameter('R', 18)
  harness.setQuery('')
  harness.selectObject('box')
  assert.equal(harness.state.params.L, 42)
  harness.selectObject('cylinder')
  assert.equal(harness.state.params.R, 18)
})
