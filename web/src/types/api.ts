export interface Strategy {
  id: string
  name: string
  description?: string
  strategy_class: string
  params: Record<string, any>
  created_at: string
  updated_at: string
}

export interface StrategyCreate {
  name: string
  description?: string
  strategy_class: string
  params: Record<string, any>
}

export interface RunSummary {
  id: string
  strategy_id?: string
  run_type: 'backtest' | 'paper'
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  start_date?: string
  end_date?: string
  initial_capital?: number
  final_value?: number
  total_return?: number
  sharpe_ratio?: number
  max_drawdown?: number
  created_at: string
  completed_at?: string
}

export interface BacktestRunRequest {
  strategy_id: string
  start_date: string
  end_date: string
  initial_capital?: number
  benchmark?: string
  rebalance_frequency?: 'daily' | 'weekly' | 'monthly'
}

export interface PaperStatus {
  date?: string
  message?: string
  cash: number
  market_value: number
  total_value: number
  daily_return: number
  n_positions: number
  positions: PaperPosition[]
}

export interface PaperPosition {
  code: string
  shares: number
  avg_cost: number
  market_value: number
  weight: number
}

export interface EquityPoint {
  date: string
  total_value: number
  daily_return: number
  cumulative_return: number
}

export interface TradeRecord {
  trade_id: string
  code: string
  date: string
  side: 'buy' | 'sell'
  shares: number
  price: number
  amount: number
}

export interface WSMessage {
  type: 'portfolio_update' | 'order_fill' | 'signal_snapshot' | 'run_complete' | 'progress'
  data: any
}

export interface MarketHealth {
  status: 'ok' | 'unknown' | 'degraded' | 'unavailable' | string
  providers: Record<string, unknown> | unknown[]
  security_master?: Record<string, unknown>
  latest_local_date: string | null
  stock_count: number
}

export interface MarketQuote {
  code: string
  name: string
  price: number | null
  change_pct: number | null
  volume: number | null
  amount: number | null
  source: string
  as_of: string | null
  received_at: string
  freshness: 'fresh' | 'realtime' | 'delayed' | 'stale' | 'unknown' | string
  is_fallback: boolean
}

export interface QuotesResponse {
  data: MarketQuote[]
  meta: {
    requested_count?: number
    returned_count?: number
    missing_codes?: string[]
    sources?: string[]
    fallback_used?: boolean
    status?: 'ok' | 'partial' | string
    [key: string]: unknown
  }
}

export interface UniverseSecurity {
  code: string
  name: string
  exchange: 'SH' | 'SZ' | 'BJ' | string
  board: string
  listed_date?: string | null
  delisted_date?: string | null
}

export interface UniverseResponse {
  data: UniverseSecurity[]
  meta: {
    source?: string
    updated_at?: string | null
    freshness?: string
    total_count?: number
    page?: number
    page_size?: number
    returned_count?: number
    [key: string]: unknown
  }
}

export interface IndexesResponse {
  data: MarketQuote[]
  meta: {
    requested_count?: number
    returned_count?: number
    missing_codes?: string[]
    sources?: string[]
    status?: 'ok' | 'partial' | string
    [key: string]: unknown
  }
}

export interface MarketOverviewResponse {
  data: {
    quoted_count: number
    advancers: number | null
    decliners: number | null
    unchanged: number | null
    total_amount: number | null
    limit_up?: number | null
    limit_down?: number | null
  }
  meta: {
    sources?: string[]
    fallback_used?: boolean
    received_at?: string
    [key: string]: unknown
  }
}

export interface DailyPrice {
  code: string
  date: string
  open?: number | null
  high?: number | null
  low?: number | null
  close?: number | null
  volume?: number | null
  amount?: number | null
  source?: string
  adjust?: string
}

export type CandleInterval = '1d' | '1w' | '1mo'

export interface CandlePoint extends DailyPrice {
  period_start?: string
  period_end?: string
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  ema12?: number | null
  ema26?: number | null
  rsi14?: number | null
  macd?: number | null
  macd_signal?: number | null
  macd_hist?: number | null
  boll_mid?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
}

export interface CandleResponse {
  data: CandlePoint[]
  meta: {
    code: string
    interval: CandleInterval
    adjust_requested?: string
    adjust_applied?: string
    start_date?: string
    end_date?: string
    source?: string
    sources?: string[]
    returned_count?: number
    daily_source_count?: number
    indicator_fields?: string[]
    as_of?: string | null
    freshness?: string
    warning_codes?: string[]
    status?: 'ok' | 'empty' | string
    [key: string]: unknown
  }
}
