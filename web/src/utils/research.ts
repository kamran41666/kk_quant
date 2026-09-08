export interface ResearchDateRange {
  start: string
  end: string
}

function scalarValue(value: unknown): string {
  return Array.isArray(value) ? String(value[0] || '') : String(value || '')
}

/** Validate a YYYY-MM-DD calendar date without applying the host time zone. */
export function validResearchDate(value: unknown): string {
  const text = scalarValue(value)
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text)
  if (!match) return ''
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
    ? text
    : ''
}

/** Match the coverage API contract: ordered, valid dates spanning at most 3660 days. */
export function validResearchDateRange(startValue: unknown, endValue: unknown): ResearchDateRange | null {
  const start = validResearchDate(startValue)
  const end = validResearchDate(endValue)
  if (!start || !end || start > end) return null
  const span = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000
  return span <= 3660 ? { start, end } : null
}
