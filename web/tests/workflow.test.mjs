import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const view = await readFile(new URL('../src/views/DailyWorkflow.vue', import.meta.url), 'utf8')
const router = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8')

test('daily workflow is an API-backed, explicitly non-live surface', () => {
  assert.match(router, /path: '\/daily-workflow'/)
  assert.match(view, /\/daily-workflow\/overview/)
  assert.match(view, /\/daily-workflow\/runs/)
  assert.match(view, /live_authorized !== false/)
  assert.match(view, /broker_connected !== false/)
  assert.match(view, /工程演示结果/)
  assert.match(view, /fillMode/)
})
