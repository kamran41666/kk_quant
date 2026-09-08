export interface DailyInventoryItem {
  code: string
  file_count: number
  row_count: number
  start_date: string | null
  end_date: string | null
  read_errors: string[]
}

export interface DailyInventoryResponse {
  items: DailyInventoryItem[]
  summary: {
    security_count: number
    file_count: number
    row_count: number
    start_date: string | null
    end_date: string | null
    error_count: number
  }
  source: 'local:parquet'
  coverage_verified: false
}

export interface DailyCoverageItem {
  code: string
  status: 'complete' | 'partial' | 'empty' | 'no_trading_days' | string
  expected_trading_days: number
  observed_trading_days: number
  missing_count: number
  missing_dates: string[]
  missing_dates_truncated: boolean
  unexpected_count: number
  unexpected_dates: string[]
  unexpected_dates_truncated: boolean
  duplicate_rows: number
  invalid_field_rows: number
  field_valid_counts: Record<string, number>
  first_observed: string | null
  last_observed: string | null
  read_error: string | null
  content_hash: string | null
}

export interface DailyCoverageReport {
  coverage_version: string
  market: string
  source: string
  start_date: string
  end_date: string
  expected_trading_days: number
  requested_codes: string[]
  adjust: string
  calendar_version: string | null
  calendar_source: string | null
  calendar_content_hash: string | null
  calendar_coverage_start: string | null
  calendar_coverage_end: string | null
  calendar_verified: boolean
  calendar_complete: boolean
  items: DailyCoverageItem[]
  dataset_hash: string
  complete: boolean
  coverage_hash: string
  meta?: Record<string, unknown>
}

export interface FundNavDataset {
  dataset_id: string
  market: string
  code: string
  source: string
  start_date: string
  end_date: string
  row_count: number
  content_hash: string
  dataset_version: string
  nav_rule: string
  created_at: string
  immutable: boolean
}

export interface FundNavDatasetResponse {
  data: FundNavDataset[]
  meta: {
    market: string
    immutable: boolean
    research_only: boolean
  }
}

export interface ProviderHealth {
  name?: string
  status?: string
  last_attempt_at?: string | null
  last_success_at?: string | null
  last_error?: string | null
  [key: string]: unknown
}

export interface MarketHealthResponse {
  status: string
  providers: ProviderHealth[] | Record<string, ProviderHealth>
  security_master?: Record<string, unknown>
  latest_local_date: string | null
  stock_count: number
}
