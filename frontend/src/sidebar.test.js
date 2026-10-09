import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SIDEBAR_DEFAULT, SIDEBAR_MAX, SIDEBAR_MIN, clampSidebarWidth } from './sidebar.js'

test('the sidebar keeps the default width when nothing is stored', () => {
  assert.equal(clampSidebarWidth(undefined, 1280), SIDEBAR_DEFAULT)
  assert.equal(clampSidebarWidth(Number.NaN, 1280), SIDEBAR_DEFAULT)
})

test('the sidebar stops at its bounds', () => {
  assert.equal(clampSidebarWidth(10, 1600), SIDEBAR_MIN)
  assert.equal(clampSidebarWidth(5000, 1600), SIDEBAR_MAX)
  assert.equal(clampSidebarWidth(480.4, 1600), 480)
})

test('a narrow window caps the sidebar at 60% of it', () => {
  assert.equal(clampSidebarWidth(700, 700), 420)
  assert.equal(clampSidebarWidth(700, 400), SIDEBAR_MIN)
})
