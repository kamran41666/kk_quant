import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// Exercise the production TypeScript without downloading a test runner.
const source = await readFile(new URL('../src/utils/market.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const market = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('ratio returns and quote percentage points use different units', () => {
  assert.equal(market.formatRatioPercent(0.0123), '+1.23%')
  assert.equal(market.formatPercentPoints(1.23), '+1.23%')
  assert.equal(market.formatRatioPercent(-0.1), '-10.00%')
})

test('missing and non-finite prices never become zero or a direction', () => {
  for (const value of [null, undefined, NaN, Infinity]) {
    assert.equal(market.formatMoney(value), '—')
    assert.equal(market.formatRatioPercent(value), '—')
    assert.equal(market.directionClass(value), '')
    assert.equal(market.directionLabel(value), '方向未知')
    assert.equal(market.chartValue(value), '—')
  }
  assert.equal(market.chartValue(0), '0')
  assert.equal(market.directionLabel(0), '平盘')
})

test('chronology is ascending regardless of API ordering and preserves null gaps', () => {
  const input = [{ date: '2026-09-03', close: 12 }, { date: '2026-09-01', close: 10 }, { date: '2026-09-02', close: null }]
  const output = market.chronological(input)
  assert.deepEqual(output.map(p => p.date), ['2026-09-01', '2026-09-02', '2026-09-03'])
  assert.equal(output[1].close, null)
  assert.equal(input[0].date, '2026-09-03')
  assert.deepEqual(market.chronological(output), output)
})

test('health counts observed successful providers, not configured ones', () => {
  assert.equal(market.availableProviderCount([{ status: 'ok' }, { status: 'unknown' }, { status: 'unavailable' }]), 1)
  assert.equal(market.availableProviderCount({ a: { status: 'ok' }, b: { status: 'unknown' } }), 1)
  assert.equal(market.availableProviderCount(null), 0)
})

test('API errors support FastAPI structured, string and validation details', () => {
  assert.equal(market.apiErrorMessage({ response: { data: { detail: { message: 'Source unavailable' } } } }, 'fallback'), 'Source unavailable')
  assert.equal(market.apiErrorMessage({ response: { data: { detail: 'Denied' } } }, 'fallback'), 'Denied')
  assert.equal(market.apiErrorMessage({ response: { data: { detail: [{ msg: 'Invalid' }] } } }, 'fallback'), 'fallback')
  assert.equal(market.apiErrorMessage(new Error('Offline'), 'fallback'), 'Offline')
})

test('freshness labels do not claim unknown data is realtime', () => {
  assert.equal(market.freshnessLabel('unknown'), '时效未知')
  assert.equal(market.freshnessLabel('stale'), '已过期')
  assert.equal(market.chartValue(1.23, true), '1.23%')
})
