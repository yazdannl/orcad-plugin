<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  BufferAttribute, BufferGeometry, DirectionalLight, HemisphereLight, Mesh,
  MeshStandardMaterial, PerspectiveCamera, Scene, Vector3, WebGLRenderer,
} from 'three'
import { EXAMPLES, PRIMS } from './primitives'
import {
  createDraftState, markDraftEdited, receiveGeneratedCode, replaceDraft,
  replaceDraftWith,
} from './codeDraft'
import { responseMatches } from './messageTracking'
import { createOperation, elapsedText, operationMatches, stageText, updateOperation } from './operationState'
import { copyPath, handoffMessage } from './handoff'
import { parameterGroups, paramUi, isParamDisabled, validationMessages } from './parameterUi'
import { buildExportPayload, formatQuality } from './exportPayload'
import {
  defaultParams, filterPrimitiveEntries, loadSettings, persistedParams,
  restoreParams, saveObjectParams, saveSettings, selectPrimitive,
} from './objectState'

const initialSettings = loadSettings()
const mode = ref('objects')
const selected = ref(PRIMS[initialSettings.selected] ? initialSettings.selected : 'gridfinity_bin')
const query = ref(typeof initialSettings.query === 'string' ? initialSettings.query : '')
const paramsByObject = reactive(initialSettings.paramsByObject && typeof initialSettings.paramsByObject === 'object'
  ? initialSettings.paramsByObject
  : {})
const params = reactive({})
const draft = reactive(createDraftState())
const example = ref('calibration_cube')
const status = ref('ready')
const previewStatus = ref('waiting for a model')
const preview = ref(null)
const stats = ref({})
const result = ref(null)
const validationErrors = reactive({})
const logLines = ref(['ready.'])
const notice = ref('')
const wireframe = ref(Boolean(initialSettings.wireframe))
const spinning = ref(initialSettings.spinning !== false)
const format = ref(['stl', 'step', '3mf'].includes(initialSettings.format) ? initialSettings.format : 'stl')
const toleranceValue = ref(Number.isFinite(initialSettings.tolerance) ? initialSettings.tolerance : 0.001)
const revision = ref(0)
let previewSequence = 0
let requestSequence = 0
let previewTimer
let hydrating = false
let latestCodeRequest = null
const activePreview = ref(null)
const activeOperation = ref(null)
const previewPending = ref(false)
const cancellation = ref(null)
const elapsedNow = ref(Date.now())
let elapsedTimer
let folderRequest = null
let noticeTimer

const busy = computed(() => Boolean(activePreview.value || activeOperation.value || previewPending.value))
const activeBusyOperation = computed(() => activeOperation.value || activePreview.value)
const busyStage = computed(() => stageText(activeBusyOperation.value?.stage, activePreview.value ? 'building preview…' : 'working…'))
const busyElapsed = computed(() => activeBusyOperation.value
  ? elapsedText(elapsedNow.value - activeBusyOperation.value.startedAt) : '')
const busyStatus = computed(() => `${busyStage.value} · ${busyElapsed.value}`)

const selectedPrim = computed(() => PRIMS[selected.value] || PRIMS.box)
const parameterSections = computed(() => parameterGroups(selectedPrim.value))
const filteredPrims = computed(() => filterPrimitiveEntries(PRIMS, query.value))
const statEntries = computed(() => Object.entries(stats.value || {}).slice(0, 6))

function log(message) {
  logLines.value.push(String(message))
  if (logLines.value.length > 60) logLines.value.shift()
}
function post(message) {
  try {
    if (!window.orca || !window.orca.postMessage) throw new Error('Orca bridge unavailable')
    window.orca.postMessage(message)
    return true
  } catch (error) {
    log(`bridge: ${error}`)
    return false
  }
}
function requestContext() {
  return { request_id: ++requestSequence, revision_id: revision.value }
}
function clearValidationErrors() {
  Object.keys(validationErrors).forEach((key) => delete validationErrors[key])
}
function setValidationErrors(message) {
  clearValidationErrors()
  Object.assign(validationErrors, validationMessages(message?.errors))
}
function cancelBackend(operation, kind) {
  if (!operation) return
  post({ command: 'cancel', target_type: kind === 'preview' ? 'preview' : 'export',
    target_request_id: operation.requestId, target_revision_id: operation.revisionId,
    ...(kind === 'preview' ? { target_seq: operation.seq } : {}) })
}
function invalidateRevision() {
  revision.value += 1
  cancelBackend(activePreview.value, 'preview')
  cancelBackend(activeOperation.value, 'operation')
  activePreview.value = null
  activeOperation.value = null
  previewPending.value = false
  cancellation.value = null
  latestCodeRequest = null
  preview.value = null
  stats.value = {}
  result.value = null
  clearValidationErrors()
  previewStatus.value = 'waiting for a model'
  if (status.value === 'building…' || status.value === 'sending…' || status.value === 'cancellation requested…') status.value = 'ready'
}
function accepts(message, expected, includeSeq = false) {
  return expected?.revisionId === revision.value && responseMatches(message, expected, includeSeq)
}
function acceptsOperation(message, expected, includeSeq = false) {
  return expected?.revisionId === revision.value && operationMatches(message, expected, includeSeq)
}
const qualityHelp = computed(() => formatQuality(format.value))
function persistSettings() {
  saveSettings({
    selected: selected.value,
    query: query.value,
    paramsByObject: persistedParams(PRIMS, paramsByObject),
    format: format.value,
    tolerance: toleranceValue.value,
    wireframe: wireframe.value,
    spinning: spinning.value,
  })
}
function saveCurrentParams(key = selected.value) {
  if (PRIMS[key]) saveObjectParams(paramsByObject, key, PRIMS[key], params)
}
function formatChanged() {
  persistSettings()
  invalidateRevision()
}
function toleranceChanged() {
  persistSettings()
  invalidateRevision()
  requestPreview()
}
function hydrateParams() {
  hydrating = true
  Object.keys(params).forEach((key) => delete params[key])
  Object.assign(params, restoreParams(selectedPrim.value, paramsByObject[selected.value]))
  hydrating = false
}
function selectObject(event) {
  selected.value = selectPrimitive(selected.value, event.target.value, PRIMS)
}
function resetDefaults() {
  saveObjectParams(paramsByObject, selected.value, selectedPrim.value, defaultParams(selectedPrim.value))
  invalidateRevision()
  hydrateParams()
  persistSettings()
  codeRequest()
  requestPreview()
}
function codeRequest() {
  const ids = requestContext()
  latestCodeRequest = { requestId: ids.request_id, revisionId: ids.revision_id }
  post({ command: 'code', kind: 'generate', primitive: selected.value, params: { ...params }, ...ids })
}
function editDraft(event) {
  invalidateRevision()
  markDraftEdited(draft, event.target.value)
}
function confirmDraftReplacement(source) {
  return !draft.dirty || window.confirm(`Replace your edited code with ${source}?`)
}
function replaceWithGenerated() {
  if (!confirmDraftReplacement('generated object code')) return
  invalidateRevision()
  replaceDraft(draft)
}
function payload(command, ids, exportFormat = format.value) {
  return buildExportPayload({
    mode: mode.value,
    command,
    primitive: selected.value,
    params,
    code: draft.codeDraft,
    format: exportFormat,
    tolerance: toleranceValue.value,
    filename: mode.value === 'objects' ? selected.value : 'model',
    context: ids || requestContext(),
  })
}
function requestPreview() {
  clearTimeout(previewTimer)
  previewPending.value = false
  if (mode.value !== 'objects') return
  previewPending.value = true
  previewTimer = setTimeout(() => {
    previewPending.value = false
    const ids = requestContext()
    const expected = createOperation('preview', { ...ids, seq: ++previewSequence })
    activePreview.value = expected
    previewStatus.value = `${busyStatus.value}`
    const message = payload('preview', ids)
    message.seq = expected.seq
    if (!post(message) && accepts(message, expected, true)) {
      activePreview.value = null
      previewStatus.value = 'preview failed'
    }
  }, 420)
}
function startOperation(command, label, exportFormat = format.value) {
  if (busy.value) return
  const ids = requestContext()
  activeOperation.value = createOperation('operation', ids)
  status.value = label
  const sent = post(payload(command, ids, exportFormat))
  if (!sent && acceptsOperation(ids, activeOperation.value)) {
    activeOperation.value = null
    status.value = 'failed'
  }
}
function cancelBusy() {
  if (previewPending.value) {
    clearTimeout(previewTimer)
    previewPending.value = false
    previewStatus.value = 'preview cancelled'
    return
  }
  if (activePreview.value) {
    cancellation.value = { ...activePreview.value, kind: 'preview' }
    cancelBackend(activePreview.value, 'preview')
    activePreview.value = null
    previewStatus.value = 'preview cancellation requested…'
    return
  }
  if (activeOperation.value) {
    cancellation.value = { ...activeOperation.value, kind: 'operation' }
    cancelBackend(activeOperation.value, 'operation')
    activeOperation.value = null
    status.value = 'cancellation requested…'
  }
}
function generate() {
  log(`export ${selected.value}`)
  startOperation('generate', 'building…')
}
function runCode() {
  if (mode.value !== 'code') {
    invalidateRevision()
    mode.value = 'code'
  }
  log('run code')
  startOperation('run', 'building…')
}
function sendPlate() {
  log('send to plate')
  startOperation('plate', 'sending…', 'stl')
}
function setMode(next) {
  if (mode.value === next) return
  invalidateRevision()
  mode.value = next
  if (next === 'code') codeRequest()
  else requestPreview()
}
function formatParam(param) {
  const [key, label, unit, type, , min, max, step] = param
  return { key, label, unit, type, min, max, step, options: Array.isArray(param[8]) ? param[8] : undefined, ...paramUi(param) }
}
function parameterDisabled(param) {
  return isParamDisabled(param, params)
}
function loadExample() {
  if (example.value === 'gridfinity_bin_2x2x6') {
    selected.value = 'gridfinity_bin'
    setMode('code')
    nextTick(codeRequest)
    return
  }
  if (!confirmDraftReplacement('this example')) return
  invalidateRevision()
  replaceDraftWith(draft, EXAMPLES[example.value] || '')
}
function notify(message) {
  notice.value = String(message)
  clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => { notice.value = '' }, 5000)
}
async function copyResultPath() {
  const path = result.value?.file
  if (!path) return
  if (await copyPath(path)) notify('path copied')
  else notify('Copy unavailable; select the path and copy it manually.')
}
function openExportsFolder() {
  const ids = requestContext()
  folderRequest = { requestId: ids.request_id, revisionId: ids.revision_id }
  if (!post({ command: 'open_exports', ...ids })) {
    folderRequest = null
    notify('Open exports folder unavailable; use the exported path below.')
  }
}
function showResult(message) {
  result.value = message
  const stateMessage = handoffMessage(message)
  if (message.ok) {
    clearValidationErrors()
    log(stateMessage || `ok ${message.filename || 'model'}`)
    if (stateMessage) notify(stateMessage)
  } else {
    setValidationErrors(message)
    log(stateMessage || message.error || 'operation failed')
    if (stateMessage) notify(stateMessage)
  }
}
function handleMessage(message) {
  if (!message) return
  if (message.type === 'progress') {
    if (acceptsOperation(message, activeOperation.value)) {
      if (message.duplicate) {
        activeOperation.value = null
        status.value = 'already running'
      } else {
        updateOperation(activeOperation.value, message)
        status.value = `${message.message || busyStage.value} · ${busyElapsed.value}`
      }
    } else if (accepts(message, activePreview.value, true)) {
      updateOperation(activePreview.value, message)
      previewStatus.value = `${message.message || busyStage.value} · ${busyElapsed.value}`
    }
    return
  }
  if (message.type === 'cancelled') {
    if (acceptsOperation(message, activeOperation.value)) {
      activeOperation.value = null
      status.value = message.pending ? 'cancelled' : 'cancel requested; result will be discarded'
    } else if (accepts(message, activePreview.value, true)) {
      activePreview.value = null
      previewStatus.value = message.pending ? 'preview cancelled' : 'preview cancellation requested'
    } else if (cancellation.value?.kind === 'operation'
        && operationMatches(message, cancellation.value)) {
      status.value = message.pending ? 'cancelled' : 'cancel requested; result will be discarded'
      cancellation.value = null
    } else if (cancellation.value?.kind === 'preview'
        && accepts(message, cancellation.value, true)) {
      previewStatus.value = message.pending ? 'preview cancelled' : 'preview cancellation requested'
      cancellation.value = null
    }
    return
  }
  if (message.type === 'code') {
    if (!accepts(message, latestCodeRequest)) return
    if (message.ok) {
      clearValidationErrors()
      receiveGeneratedCode(draft, message.request_id, latestCodeRequest.requestId, message.code)
    } else {
      setValidationErrors(message)
      log(message.error || 'code generation failed')
    }
    return
  }
  if (message.type === 'preview') {
    if (!accepts(message, activePreview.value, true)) return
    activePreview.value = null
    if (message.ok) {
      clearValidationErrors()
      preview.value = message.preview
      stats.value = message.stats || {}
      previewStatus.value = 'preview ready'
    } else {
      setValidationErrors(message)
      previewStatus.value = 'preview failed'
      log(message.error || 'preview failed')
    }
    return
  }
  if (message.type === 'folder_result') {
    if (!accepts(message, folderRequest)) return
    folderRequest = null
    if (message.ok) notify('open request sent for exports folder')
    else notify('Open exports folder unavailable; use the exported path below.')
    return
  }
  if (message.type === 'plate_result' || message.type === 'result') {
    if (!acceptsOperation(message, activeOperation.value)) return
    activeOperation.value = null
    showResult(message)
    if (message.preview) preview.value = message.preview
    if (message.stats) stats.value = message.stats
    if (message.type === 'plate_result') {
      status.value = message.open_request_sent ? 'open request sent'
        : message.export_ok ? 'exported; open request failed' : 'failed'
    } else status.value = message.ok ? 'done' : 'failed'
    return
  }
  if (message.type === 'error' || message.ok === false) {
    if (!acceptsOperation(message, activeOperation.value)) return
    activeOperation.value = null
    showResult({ ...message, ok: false })
    status.value = 'failed'
  }
}

let scene
let camera
let renderer
let mesh
let modelSize = 0
let frameId
const viewer = ref(null)
let drag

function removeMesh() {
  if (!mesh) return
  scene.remove(mesh)
  mesh.geometry.dispose()
  mesh.material.dispose()
  mesh = null
}
function fitView() {
  if (!camera || !modelSize) return
  camera.position.set(modelSize * 1.9, modelSize * 1.5, modelSize * 1.9)
  camera.lookAt(0, 0, 0)
}
function applyPreview(payload) {
  removeMesh()
  if (!payload?.tris?.length) {
    previewStatus.value = 'no mesh returned'
    return
  }
  if (!renderer) {
    previewStatus.value = 'WebGL unavailable'
    return
  }
  const geometry = new BufferGeometry()
  geometry.setAttribute('position', new BufferAttribute(new Float32Array(payload.tris), 3))
  geometry.computeVertexNormals()
  geometry.computeBoundingBox()
  const center = geometry.boundingBox.getCenter(new Vector3())
  const size = geometry.boundingBox.getSize(new Vector3())
  modelSize = Math.max(size.x, size.y, size.z, 1)
  mesh = new Mesh(geometry, new MeshStandardMaterial({ color: 0x35c4b0, roughness: 0.75, metalness: 0.05, wireframe: wireframe.value }))
  mesh.position.set(-center.x, -center.y, -center.z)
  scene.add(mesh)
  fitView()
  previewStatus.value = `mesh ready · ${payload.total || payload.tris.length / 9} triangles`
}
function resizeViewer() {
  if (!renderer || !viewer.value) return
  const width = viewer.value.clientWidth || 640
  const height = viewer.value.clientHeight || 400
  renderer.setSize(width, height, false)
  camera.aspect = width / height
  camera.updateProjectionMatrix()
}
function initViewer() {
  try {
    scene = new Scene()
    camera = new PerspectiveCamera(40, 1, 0.01, 100000)
    renderer = new WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    viewer.value.appendChild(renderer.domElement)
    scene.add(new HemisphereLight(0xffffff, 0x263746, 2))
    const light = new DirectionalLight(0xffffff, 2.4)
    light.position.set(70, 100, 80)
    scene.add(light)
    viewer.value.addEventListener('pointerdown', (event) => {
      drag = { x: event.clientX, y: event.clientY }
      viewer.value.setPointerCapture?.(event.pointerId)
      viewer.value.style.cursor = 'grabbing'
    })
    viewer.value.addEventListener('pointermove', (event) => {
      if (!drag || !mesh) return
      mesh.rotation.y += (event.clientX - drag.x) * 0.01
      mesh.rotation.x = Math.max(-1.45, Math.min(1.45, mesh.rotation.x + (event.clientY - drag.y) * 0.01))
      drag = { x: event.clientX, y: event.clientY }
    })
    ;['pointerup', 'pointercancel', 'pointerleave'].forEach((name) => viewer.value.addEventListener(name, () => {
      drag = null
      viewer.value.style.cursor = 'grab'
    }))
    viewer.value.addEventListener('wheel', (event) => {
      event.preventDefault()
      if (!camera || !modelSize) return
      camera.position.multiplyScalar(event.deltaY > 0 ? 1.1 : 0.9)
      camera.position.setLength(Math.max(modelSize * 0.35, Math.min(modelSize * 8, camera.position.length())))
      camera.lookAt(0, 0, 0)
    }, { passive: false })
    resizeViewer()
    window.addEventListener('resize', resizeViewer)
    frameId = requestAnimationFrame(renderFrame)
  } catch (error) {
    previewStatus.value = 'preview unavailable'
    log(error)
  }
}
function renderFrame() {
  frameId = requestAnimationFrame(renderFrame)
  if (mesh && spinning.value) mesh.rotation.y += 0.005
  renderer?.render(scene, camera)
}
function resetView() {
  if (!mesh) return
  mesh.rotation.set(0, 0, 0)
  fitView()
}
function toggleWireframe() {
  wireframe.value = !wireframe.value
  if (mesh) mesh.material.wireframe = wireframe.value
}

watch(query, persistSettings)
watch(selected, (value, previous) => {
  saveCurrentParams(previous)
  invalidateRevision()
  hydrateParams()
  persistSettings()
  codeRequest()
  requestPreview()
}, { flush: 'sync' })
watch(params, () => {
  if (!hydrating) {
    saveCurrentParams()
    persistSettings()
    invalidateRevision()
    codeRequest()
    requestPreview()
  }
}, { deep: true, flush: 'sync' })
watch(preview, (value) => applyPreview(value))
watch(wireframe, (value) => { if (mesh) mesh.material.wireframe = value; persistSettings() })
watch(spinning, persistSettings)
watch(busy, (value) => {
  clearInterval(elapsedTimer)
  if (value) {
    elapsedNow.value = Date.now()
    elapsedTimer = setInterval(() => { elapsedNow.value = Date.now() }, 100)
  }
})
onMounted(() => {
  hydrateParams()
  window.orca?.onMessage?.(handleMessage)
  nextTick(() => { initViewer(); codeRequest(); requestPreview() })
})
onBeforeUnmount(() => {
  clearTimeout(previewTimer)
  clearInterval(elapsedTimer)
  cancelAnimationFrame(frameId)
  window.removeEventListener('resize', resizeViewer)
  renderer?.dispose()
  clearTimeout(noticeTimer)
})
</script>

<template>
  <div class="min-h-screen bg-[var(--bg)] text-[var(--fg)]">
    <div v-if="notice" class="fixed right-3 top-3 z-10 rounded-lg border border-[var(--accent)] bg-[var(--panel)] px-3 py-2 text-xs shadow-lg" role="status">{{ notice }}</div>
    <header class="flex h-13 items-center gap-3 border-b border-[var(--line)] bg-[var(--panel)] px-4">
      <div class="font-bold tracking-wide">orcad <span class="font-normal text-[var(--accent)]">build123d</span></div>
      <div class="text-xs text-[var(--muted)]" aria-live="polite">{{ busy ? busyStatus : status }}</div>
      <button v-if="busy" class="btn btn-small" type="button" @click="cancelBusy">Cancel</button>
      <div class="flex-1" />
      <label class="sr-only" for="fmt">Export format</label>
      <select id="fmt" v-model="format" class="control w-20" title="Export format" @change="formatChanged"><option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option></select>
      <label class="sr-only" for="tol">Mesh tolerance</label>
      <input id="tol" v-model.number="toleranceValue" class="control w-20" type="number" step="0.001" min="0.0001" max="1" :title="qualityHelp" @input="toleranceChanged">
      <span class="max-w-64 text-[11px] text-[var(--muted)]" title="Export quality">{{ qualityHelp }}</span>
      <button class="btn btn-primary" :disabled="busy" @click="mode === 'objects' ? generate() : runCode()">Run / export</button>
    </header>

    <div class="mx-auto grid max-w-[1500px] gap-3.5 p-3.5 lg:grid-cols-[310px_minmax(0,1fr)]">
      <aside class="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] lg:self-start">
        <nav class="grid grid-cols-2 gap-1 border-b border-[var(--line)] p-1.5">
          <button class="tab" :class="{ active: mode === 'objects' }" @click="setMode('objects')">Objects</button>
          <button class="tab" :class="{ active: mode === 'code' }" @click="setMode('code')">Code</button>
        </nav>
        <section v-if="mode === 'objects'" class="space-y-3 p-3">
          <div>
            <div class="flex items-center justify-between gap-2"><label class="eyebrow" for="objectSearch">Model</label><button v-if="query" class="btn btn-small" type="button" @click="query = ''">Clear search</button></div>
            <input id="objectSearch" v-model="query" class="control mt-1 w-full" placeholder="Filter objects…">
          </div>
          <div v-if="!filteredPrims.length" class="rounded-lg border border-dashed border-[var(--line)] p-3 text-xs text-[var(--muted)]" role="status">
            <p>No objects match “{{ query }}”.</p><button class="btn btn-small mt-2" type="button" @click="query = ''">Clear search</button>
          </div>
          <select v-else :value="selected" class="control w-full" @change="selectObject"><option v-for="([key, prim]) in filteredPrims" :key="key" :value="key">{{ prim.label }}</option></select>
          <p class="text-xs text-[var(--muted)]">{{ selectedPrim.blurb }}</p>
          <div class="flex items-center justify-between gap-2"><div class="eyebrow">Parameters</div><button class="btn btn-small" type="button" @click="resetDefaults">Reset defaults</button></div>
          <details v-for="section in parameterSections" :key="section.name" open class="parameter-section">
            <summary class="flex cursor-pointer items-center justify-between gap-2 px-2 py-1.5 text-xs font-bold">{{ section.name }} <span class="text-[var(--muted)]">{{ section.params.length }}</span></summary>
            <div v-for="param in section.params" :key="param[0]" class="border-b border-dashed border-[var(--line)] px-2 py-2 last:border-0" :class="{ 'opacity-50': parameterDisabled(param) }">
              <div class="flex items-center justify-between gap-2"><label :for="`param-${param[0]}`" class="text-xs" :title="formatParam(param).help"><b>{{ formatParam(param).key }}</b> {{ formatParam(param).label }} <span class="text-[11px] text-[var(--muted)]">{{ formatParam(param).unit }}</span></label>
                <input v-if="param[3] === 'bool'" :id="`param-${param[0]}`" v-model="params[param[0]]" type="checkbox" class="h-4 w-4 accent-[var(--accent)]" :disabled="parameterDisabled(param)" :title="formatParam(param).help" :aria-invalid="Boolean(validationErrors[param[0]])">
                <select v-else-if="formatParam(param).options" :id="`param-${param[0]}`" v-model="params[param[0]]" class="control min-w-40" :disabled="parameterDisabled(param)" :title="formatParam(param).help" :aria-invalid="Boolean(validationErrors[param[0]])">
                  <option v-for="option in formatParam(param).options" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
                <input v-else :id="`param-${param[0]}`" v-model.number="params[param[0]]" class="control w-20 text-right" type="number" :min="param[5]" :max="param[6]" :step="param[7]" :disabled="parameterDisabled(param)" :title="formatParam(param).help" :aria-invalid="Boolean(validationErrors[param[0]])">
              </div>
              <p v-if="formatParam(param).help" class="mt-1 text-[11px] leading-4 text-[var(--muted)]">{{ formatParam(param).help }}</p>
              <p v-if="validationErrors[param[0]]" class="mt-1 text-[11px] leading-4 text-[var(--danger)]" role="alert">{{ validationErrors[param[0]] }}</p>
              <input v-if="param[3] !== 'bool' && !formatParam(param).options" v-model.number="params[param[0]]" class="mt-1.5 w-full accent-[var(--accent)]" type="range" :min="param[5]" :max="param[6]" :step="param[7]" :disabled="parameterDisabled(param)">
            </div>
          </details>
          <button class="btn btn-primary w-full" :disabled="busy" @click="generate">Generate + export</button>
        </section>
        <section v-else class="space-y-2 p-3">
          <div class="flex items-center justify-between gap-2"><label class="eyebrow" for="code">build123d code <span v-if="draft.dirty" class="text-[var(--accent)]">· edited</span></label><button v-if="draft.generatedCode" class="btn btn-small" @click="replaceWithGenerated">Replace draft</button></div>
          <textarea id="code" :value="draft.codeDraft" @input="editDraft" class="h-[410px] w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--bg)] p-2.5 font-mono text-xs leading-5 outline-none" spellcheck="false"></textarea>
          <div class="flex gap-1.5"><select v-model="example" class="control min-w-0 flex-1"><option v-for="(_, key) in EXAMPLES" :key="key" :value="key">{{ key }}</option></select><button class="btn" @click="loadExample">Load</button><button class="btn btn-primary" :disabled="busy" @click="runCode">Run</button></div>
        </section>
      </aside>

      <main class="grid min-w-0 gap-3.5">
        <section class="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
          <div class="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-2.5 py-2"><b class="text-sm">Preview</b><span class="text-xs text-[var(--muted)]">{{ previewStatus }}</span><div class="flex-1" /><button class="btn btn-small" @click="resetView">Reset</button><button class="btn btn-small" @click="toggleWireframe">Wireframe: {{ wireframe ? 'on' : 'off' }}</button><button class="btn btn-small" @click="spinning = !spinning">Spin: {{ spinning ? 'on' : 'off' }}</button><button class="btn btn-primary btn-small" :disabled="busy" title="Always exports STL for OrcaSlicer" @click="sendPlate">Send to plate</button></div>
          <div ref="viewer" class="viewer relative h-[510px] bg-[var(--bg)] max-sm:h-[330px]"><div v-if="!preview?.tris?.length" class="pointer-events-none absolute inset-0 grid place-items-center text-center text-xs text-[var(--muted)]"><span><b class="mb-1 block text-[var(--fg)]">Nothing previewed yet</b>Choose a model and adjust a parameter.</span></div></div>
          <div class="flex flex-wrap items-center gap-1.5 border-t border-[var(--line)] px-2.5 py-2 text-xs"><span v-for="([key, value]) in statEntries" :key="key" class="rounded bg-[var(--panel2)] px-1.5 py-0.5">{{ key }}: <b class="font-mono font-normal">{{ value }}</b></span><span class="flex-1" /><span class="text-[var(--muted)]">drag to rotate · wheel to zoom</span></div>
        </section>
        <div class="grid gap-3.5 md:grid-cols-2"><section class="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-3"><h2 class="mb-2 text-xs font-bold">Last export</h2><div v-if="!result" class="min-h-19 text-xs text-[var(--muted)]">Nothing exported yet.</div><div v-else class="space-y-2 text-xs"><p v-if="result.message" class="whitespace-pre-wrap" :class="result.ok ? 'text-[var(--muted)]' : 'text-[var(--danger)]'">{{ result.message }}</p><p v-if="result.error" class="whitespace-pre-wrap text-[var(--danger)]">{{ result.error }}</p><div v-if="result.file" class="grid grid-cols-[80px_1fr] gap-x-2 gap-y-1"><span class="text-[var(--muted)]">file</span><span class="break-all font-mono">{{ result.file }}</span><span class="text-[var(--muted)]">size</span><span class="font-mono">{{ result.size_bytes }} bytes</span></div><div v-if="result.file" class="flex flex-wrap gap-1.5"><button class="btn btn-small" type="button" @click="copyResultPath">Copy path</button><button class="btn btn-small" type="button" @click="openExportsFolder">Open exports folder</button></div><p v-if="result.file" class="text-[var(--muted)]">If it is not on the plate, drag the file above into OrcaSlicer Prepare.</p></div></section><section class="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-3"><h2 class="mb-2 text-xs font-bold">Activity</h2><pre class="h-19 overflow-auto whitespace-pre-wrap rounded-lg border border-[var(--line)] bg-[var(--bg)] p-2 font-mono text-[11px] leading-4">{{ logLines.join('\n') }}</pre></section></div>
      </main>
    </div>
  </div>
</template>
