import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { SETUP_GUIDANCE, cadReadinessStatus } from './setupGuidance.js'

const sourceDir = path.dirname(fileURLToPath(import.meta.url))
const readme = fs.readFileSync(path.join(sourceDir, '..', '..', 'README.md'), 'utf8')

test('first-run guidance is actionable and documented', () => {
  for (const phrase of ['hundreds of MB', 'network and write access', 'may take time']) {
    assert.match(SETUP_GUIDANCE.firstRun, new RegExp(phrase))
    assert.match(readme, new RegExp(phrase))
  }
  assert.match(SETUP_GUIDANCE.recovery, /reopen the Plugins dialog or restart OrcaSlicer/i)
  assert.match(SETUP_GUIDANCE.firstRun, /Code-mode CAD/i)
  assert.match(SETUP_GUIDANCE.firstRun, /OpenSCAD 2023/i)
  assert.match(SETUP_GUIDANCE.readiness, /probes OpenSCAD/i)
  assert.match(readme, /retry dependency setup/i)
  assert.match(readme, /Gridfinity object mode instead probes\s+OpenSCAD/i)
})

test('bridge and CAD/model readiness are distinct and dependency status is honest', () => {
  assert.equal(cadReadinessStatus('unchecked').label, 'CAD/model not checked')
  assert.match(cadReadinessStatus('unchecked').detail, /bridge readiness does not check dependencies/i)
  assert.equal(cadReadinessStatus('ready').label, 'CAD/model ready')
  assert.notEqual(cadReadinessStatus('unchecked').label, cadReadinessStatus('ready').label)
})

test('code trust and result recovery guidance is explicit', () => {
  assert.match(SETUP_GUIDANCE.trust, /trusted/i)
  assert.match(SETUP_GUIDANCE.trust, /in-process/i)
  assert.match(SETUP_GUIDANCE.trust, /no security sandbox/i)
  assert.match(SETUP_GUIDANCE.trust, /validation\/UX only, not a security boundary/i)
  assert.match(SETUP_GUIDANCE.result, /result = Box\(20, 20, 20\)/)
  assert.match(SETUP_GUIDANCE.codeRecovery, /last successful result remains unchanged/i)
  assert.match(readme, /no security sandbox/i)
  assert.match(readme, /Assign the final solid to `result`/)
})
