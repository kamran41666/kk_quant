import type { ManualEnvelope } from '@/types/manual'

export function unwrapManual<T>(payload: ManualEnvelope<T> | T): T {
  if (payload && typeof payload === 'object' && 'data' in payload && 'manual_execution' in payload) {
    return (payload as ManualEnvelope<T>).data
  }
  return payload as T
}

export function newIdempotencyKey(prefix: string): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${random}`
}

export function decimalText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value)
  return number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 8 })
}

export function manualError(error: any): string {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.code) return detail.message ? `${detail.code}：${detail.message}` : detail.code
  return error?.message || '请求失败，请检查本地服务状态。'
}
