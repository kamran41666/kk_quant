export type WorkflowStep = 'research' | 'observe' | 'plan' | 'confirm' | 'fill' | 'review' | 'next_day'

export interface WorkflowEnvelope<T> {
  data: T
  mode?: 'engineering_demo' | 'project_state' | string
  live_authorized?: boolean
  broker_connected?: boolean
}

export interface WorkflowOverview {
  mode?: string
  stats?: Record<string, number | string | null>
  candidates?: Array<Record<string, unknown>>
  releases?: Array<Record<string, unknown>>
  pilots?: Array<Record<string, unknown>>
  accounts?: Array<Record<string, unknown>>
  blockers?: Array<{ title?: string; reason?: string; status?: string; target?: string }>
  links?: Array<{ label: string; path: string; reason?: string }>
  [key: string]: unknown
}

export interface WorkflowRun {
  id: string
  revision: number
  seed?: number
  mode?: string
  current_step: WorkflowStep
  next_step?: WorkflowStep | null
  status?: string
  state?: Record<string, unknown>
  timeline?: Array<{ step: WorkflowStep | string; status?: string; label?: string; completed_at?: string; detail?: string }>
  research?: Record<string, unknown>
  observation?: Record<string, unknown>
  decision?: Record<string, unknown>
  plan?: Record<string, unknown>
  fill?: Record<string, unknown>
  review?: Record<string, unknown>
  metrics?: {
    equity_curve?: Array<Record<string, unknown>>
    [key: string]: unknown
  }
  [key: string]: unknown
}

export interface WorkflowAdvanceRequest {
  action: WorkflowStep
  fill_mode?: 'full' | 'partial' | 'unfilled'
  expected_revision: number
}
