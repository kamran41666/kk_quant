import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/research.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText
const research = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

for (const timezone of ['Asia/Shanghai', 'UTC']) {
  test(`research dates are calendar-stable in ${timezone}`, () => {
    const previous = process.env.TZ
    process.env.TZ = timezone
    try {
      assert.equal(research.validResearchDate('2026-09-08'), '2026-09-08')
      assert.equal(research.validResearchDate('2024-02-29'), '2024-02-29')
      assert.deepEqual(research.validResearchDateRange('2024-02-29', '2026-09-08'), { start: '2024-02-29', end: '2026-09-08' })
    } finally {
      if (previous === undefined) delete process.env.TZ
      else process.env.TZ = previous
    }
  })
}

test('research dates reject invalid days and non-leap February 29', () => {
  for (const value of ['2023-02-29', '2026-02-30', '2026-13-01', '2026-00-01', '2026-09-31', '09/08/2026']) {
    assert.equal(research.validResearchDate(value), '')
  }
})

test('research ranges reject reversed dates and spans beyond the API limit', () => {
  assert.equal(research.validResearchDateRange('2026-09-08', '2026-09-07'), null)
  assert.deepEqual(research.validResearchDateRange('2016-01-01', '2026-01-08'), { start: '2016-01-01', end: '2026-01-08' })
  assert.equal(research.validResearchDateRange('2016-01-01', '2026-01-09'), null)
})
