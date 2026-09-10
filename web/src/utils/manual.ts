import type { ManualEnvelope } from '@/types/manual'

export function unwrapManual<T>(payload: ManualEnvelope<T> | T): T {
  if (!payload || typeof payload !== 'object' || !('data' in payload)) throw new Error('人工执行接口缺少安全响应封装。')
  const envelope = payload as ManualEnvelope<T>
  if (envelope.manual_execution !== true || envelope.broker_connected !== false || envelope.user_reported_fills !== true || envelope.live_order_submission !== false) {
    throw new Error('人工执行接口安全边界不完整，已拒绝处理响应。')
  }
  return envelope.data
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
