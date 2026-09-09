import type { FactorGateResult } from '@/types/factors'

const STAGE_LABELS: Record<string, string> = {
  training: '训练',
  validation: '验证',
  holdout: '留出',
}

const STATUS_LABELS: Record<string, string> = {
  registered: '已登记',
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  training_passed: '训练通过',
  training_rejected: '训练拒绝',
  validation_passed: '验证通过',
  validation_rejected: '验证拒绝',
  holdout_passed: '留出通过',
  holdout_rejected: '留出拒绝',
  legacy_evaluated: '旧版已评估',
  evaluated_not_promoted: '已评估，未晋级',
}

const GATE_LABELS: Record<string, string> = {
  coverage: '有效值覆盖',
  ic_observations: 'IC 截面数',
  directional_ic: '方向调整 IC',
  baseline_correlation: '基线相关性',
  half_sign_consistency: '前后半段同向',
  directional_quantile_spread: '方向分层价差',
}

export function factorStageLabel(value: string): string {
  return STAGE_LABELS[value] ?? value
}

export function factorStatusLabel(value: string | null | undefined): string {
  if (!value) return '尚无结论'
  return STATUS_LABELS[value] ?? value
}

export function factorTone(value: string | null | undefined): 'good' | 'bad' | 'warn' | 'neutral' {
  if (!value) return 'neutral'
  if (value.endsWith('_passed')) return 'good'
  if (value.endsWith('_rejected') || value === 'failed') return 'bad'
  if (value === 'running' || value === 'queued') return 'warn'
  return 'neutral'
}

export function factorGateLabel(value: string): string {
  return GATE_LABELS[value] ?? value
}

export function factorMetric(value: unknown, kind: 'ratio' | 'number' | 'integer' = 'number'): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (kind === 'ratio') return `${(value * 100).toFixed(2)}%`
  if (kind === 'integer') return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value)
  return value.toFixed(4)
}

export function factorGateActual(gate: FactorGateResult): string {
  if (typeof gate.actual === 'boolean') return gate.actual ? '是' : '否'
  if (gate.name === 'coverage' || gate.name === 'directional_quantile_spread') {
    return factorMetric(gate.actual, 'ratio')
  }
  if (gate.name === 'ic_observations') return factorMetric(gate.actual, 'integer')
  return factorMetric(gate.actual)
}

export function shortFactorHash(value: string | null | undefined): string {
  if (!value) return '—'
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value
}

export function normalizeFactorGates(value: unknown): FactorGateResult[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is FactorGateResult => {
    if (!item || typeof item !== 'object') return false
    const gate = item as Partial<FactorGateResult>
    return typeof gate.name === 'string'
      && typeof gate.passed === 'boolean'
      && typeof gate.rule === 'string'
      && (gate.actual === null || typeof gate.actual === 'boolean'
        || (typeof gate.actual === 'number' && Number.isFinite(gate.actual)))
  })
}
