// The chunk the page fetches when the 3D view is first needed. It is built on
// its own (vite.viewport.config.js) so it stays out of the page OrcaSlicer loads
// with SetPage(), and it registers itself on window for the app to pick up.
//
// Only the classes Viewport.jsx uses are imported, so three.js tree-shakes
// instead of shipping every export.
import {
  BufferAttribute, BufferGeometry, Color, DirectionalLight, EdgesGeometry, GridHelper, Group, HemisphereLight,
  LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshBasicMaterial, MeshStandardMaterial, PerspectiveCamera,
  Raycaster, Scene, Sphere, SphereGeometry, Vector2, Vector3, WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

globalThis.__orcadThree = {
  THREE: {
    BufferAttribute, BufferGeometry, Color, DirectionalLight, EdgesGeometry, GridHelper, Group, HemisphereLight,
    LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshBasicMaterial, MeshStandardMaterial, PerspectiveCamera,
    Raycaster, Scene, Sphere, SphereGeometry, Vector2, Vector3, WebGLRenderer,
  },
  OrbitControls,
}
