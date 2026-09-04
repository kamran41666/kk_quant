export function formatMoney(value: number | null | undefined, compact = false): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (compact && Math.abs(value) >= 100_000_000) return `¥${(value / 100_000_000).toFixed(2)}亿`
  if (compact && Math.abs(value) >= 10_000) return `¥${(value / 10_000).toFixed(2)}万`
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

export function formatRatioPercent(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const normalized = value * 100
  return `${normalized > 0 ? '+' : ''}${normalized.toFixed(digits)}%`
}

export function formatPercentPoints(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`
}

export function directionClass(value: number | null | undefined): 'market-up' | 'market-down' | '' {
  if (value == null || !Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'market-up' : 'market-down'
}

export function directionLabel(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '方向未知'
  if (value > 0) return '上涨'
  if (value < 0) return '下跌'
  return '平盘'
}

export function compactNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)}亿`
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)}万`
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value)
}

export function freshnessLabel(value: string): string {
  const labels: Record<string, string> = {
    fresh: '实时（源时间）',
    realtime: '实时',
    delayed: '延迟',
    stale: '已过期',
    unknown: '时效未知',
  }
  return labels[value] ?? value
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message
    return fallback
  }
  return error instanceof Error ? error.message : fallback
}

export function chronological<T extends { date: string }>(points: T[]): T[] {
  return [...points].sort((a, b) => a.date.localeCompare(b.date))
}

export function availableProviderCount(providers: unknown): number {
  const values = Array.isArray(providers) ? providers : providers && typeof providers === 'object' ? Object.values(providers) : []
  return values.filter(value => value && typeof value === 'object' && value.status === 'ok').length
}

export function chartValue(value: unknown, percent = false): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return percent ? `${value.toFixed(2)}%` : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
