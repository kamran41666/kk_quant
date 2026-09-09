import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/manual.ts', import.meta.url), 'utf8')
const withoutTypeImport = source.replace(/^import type .*$/m, '')
const compiled = ts.transpileModule(withoutTypeImport, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const manual = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const view = await readFile(new URL('../src/views/ManualTrading.vue', import.meta.url), 'utf8')

test('manual envelope and decimal formatting remain explicit', () => {
  assert.deepEqual(manual.unwrapManual({ data: { cash: '1' }, manual_execution: true }), { cash: '1' })
  assert.deepEqual(manual.unwrapManual({ cash: '1' }), { cash: '1' })
  assert.equal(manual.decimalText('100000'), '100,000.00')
  assert.equal(manual.decimalText(null), '—')
})

test('manual UI preserves the paper-only boundary and has no submit path', () => {
  assert.match(view, /用户报告的成交/)
  assert.match(view, /实盘委托已阻断/)
  assert.match(view, /系统未向券商发送任何委托/)
  assert.doesNotMatch(view, /submitOrder|sendToBroker|broker\.submit/)
})
