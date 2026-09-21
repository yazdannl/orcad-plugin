import assert from 'node:assert/strict'
import test from 'node:test'
import { copyPath, handoffMessage } from './handoff.js'

test('copies a path through the modern clipboard API', async () => {
  let copied = ''
  const host = { navigator: { clipboard: { writeText: async (value) => { copied = value } } } }
  assert.equal(await copyPath('/tmp/model.stl', host), true)
  assert.equal(copied, '/tmp/model.stl')
})

test('falls back to a temporary textarea when clipboard is unavailable', async () => {
  let executed = false
  const area = {
    value: '', style: {}, setAttribute() {}, select() {}, remove() {},
  }
  const host = {
    document: {
      body: { appendChild() {} },
      createElement: () => area,
      execCommand: (command) => { executed = command === 'copy'; return true },
    },
  }
  assert.equal(await copyPath('/exports/model.stl', host), true)
  assert.equal(area.value, '/exports/model.stl')
  assert.equal(executed, true)
})

test('reports handoff states without claiming import', () => {
  assert.equal(handoffMessage({ handoff_status: 'open_request_sent' }), 'open request sent')
  assert.equal(handoffMessage({ handoff_status: 'open_request_failed' }), 'export succeeded; open request failed')
  assert.equal(handoffMessage({ handoff_status: 'export_failed' }), 'export failed')
})
