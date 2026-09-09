export type FactorStage = 'training' | 'validation' | 'holdout'

export interface FactorExpressionSpec {
  name: string
  hypothesis: string
  direction: 1 | -1
  role: 'rank' | 'filter' | 'risk' | string
  source: string
  parent_hash?: string | null
  protocol_version?: string
  expression: Record<string, unknown>
  expression_hash?: string
  required_fields?: string[]
  lookback?: number
}

export interface FactorCandidate {
  id: string
  name: string
  expression_hash: string
  spec: FactorExpressionSpec
  status: string
  rejection_reason: string | null
  created_at: string
  updated_at: string
}

export interface FactorGateResult {
  name: string
  passed: boolean
  actual: number | boolean | null
  rule: string
}

export interface FactorExperimentResult {
  protocol_version?: string
  candidate_name?: string
  decision?: string
  coverage?: number | null
  directional_ic?: number | null
  maximum_absolute_correlation?: number | null
  most_correlated_factor?: string | null
  gate_results?: FactorGateResult[]
  limitations?: string[]
  ic_summary?: {
    ic_mean?: number | null
    icir?: number | null
    n_obs?: number | null
  }
  quantile_summary?: {
    directional_top_bottom_spread_mean?: number | null
    monotonicity_spearman?: number | null
  }
}

export interface FactorExperiment {
  id: string
  candidate_id: string
  dataset_id: string
  data_content_hash: string
  start_date: string
  end_date: string
  forward_horizon: number
  stage: FactorStage
  evaluation_policy: Record<string, unknown>
  status: string
  attempt: number
  lease_owner: string | null
  lease_until: string | null
  result: FactorExperimentResult | null
  artifact_dir: string | null
  error_code: string | null
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  updated_at: string
}

export interface FactorMemoryItem {
  experiment_id: string
  candidate_id: string
  name: string | null
  expression_hash: string | null
  dataset_id: string
  period: [string, string]
  forward_horizon: number
  decision: string
  failed_gates: string[]
  coverage: number | null
  directional_ic: number | null
  maximum_absolute_correlation: number | null
  error_code: string | null
}

export interface FactorMemory {
  protocol_version: string
  counts: {
    training_passed: number
    training_rejected: number
    legacy_evaluated: number
    failed: number
  }
  items: FactorMemoryItem[]
  limitations: string[]
}
