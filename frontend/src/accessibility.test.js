import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const sourceDir = path.dirname(fileURLToPath(import.meta.url))
const app = fs.readFileSync(path.join(sourceDir, 'App.vue'), 'utf8')
const css = fs.readFileSync(path.join(sourceDir, 'style.css'), 'utf8')

test('form labels point to native controls', () => {
  const labels = [...app.matchAll(/<label\b([^>]*)>/g)].map(([, attributes]) => attributes)
  assert.ok(labels.length > 0)
  assert.ok(labels.every((attributes) => /(?:\sfor=|:for=)/.test(attributes)))
  for (const id of ['fmt', 'tol', 'objectSearch', 'objectSelect', 'code', 'example']) {
    assert.match(app, new RegExp(`<label[^>]+for="${id}"`))
    assert.match(app, new RegExp(`id="${id}"`))
  }
  assert.match(app, /param-label-\$\{param\[0\]\}/)
  assert.match(app, /param-control-\$\{param\[0\]\}/)
  assert.match(app, /param-range-\$\{param\[0\]\}/)
})

test('mode tabs and preview toggles expose keyboard-friendly semantics', () => {
  assert.match(app, /role="tablist"/)
  assert.match(app, /role="tab"[^>]+:aria-selected/)
  assert.match(app, /aria-controls="objects-panel"/)
  assert.match(app, /aria-controls="code-panel"/)
  assert.match(app, /role="tabpanel"/)
  assert.match(app, /role="switch"[^>]+:aria-checked="wireframe"/)
  assert.match(app, /role="switch"[^>]+:aria-checked="spinning"/)
  const buttons = [...app.matchAll(/<button\b([^>]*)>/g)].map(([, attributes]) => attributes)
  assert.ok(buttons.length > 0)
  assert.ok(buttons.every((attributes) => /\btype="button"/.test(attributes)))
  assert.match(app, /role="img" aria-label="Interactive 3D model preview/)
})

test('status and errors use live regions without making progress timers live', () => {
  assert.match(app, /aria-live="polite" aria-atomic="true"[^>]*>\{\{ liveStatus \}\}/)
  assert.match(app, /role="status" aria-live="polite"/)
  assert.match(app, /role="alert" aria-live="assertive"/)
  assert.doesNotMatch(app, /busy \? busyStatus : status[^<]*aria-live/)
})

test('reduced motion disables default spin and pauses inactive rendering', () => {
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/)
  assert.match(css, /animation-duration:/)
  assert.match(css, /transition-duration:/)
  assert.match(app, /matchMedia\('\(prefers-reduced-motion: reduce\)'\)/)
  assert.match(app, /initialSpinEnabled\(initialSettings\.spinning, reducedMotionQuery\?\.matches\)/)
  assert.match(app, /IntersectionObserver/)
  assert.match(app, /visibilitychange/)
  assert.match(app, /function stopRender\(\)/)
  assert.match(app, /renderer\?\.renderLists\?\.dispose\?\.\(\)/)
})
