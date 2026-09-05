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
