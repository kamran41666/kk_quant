import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/factors.ts', import.meta.url), 'utf8')
const withoutTypeImport = source.replace(/^import type .*$/m, '')
const compiled = ts.transpileModule(withoutTypeImport, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const factors = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('factor stage and decisions remain distinct', () => {
  assert.equal(factors.factorStageLabel('training'), '训练')
  assert.equal(factors.factorStageLabel('holdout'), '留出')
  assert.equal(factors.factorStatusLabel('training_passed'), '训练通过')
  assert.equal(factors.factorTone('training_passed'), 'good')
  assert.equal(factors.factorTone('training_rejected'), 'bad')
  assert.equal(factors.factorTone('queued'), 'warn')
})

test('factor metrics preserve missing values and ratio units', () => {
  assert.equal(factors.factorMetric(0.8234, 'ratio'), '82.34%')
  assert.equal(factors.factorMetric(-0.03125, 'ratio'), '-3.13%')
  assert.equal(factors.factorMetric(Number.NaN, 'number'), '—')
  assert.equal(factors.factorMetric(null, 'integer'), '—')
})

test('gate normalization rejects malformed API rows', () => {
  const gates = factors.normalizeFactorGates([
    { name: 'coverage', passed: true, actual: 0.8, rule: '>=0.7' },
    { name: 'bad', passed: 'yes', actual: Infinity, rule: null },
    null,
  ])
  assert.deepEqual(gates, [{ name: 'coverage', passed: true, actual: 0.8, rule: '>=0.7' }])
  assert.equal(factors.factorGateActual(gates[0]), '80.00%')
})
