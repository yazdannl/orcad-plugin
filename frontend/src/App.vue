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
import { parameterGroups, paramUi, isParamDisabled } from './parameterUi'
import { buildExportPayload, formatQuality } from './exportPayload'

const mode = ref('objects')
const selected = ref('gridfinity_bin')
const query = ref('')
const params = reactive({})
const draft = reactive(createDraftState())
const example = ref('calibration_cube')
const status = ref('ready')
const previewStatus = ref('waiting for a model')
const preview = ref(null)
const stats = ref({})
const result = ref(null)
const logLines = ref(['ready.'])
const wireframe = ref(false)
const spinning = ref(true)
const format = ref('stl')
const toleranceValue = ref(0.001)
const revision = ref(0)
let previewSequence = 0
let requestSequence = 0
let previewTimer
let hydrating = false
let latestCodeRequest = null
let activePreview = null
let activeOperation = null

const selectedPrim = computed(() => PRIMS[selected.value] || PRIMS.box)
const parameterSections = computed(() => parameterGroups(selectedPrim.value))
const filteredPrims = computed(() => Object.entries(PRIMS).filter(([key, prim]) => {
  const q = query.value.trim().toLowerCase()
  return !q || key.includes(q) || prim.label.toLowerCase().includes(q)
}))
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
function invalidateRevision() {
  revision.value += 1
  activePreview = null
  activeOperation = null
  latestCodeRequest = null
  preview.value = null
  stats.value = {}
  result.value = null
  previewStatus.value = 'waiting for a model'
  if (status.value === 'building…' || status.value === 'sending…') status.value = 'ready'
}
function accepts(message, expected, includeSeq = false) {
  return expected?.revisionId === revision.value && responseMatches(message, expected, includeSeq)
}
const qualityHelp = computed(() => formatQuality(format.value))
function formatChanged() {
  invalidateRevision()
}
function toleranceChanged() {
  invalidateRevision()
  requestPreview()
}
function hydrateParams() {
  hydrating = true
  Object.keys(params).forEach((key) => delete params[key])
  selectedPrim.value.params.forEach((param) => { params[param[0]] = param[4] })
  hydrating = false
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
  if (mode.value !== 'objects') return
  previewTimer = setTimeout(() => {
    const ids = requestContext()
    const expected = { requestId: ids.request_id, revisionId: ids.revision_id, seq: ++previewSequence }
    activePreview = expected
    previewStatus.value = 'building preview…'
    const message = payload('preview', ids)
    message.seq = expected.seq
    if (!post(message) && accepts(message, expected, true)) {
      activePreview = null
      previewStatus.value = 'preview failed'
    }
  }, 420)
}
function startOperation(command, label, exportFormat = format.value) {
  const ids = requestContext()
  activeOperation = { requestId: ids.request_id, revisionId: ids.revision_id }
  status.value = label
  const sent = post(payload(command, ids, exportFormat))
  if (!sent && accepts(ids, activeOperation)) {
    activeOperation = null
    status.value = 'failed'
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
function showResult(message) {
  result.value = message
  if (message.ok) log(`ok ${message.filename || 'model'}`)
  else log(message.error || 'operation failed')
}
function handleMessage(message) {
  if (!message) return
  if (message.type === 'progress') {
    if (accepts(message, activeOperation)) {
      if (message.duplicate) {
        activeOperation = null
        status.value = 'failed'
      } else status.value = message.message || 'working…'
    }
    return
  }
  if (message.type === 'code') {
    if (!accepts(message, latestCodeRequest)) return
    if (message.ok) receiveGeneratedCode(draft, message.request_id, latestCodeRequest.requestId, message.code)
    else log(message.error || 'code generation failed')
    return
  }
  if (message.type === 'preview') {
    if (!accepts(message, activePreview, true)) return
    activePreview = null
    if (message.ok) {
      preview.value = message.preview
      stats.value = message.stats || {}
      previewStatus.value = 'preview ready'
    } else {
      previewStatus.value = 'preview failed'
      log(message.error || 'preview failed')
    }
    return
  }
  if (message.type === 'plate_result' || message.type === 'result') {
    if (!accepts(message, activeOperation)) return
    activeOperation = null
    showResult(message)
    if (message.ok) {
      preview.value = message.preview
      stats.value = message.stats || {}
      status.value = message.type === 'plate_result' ? 'sent' : 'done'
    } else status.value = 'failed'
    return
  }
  if (message.type === 'error' || message.ok === false) {
    if (!accepts(message, activeOperation)) return
    activeOperation = null
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

watch(query, () => {
  if (!filteredPrims.value.some(([key]) => key === selected.value) && filteredPrims.value[0]) {
    selected.value = filteredPrims.value[0][0]
  }
})
watch(selected, () => { invalidateRevision(); hydrateParams(); codeRequest(); requestPreview() }, { flush: 'sync' })
watch(params, () => { if (!hydrating) { invalidateRevision(); codeRequest(); requestPreview() } }, { deep: true, flush: 'sync' })
watch(preview, (value) => applyPreview(value))
watch(wireframe, (value) => { if (mesh) mesh.material.wireframe = value })
onMounted(() => {
  hydrateParams()
  window.orca?.onMessage?.(handleMessage)
  nextTick(() => { initViewer(); codeRequest(); requestPreview() })
})
onBeforeUnmount(() => {
  clearTimeout(previewTimer)
  cancelAnimationFrame(frameId)
  window.removeEventListener('resize', resizeViewer)
  renderer?.dispose()
})
</script>

<template>
  <div class="min-h-screen bg-[var(--bg)] text-[var(--fg)]">
    <header class="flex h-13 items-center gap-3 border-b border-[var(--line)] bg-[var(--panel)] px-4">
      <div class="font-bold tracking-wide">orcad <span class="font-normal text-[var(--accent)]">build123d</span></div>
      <div class="text-xs text-[var(--muted)]">{{ status }}</div>
      <div class="flex-1" />
      <label class="sr-only" for="fmt">Export format</label>
      <select id="fmt" v-model="format" class="control w-20" title="Export format" @change="formatChanged"><option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option></select>
      <label class="sr-only" for="tol">Mesh tolerance</label>
      <input id="tol" v-model.number="toleranceValue" class="control w-20" type="number" step="0.001" min="0.0001" max="1" :title="qualityHelp" @input="toleranceChanged">
      <span class="max-w-64 text-[11px] text-[var(--muted)]" title="Export quality">{{ qualityHelp }}</span>
      <button class="btn btn-primary" @click="mode === 'objects' ? generate() : runCode()">Run / export</button>
    </header>

    <div class="mx-auto grid max-w-[1500px] gap-3.5 p-3.5 lg:grid-cols-[310px_minmax(0,1fr)]">
      <aside class="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)] lg:self-start">
        <nav class="grid grid-cols-2 gap-1 border-b border-[var(--line)] p-1.5">
          <button class="tab" :class="{ active: mode === 'objects' }" @click="setMode('objects')">Objects</button>
          <button class="tab" :class="{ active: mode === 'code' }" @click="setMode('code')">Code</button>
        </nav>
        <section v-if="mode === 'objects'" class="space-y-3 p-3">
          <div><label class="eyebrow" for="objectSearch">Model</label><input id="objectSearch" v-model="query" class="control mt-1 w-full" placeholder="Filter objects…"></div>
          <select v-model="selected" class="control w-full"><option v-for="([key, prim]) in filteredPrims" :key="key" :value="key">{{ prim.label }}</option></select>
          <p class="text-xs text-[var(--muted)]">{{ selectedPrim.blurb }}</p>
          <div class="eyebrow">Parameters</div>
          <details v-for="section in parameterSections" :key="section.name" open class="parameter-section">
            <summary class="flex cursor-pointer items-center justify-between gap-2 px-2 py-1.5 text-xs font-bold">{{ section.name }} <span class="text-[var(--muted)]">{{ section.params.length }}</span></summary>
            <div v-for="param in section.params" :key="param[0]" class="border-b border-dashed border-[var(--line)] px-2 py-2 last:border-0" :class="{ 'opacity-50': parameterDisabled(param) }">
              <div class="flex items-center justify-between gap-2"><label :for="`param-${param[0]}`" class="text-xs" :title="formatParam(param).help"><b>{{ formatParam(param).key }}</b> {{ formatParam(param).label }} <span class="text-[11px] text-[var(--muted)]">{{ formatParam(param).unit }}</span></label>
                <input v-if="param[3] === 'bool'" :id="`param-${param[0]}`" v-model="params[param[0]]" type="checkbox" class="h-4 w-4 accent-[var(--accent)]" :disabled="parameterDisabled(param)" :title="formatParam(param).help">
                <select v-else-if="formatParam(param).options" :id="`param-${param[0]}`" v-model="params[param[0]]" class="control min-w-40" :disabled="parameterDisabled(param)" :title="formatParam(param).help">
                  <option v-for="option in formatParam(param).options" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
                <input v-else :id="`param-${param[0]}`" v-model.number="params[param[0]]" class="control w-20 text-right" type="number" :min="param[5]" :max="param[6]" :step="param[7]" :disabled="parameterDisabled(param)" :title="formatParam(param).help">
              </div>
              <p v-if="formatParam(param).help" class="mt-1 text-[11px] leading-4 text-[var(--muted)]">{{ formatParam(param).help }}</p>
              <input v-if="param[3] !== 'bool' && !formatParam(param).options" v-model.number="params[param[0]]" class="mt-1.5 w-full accent-[var(--accent)]" type="range" :min="param[5]" :max="param[6]" :step="param[7]" :disabled="parameterDisabled(param)">
            </div>
          </details>
          <button class="btn btn-primary w-full" @click="generate">Generate + export</button>
        </section>
        <section v-else class="space-y-2 p-3">
          <div class="flex items-center justify-between gap-2"><label class="eyebrow" for="code">build123d code <span v-if="draft.dirty" class="text-[var(--accent)]">· edited</span></label><button v-if="draft.generatedCode" class="btn btn-small" @click="replaceWithGenerated">Replace draft</button></div>
          <textarea id="code" :value="draft.codeDraft" @input="editDraft" class="h-[410px] w-full resize-y rounded-lg border border-[var(--line)] bg-[var(--bg)] p-2.5 font-mono text-xs leading-5 outline-none" spellcheck="false"></textarea>
          <div class="flex gap-1.5"><select v-model="example" class="control min-w-0 flex-1"><option v-for="(_, key) in EXAMPLES" :key="key" :value="key">{{ key }}</option></select><button class="btn" @click="loadExample">Load</button><button class="btn btn-primary" @click="runCode">Run</button></div>
        </section>
      </aside>

      <main class="grid min-w-0 gap-3.5">
        <section class="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
          <div class="flex flex-wrap items-center gap-1.5 border-b border-[var(--line)] px-2.5 py-2"><b class="text-sm">Preview</b><span class="text-xs text-[var(--muted)]">{{ previewStatus }}</span><div class="flex-1" /><button class="btn btn-small" @click="resetView">Reset</button><button class="btn btn-small" @click="toggleWireframe">Wireframe: {{ wireframe ? 'on' : 'off' }}</button><button class="btn btn-small" @click="spinning = !spinning">Spin: {{ spinning ? 'on' : 'off' }}</button><button class="btn btn-primary btn-small" title="Always exports STL for OrcaSlicer" @click="sendPlate">Send to plate</button></div>
          <div ref="viewer" class="viewer relative h-[510px] bg-[var(--bg)] max-sm:h-[330px]"><div v-if="!preview?.tris?.length" class="pointer-events-none absolute inset-0 grid place-items-center text-center text-xs text-[var(--muted)]"><span><b class="mb-1 block text-[var(--fg)]">Nothing previewed yet</b>Choose a model and adjust a parameter.</span></div></div>
          <div class="flex flex-wrap items-center gap-1.5 border-t border-[var(--line)] px-2.5 py-2 text-xs"><span v-for="([key, value]) in statEntries" :key="key" class="rounded bg-[var(--panel2)] px-1.5 py-0.5">{{ key }}: <b class="font-mono font-normal">{{ value }}</b></span><span class="flex-1" /><span class="text-[var(--muted)]">drag to rotate · wheel to zoom</span></div>
        </section>
        <div class="grid gap-3.5 md:grid-cols-2"><section class="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-3"><h2 class="mb-2 text-xs font-bold">Last export</h2><div v-if="!result" class="min-h-19 text-xs text-[var(--muted)]">Nothing exported yet.</div><div v-else-if="!result.ok" class="whitespace-pre-wrap font-mono text-xs text-[var(--danger)]">{{ result.error }}</div><div v-else class="grid grid-cols-[80px_1fr] gap-x-2 gap-y-1 text-xs"><span class="text-[var(--muted)]">file</span><span class="break-all font-mono">{{ result.file }}</span><span class="text-[var(--muted)]">size</span><span class="font-mono">{{ result.size_bytes }} bytes</span></div></section><section class="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-3"><h2 class="mb-2 text-xs font-bold">Activity</h2><pre class="h-19 overflow-auto whitespace-pre-wrap rounded-lg border border-[var(--line)] bg-[var(--bg)] p-2 font-mono text-[11px] leading-4">{{ logLines.join('\n') }}</pre></section></div>
      </main>
    </div>
  </div>
</template>
