import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const dir = path.dirname(fileURLToPath(import.meta.url))
const app = fs.readFileSync(path.join(dir, 'App.jsx'), 'utf8')
const css = fs.readFileSync(path.join(dir, 'style.css'), 'utf8')
test('React app includes labelled controls and live status', () => {
  assert.match(app, /aria-label="Export format"/); assert.match(app, /htmlFor="search"/)
  assert.match(app, /<Viewport/); assert.match(app, /aria-live="polite"/)
  assert.match(app, /role="alert"/); assert.match(app, /aria-pressed={wireframe}/); assert.match(app, /aria-pressed={spinning}/)
})
test('embedded layout supports reduced motion and small panels', () => {
  assert.match(css, /prefers-reduced-motion/); assert.match(css, /max-width: 800px/); assert.match(css, /focus-visible/)
})
