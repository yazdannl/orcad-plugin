import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import {
  BufferAttribute, BufferGeometry, Color, DirectionalLight, EdgesGeometry, GridHelper, Group, HemisphereLight,
  LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshStandardMaterial, PerspectiveCamera, Scene, Sphere, Vector3, WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

const VIEWS = {
  iso: { dir: [1, -1.25, 0.95], up: [0, 0, 1] },
  front: { dir: [0, -1, 0], up: [0, 0, 1] },
  right: { dir: [1, 0, 0], up: [0, 0, 1] },
  top: { dir: [0, 0, 1], up: [0, 1, 0] },
}
const EDGE_LIMIT = 250_000

function cssColor(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return new Color(value || fallback)
}

function readTheme() {
  return {
    model: cssColor('--model', '#2dd4bf'),
    edge: cssColor('--model-edge', '#0f3d3a'),
    grid: cssColor('--grid', '#243044'),
    gridStrong: cssColor('--grid-strong', '#37506e'),
  }
}

// Painter's-algorithm fallback for webviews without WebGL (e.g. blocked GPU).
function drawSoftware(ctx, width, height, camera, geometry, color, wireframe) {
  ctx.clearRect(0, 0, width, height)
  if (!geometry) return
  const pos = geometry.getAttribute('position').array
  const idx = geometry.getIndex().array
  const count = idx.length / 3
  const stride = Math.max(1, Math.ceil(count / 60000))
  const v = new Vector3(), p = [new Vector3(), new Vector3(), new Vector3()]
  camera.updateMatrixWorld()
  const light = new Vector3(0.4, -0.6, 0.8).normalize().applyQuaternion(camera.quaternion)
  const tris = []
  for (let t = 0; t < count; t += stride) {
    for (let k = 0; k < 3; k += 1) {
      const i = idx[t * 3 + k] * 3
      p[k].set(pos[i], pos[i + 1], pos[i + 2])
    }
    const n = v.subVectors(p[1], p[0]).cross(new Vector3().subVectors(p[2], p[0])).normalize()
    const shade = 0.35 + 0.65 * Math.abs(n.dot(light))
    const screen = p.map((q) => q.clone().applyMatrix4(geometry.userData.offset).project(camera))
    tris.push([screen, (screen[0].z + screen[1].z + screen[2].z) / 3, shade])
  }
  tris.sort((a, b) => b[1] - a[1])
  for (const [s, , shade] of tris) {
    ctx.beginPath()
    ctx.moveTo((s[0].x + 1) * width / 2, (1 - s[0].y) * height / 2)
    ctx.lineTo((s[1].x + 1) * width / 2, (1 - s[1].y) * height / 2)
    ctx.lineTo((s[2].x + 1) * width / 2, (1 - s[2].y) * height / 2)
    ctx.closePath()
    const c = `rgb(${color.r * 255 * shade | 0},${color.g * 255 * shade | 0},${color.b * 255 * shade | 0})`
    if (wireframe) { ctx.strokeStyle = c; ctx.stroke() } else { ctx.fillStyle = c; ctx.strokeStyle = c; ctx.fill(); ctx.stroke() }
  }
}

export const Viewport = forwardRef(function Viewport({ mesh, fitKey, wireframe, edges, grid, dimmed }, ref) {
  const host = useRef(null)
  const api = useRef(null)
  const [note, setNote] = useState('')

  useEffect(() => {
    const el = host.current
    const scene = new Scene()
    const camera = new PerspectiveCamera(35, 1, 0.1, 20000)
    camera.up.set(0, 0, 1)
    camera.position.set(120, -150, 110)
    let renderer = null
    let ctx = null
    const canvas = document.createElement('canvas')
    canvas.className = 'viewport-canvas'
    el.appendChild(canvas)
    try {
      renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    } catch {
      ctx = canvas.getContext('2d')
      setNote('WebGL is unavailable; showing a simplified software preview.')
    }
    const controls = new OrbitControls(camera, canvas)
    controls.enableDamping = Boolean(renderer)
    controls.dampingFactor = 0.12
    controls.screenSpacePanning = true
    controls.zoomToCursor = true

    const hemi = new HemisphereLight(0xffffff, 0x445066, 1.5)
    const key = new DirectionalLight(0xffffff, 1.6)
    const fill = new DirectionalLight(0xffffff, 0.6)
    key.position.set(0.5, -0.4, 1)
    fill.position.set(-1, 0.6, -0.2)
    camera.add(key, fill)
    scene.add(hemi, camera)
    const plate = new Group()
    scene.add(plate)
    const model = new Group()
    scene.add(model)

    const state = { theme: readTheme(), geometry: null, surface: null, lines: null, radius: 60, framed: 60, frame: 0,
      wireframe: false, edges: true, grid: true }

    const render = () => {
      state.frame = 0
      const moving = controls.update()
      if (renderer) renderer.render(scene, camera)
      else drawSoftware(ctx, canvas.width, canvas.height, camera, state.geometry, state.theme.model, state.wireframe)
      if (moving) request()
    }
    const request = () => { if (!state.frame) state.frame = requestAnimationFrame(render) }
    controls.addEventListener('change', request)

    const resize = () => {
      const w = Math.max(1, el.clientWidth), h = Math.max(1, el.clientHeight)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      if (renderer) renderer.setSize(w, h, false)
      else { canvas.width = w; canvas.height = h }
      request()
    }

    const buildPlate = () => {
      plate.clear()
      if (!state.grid) return
      const extent = Math.max(100, Math.ceil(state.radius * 3 / 10) * 10)
      const helper = new GridHelper(extent, extent / 10, state.theme.gridStrong, state.theme.grid)
      helper.rotation.x = Math.PI / 2
      helper.material.transparent = true
      helper.material.opacity = 0.8
      plate.add(helper)
    }

    // name=null keeps the current viewing direction and only re-frames.
    const setView = (name, radius = state.radius) => {
      const view = VIEWS[name] || VIEWS.iso
      const dir = name ? new Vector3(...view.dir).normalize() : camera.position.clone().sub(controls.target).normalize()
      const fovV = camera.fov * Math.PI / 180
      const fovH = 2 * Math.atan(Math.tan(fovV / 2) * camera.aspect)
      const distance = radius / Math.sin(Math.min(fovV, fovH) / 2) * 1.04
      const target = new Vector3(0, 0, state.center ?? 0)
      if (name) camera.up.set(...view.up)
      camera.position.copy(target).addScaledVector(dir, distance)
      controls.target.copy(target)
      camera.near = Math.max(0.05, distance / 1000)
      camera.far = distance * 50
      camera.updateProjectionMatrix()
      controls.update()
      request()
    }

    const applyMaterial = () => {
      if (state.surface) {
        state.surface.material.color.copy(state.theme.model)
        state.surface.material.wireframe = state.wireframe
      }
      if (state.lines) {
        state.lines.material.color.copy(state.theme.edge)
        state.lines.visible = state.edges && !state.wireframe
      }
      request()
    }

    const setMesh = (data, refit) => {
      model.clear()
      state.geometry?.dispose()
      state.lines?.geometry.dispose()
      state.geometry = state.surface = state.lines = null
      if (!data) { request(); return }
      const geometry = new BufferGeometry()
      geometry.setAttribute('position', new BufferAttribute(data.positions, 3))
      geometry.setIndex(new BufferAttribute(data.indices, 1))
      geometry.computeBoundingBox()
      const box = geometry.boundingBox
      const center = box.getCenter(new Vector3())
      // Sit the model on the plate, centered like OrcaSlicer's auto-arrange.
      const offset = new Vector3(-center.x, -center.y, -box.min.z)
      const surface = new Mesh(geometry, new MeshStandardMaterial({
        color: state.theme.model, roughness: 0.52, metalness: 0.08, flatShading: true,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      }))
      surface.position.copy(offset)
      model.add(surface)
      if (data.indices.length / 3 <= EDGE_LIMIT) {
        const lines = new LineSegments(new EdgesGeometry(geometry, 28),
          new LineBasicMaterial({ color: state.theme.edge, transparent: true, opacity: 0.55 }))
        lines.position.copy(offset)
        model.add(lines)
        state.lines = lines
      }
      geometry.userData.offset = new Matrix4().makeTranslation(offset.x, offset.y, offset.z)
      state.geometry = geometry
      state.surface = surface
      const size = box.getSize(new Vector3())
      const radius = new Sphere(new Vector3(), size.length() / 2).radius || 10
      // Re-frame when the model outgrows the view or shrinks to a speck.
      const reframe = refit || radius > state.framed * 1.02 || radius < state.framed * 0.5
      state.radius = radius
      state.center = size.z / 2
      if (reframe) {
        state.framed = radius
        buildPlate()
        setView(refit ? 'iso' : null, radius)
      }
      applyMaterial()
    }

    const observer = new ResizeObserver(resize)
    observer.observe(el)
    const themeObserver = new MutationObserver(() => { state.theme = readTheme(); buildPlate(); applyMaterial() })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-orca-theme', 'data-theme'] })
    const lost = (event) => { event.preventDefault(); setNote('The 3D view lost its graphics context. Switch tabs or reload the page to restore it.') }
    canvas.addEventListener('webglcontextlost', lost)
    resize()
    buildPlate()
    setView('iso')

    api.current = {
      setMesh,
      setView: (name) => setView(name),
      fit: () => setView('iso'),
      options: (next) => {
        const gridChanged = next.grid !== state.grid
        Object.assign(state, next)
        if (gridChanged) buildPlate()
        applyMaterial()
      },
    }
    return () => {
      observer.disconnect()
      themeObserver.disconnect()
      canvas.removeEventListener('webglcontextlost', lost)
      cancelAnimationFrame(state.frame)
      controls.dispose()
      state.geometry?.dispose()
      state.lines?.geometry.dispose()
      renderer?.dispose()
      canvas.remove()
      api.current = null
    }
  }, [])

  const lastFitKey = useRef(null)
  useEffect(() => {
    const refit = lastFitKey.current !== fitKey
    if (mesh) lastFitKey.current = fitKey
    api.current?.setMesh(mesh, refit)
  }, [mesh, fitKey])
  useEffect(() => { api.current?.options({ wireframe, edges, grid }) }, [wireframe, edges, grid])
  useImperativeHandle(ref, () => ({
    setView: (name) => api.current?.setView(name),
    fit: () => api.current?.fit(),
  }), [])

  return (
    <div className={`viewport${dimmed ? ' is-dimmed' : ''}`} ref={host} role="img"
      aria-label="3D model preview. Drag to rotate, right-drag to pan, scroll to zoom.">
      {/* never render a text node here: React would wipe the imperatively added canvas */}
      {note ? <div className="viewport-note">{note}</div> : null}
    </div>
  )
})
