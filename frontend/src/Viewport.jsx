import { useEffect, useRef } from 'react'
import { BufferAttribute, BufferGeometry, Color, DirectionalLight, GridHelper, Group, HemisphereLight, Mesh, MeshStandardMaterial, PerspectiveCamera, Scene, WebGLRenderer, Vector3 } from 'three'
import { decodeMeshPayload } from './meshPayload.js'
import { fitCameraDistance, viewCameraState, zoomCameraDistance } from './viewerState.js'

export function Viewport({ preview, wireframe, spinning, onReady }) {
  const host = useRef(null)
  const state = useRef({})
  useEffect(() => {
    const el = host.current
    if (!el) return undefined
    const scene = new Scene(); scene.background = new Color(0x11161d)
    const root = new Group(); scene.add(root)
    const camera = new PerspectiveCamera(40, 1, 0.01, 100000)
    const renderer = new WebGLRenderer({ antialias: true, alpha: false }); renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2)); el.appendChild(renderer.domElement)
    scene.add(new HemisphereLight(0xffffff, 0x263746, 1.8)); const key = new DirectionalLight(0xffffff, 2.4); key.position.set(70, 100, 80); scene.add(key)
    const grid = new GridHelper(500, 50, 0x33404c, 0x202a34); grid.position.z = -0.01; scene.add(grid)
    const s = state.current = { scene, root, camera, renderer, mesh: null, size: 1, frame: null, drag: null, visible: true }
    const render = () => { s.frame = null; if (s.visible) renderer.render(scene, camera) }
    const request = () => { if (s.frame == null) s.frame = requestAnimationFrame(render) }
    const resize = () => { const width = Math.max(el.clientWidth, 1); const height = Math.max(el.clientHeight, 1); renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix(); request() }
    const setView = (view, distance = camera.position.length() || 3) => { const next = viewCameraState(view, distance); camera.position.fromArray(next.position); camera.up.fromArray(next.up); camera.lookAt(...next.target); request() }
    const fit = () => { const d = fitCameraDistance(s.size, camera.aspect, camera.fov); camera.position.copy(camera.position.clone().normalize().multiplyScalar(d)); camera.lookAt(0, 0, 0); request() }
    const zoom = (factor) => { camera.position.setLength(zoomCameraDistance(camera.position.length(), s.size, factor)); camera.lookAt(0, 0, 0); request() }
    const dispose = () => { if (s.mesh) { root.remove(s.mesh); s.mesh.geometry.dispose(); s.mesh.material.dispose(); s.mesh = null } }
    const setMesh = (payload) => { dispose(); const decoded = decodeMeshPayload(payload); if (!decoded) { s.size = 1; request(); return }
      const geometry = new BufferGeometry(); geometry.setAttribute('position', new BufferAttribute(decoded.vertices, 3)); if (decoded.indices) geometry.setIndex(new BufferAttribute(decoded.indices, 1)); geometry.computeVertexNormals(); geometry.computeBoundingBox()
      const center = geometry.boundingBox.getCenter(new Vector3()); const size = geometry.boundingBox.getSize(new Vector3()); s.size = Math.max(size.x, size.y, size.z, 1)
      const mesh = new Mesh(geometry, new MeshStandardMaterial({ color: 0x35c4b0, roughness: .75, metalness: .05, wireframe })); mesh.position.set(-center.x, -center.y, -center.z); root.add(mesh); s.mesh = mesh; fit()
    }
    const pointerDown = (event) => { s.drag = { x: event.clientX, y: event.clientY }; el.setPointerCapture?.(event.pointerId) }
    const pointerMove = (event) => { if (!s.drag || !s.mesh) return; root.rotation.y += (event.clientX - s.drag.x) * .01; root.rotation.x = Math.max(-1.45, Math.min(1.45, root.rotation.x + (event.clientY - s.drag.y) * .01)); s.drag = { x: event.clientX, y: event.clientY }; request() }
    const end = () => { s.drag = null }
    const wheel = (event) => { event.preventDefault(); zoom(event.deltaY > 0 ? 1.1 : .9) }
    el.addEventListener('pointerdown', pointerDown); el.addEventListener('pointermove', pointerMove); el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end); el.addEventListener('wheel', wheel, { passive: false })
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null; observer?.observe(el); window.addEventListener('resize', resize); resize(); setView('iso', 3)
    s.setMesh = setMesh
    onReady?.({ fit, zoom, view: setView, reset: () => { root.rotation.set(0, 0, 0); setView('iso', s.size ? fitCameraDistance(s.size, camera.aspect, camera.fov) : 3) } })
    return () => { observer?.disconnect(); window.removeEventListener('resize', resize); el.removeEventListener('pointerdown', pointerDown); el.removeEventListener('pointermove', pointerMove); el.removeEventListener('pointerup', end); el.removeEventListener('pointercancel', end); el.removeEventListener('wheel', wheel); if (s.frame != null) cancelAnimationFrame(s.frame); dispose(); renderer.renderLists.dispose(); renderer.dispose(); renderer.domElement.remove() }
  }, [onReady])
  useEffect(() => { const s = state.current; if (!s.mesh) return; s.mesh.material.wireframe = wireframe; if (s.frame == null) s.frame = requestAnimationFrame(() => { s.frame = null; s.renderer.render(s.scene, s.camera) }) }, [wireframe])
  useEffect(() => { const s = state.current; if (!s.root) return; if (spinning) { let frame; const tick = () => { s.root.rotation.y += .005; s.renderer.render(s.scene, s.camera); frame = requestAnimationFrame(tick) }; frame = requestAnimationFrame(tick); return () => cancelAnimationFrame(frame) } }, [spinning])
  useEffect(() => { state.current.setMesh?.(preview) }, [preview])
  return <div ref={host} className="h-full w-full" role="img" aria-label="Interactive 3D model preview. Drag to rotate and use the controls to zoom." />
}
