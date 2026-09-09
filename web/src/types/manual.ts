export interface ManualEnvelope<T> {
  data: T
  manual_execution: true
  broker_connected: false
  user_reported_fills: true
  live_order_submission: false
  as_of: string
  evidence_status: string
}

export interface ManualAccount {
  id: string
  name: string
  currency: string
  broker_label: string
  confirmed_cash: string
  status: 'draft' | 'active' | 'reconcile' | 'suspended' | 'closed' | string
  ledger_checkpoint_hash: string
  created_at: string
  updated_at: string
}

export interface ManualPosition {
  quantity: string
  available_quantity: string
}

export interface ManualState {
  account_id: string
  cash: string
  ledger_checkpoint_hash: string
  positions: Record<string, ManualPosition>
}

export interface ManualPlanItem {
  id: string
  code: string
  side: 'buy' | 'sell'
  planned_quantity: string
  reference_price: string
  price_source: string
  status: string
  reason_codes: string
}

export interface ManualPlan {
  id: string
  execution_date: string
  execution_session: 'open' | 'close' | string
  plan_type: string
  status: string
  cash_before: string
  expected_cash_after: string
  expected_fees: string
  blocked_reason?: string | null
  items: ManualPlanItem[]
}
