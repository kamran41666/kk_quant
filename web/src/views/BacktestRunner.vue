<template>
  <div class="page">
    <div class="page-header">
      <div><span class="research-eyebrow">STRATEGY LAB</span><h1>回测中心</h1><p class="page-subtitle">从数据检查到策略实验，保留每一次研究的证据。</p></div>
      <router-link class="btn-secondary" :to="{ path: '/data', query: { tab: 'coverage', start_date: config.start_date, end_date: config.end_date } }">检查研究数据 ↗</router-link>
    </div>
    <section class="run-metrics" aria-label="最近回测概况">
      <article><span>近期实验</span><strong class="numeric">{{ runs.length }}</strong><small>最近 100 条记录</small></article>
      <article><span>已完成</span><strong class="numeric">{{ runs.filter(r => r.status === 'completed').length }}</strong><small>可进入绩效比较</small></article>
      <article><span>执行中</span><strong class="numeric">{{ activeRuns }}</strong><small>{{ activeRuns ? '每 5 秒更新状态' : '等待下一次实验' }}</small></article>
      <article><span>执行口径</span><strong class="execution-label">{{ config.market === 'us-equity' ? '暂未开放' : config.market === 'cn-fund' ? '下一有效净值' : '次日开盘' }}</strong><small>{{ config.market === 'us-equity' ? '市场数据证据尚未完整' : config.market === 'cn-fund' ? '基金 NAV 日频' : 'A 股事件驱动 · T+1' }}</small></article>
    </section>

    <!-- Run Config -->
    <div class="card config-card">
      <div class="section-header"><h3>配置研究实验</h3><span class="hint">01 / CONFIGURE</span></div>
      <div class="form-row">
        <label>策略
          <select v-model="config.strategy_id">
            <option value="">-- 选择策略 --</option>
            <option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.name }}</option>
          </select>
        </label>
        <label>回测市场
          <select v-model="config.market" :disabled="!!selectedStrategyMarket">
            <option value="a-share">A 股</option>
            <option value="cn-fund">国内基金（NAV 日频）</option>
            <option value="us-equity">美股（当前不可回测）</option>
          </select>
        </label>
        <label v-if="config.market === 'cn-fund'">基金代码（逗号分隔）
          <input v-model="fundSymbolsInput" placeholder="110022,161725" />
        </label>
        <label v-if="config.market === 'cn-fund'">纸面费用率
          <input v-model.number="config.fund_fee_rate" type="number" min="0" max="0.1" step="0.0001" placeholder="0 = 不计费用" />
          <small class="field-hint">仅用于敏感性分析；0.001 = 0.1%</small>
        </label>
        <label v-if="config.market === 'a-share'">成交成本情景
          <select v-model="config.cost_scenario">
            <option v-for="scenario in costScenarios" :key="scenario.id" :value="scenario.id">
              {{ scenario.label }}
            </option>
          </select>
          <small class="field-hint">纸面假设；不会连接券商或代表真实报价</small>
        </label>
        <DateRangePicker v-model:start="config.start_date" v-model:end="config.end_date" />
        <label>初始资金 <input v-model.number="config.initial_capital" type="number" /></label>
        <label>调仓频率
          <select v-model="config.rebalance_frequency">
            <option value="">按策略建议（{{ frequencyLabel(selectedStrategy?.spec?.execution.rebalance_frequency) }}）</option>
            <option value="daily">每日</option>
            <option value="weekly">每周</option>
            <option value="monthly">每月</option>
          </select>
        </label>
        <div v-if="selectedStrategy?.spec?.parameters.length" class="runtime-params">
          <span class="runtime-title">本次运行参数</span>
          <label v-for="parameter in selectedStrategy.spec.parameters" :key="parameter.key">
            {{ parameter.label }}
            <select v-if="parameter.choices.length" v-model="parameterOverrides[parameter.key]">
              <option v-for="choice in parameter.choices" :key="String(choice)" :value="choice">{{ choice }}</option>
            </select>
            <input v-else-if="parameter.type === 'boolean'" v-model="parameterOverrides[parameter.key]" type="checkbox" />
            <input v-else-if="parameter.type === 'integer' || parameter.type === 'number'" v-model.number="parameterOverrides[parameter.key]" type="number" :min="parameter.minimum ?? undefined" :max="parameter.maximum ?? undefined" :step="parameter.type === 'integer' ? 1 : 'any'" />
            <input v-else v-model="parameterOverrides[parameter.key]" type="text" />
            <small class="field-hint">{{ parameter.description || parameter.key }}</small>
          </label>
        </div>
      </div>
      <button class="btn-primary" aria-describedby="run-block-reason" :title="runUnavailableReason" @click="runBacktest" :disabled="running || !!runUnavailableReason">{{ running ? '运行中...' : runUnavailableReason ? '当前不可从此入口回测' : '开始回测' }}</button>
      <div v-if="runUnavailableReason" id="run-block-reason" class="run-block-reason" role="status">
        <strong>为什么不能回测？</strong>
        <span>{{ runUnavailableReason }}</span>
        <router-link v-if="selectedStrategy?.spec?.extensions?.research_engine_required" :to="{ path: '/data', query: { tab: 'coverage', start_date: config.start_date, end_date: config.end_date } }">查看研究数据和固定实验入口 ↗</router-link>
      </div>
      <p v-if="config.market === 'a-share'" class="field-hint execution-note">A 股使用已归档的历史成分股票池；策略预热期也需要行情。可先在研究数据中心检查区间，实际执行仍会校验策略数据要求。</p>
      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <section id="selected-research" v-if="selectedRun?.status === 'completed' && selectedRun.data_manifest?.research_experiment === true" class="selected-research">
      <p class="selected-research-label">{{ selectedRun.data_manifest.research_label }}</p>
      <ResearchComparisonChart :run-id="selectedRun.id" :run-label="String(selectedRun.data_manifest.research_label || '')" />
    </section>

    <!-- Run History -->
    <div class="section">
      <div class="section-header"><h2>实验记录</h2><button type="button" class="btn-secondary" @click="loadData">刷新记录</button></div>
      <div class="data-table-wrap" v-if="runs.length > 0"><table class="data-table">
        <thead>
          <tr><th>研究实验</th><th>市场</th><th>状态</th><th>区间</th><th>收益</th><th>Sharpe</th><th>MaxDD</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id" @click="selectRun(r)" :class="{ selected: selectedRun?.id === r.id }">
            <td><button class="run-select" type="button" @click.stop="selectRun(r)"><span v-if="r.data_manifest?.research_label" class="research-run-label">{{ r.data_manifest.research_label }}</span><code>{{ r.id?.slice(0, 8) }}</code></button></td>
            <td>{{ marketLabel(r.market) }}</td>
            <td><span class="badge" :class="'status-' + r.status">{{ statusLabel(r.status) }}</span></td>
            <td>{{ r.start_date }} ~ {{ r.end_date }}</td>
            <td :class="r.total_return == null ? '' : r.total_return >= 0 ? 'positive' : 'negative'">{{ r.total_return == null ? '—' : (r.total_return * 100).toFixed(1) + '%' }}</td>
            <td>{{ r.sharpe_ratio?.toFixed(2) ?? '-' }}</td>
            <td>{{ r.max_drawdown == null ? '—' : (r.max_drawdown * 100).toFixed(1) + '%' }}</td>
            <td>{{ r.created_at?.slice(0, 10) }}</td>
          </tr>
        </tbody>
      </table></div>
      <div v-else class="card state-panel"><strong>开始你的第一次实验</strong><p>选择策略与研究区间，完成数据检查后运行回测。</p></div>
    </div>

    <section v-if="selectedRun" class="card evidence-card" aria-labelledby="evidence-title">
      <div class="evidence-header">
        <div><h2 id="evidence-title">回测证据</h2><p class="hint">纸面观察只允许使用与市场、策略版本和数据清单一致的已完成回测。</p></div>
        <span class="badge" :class="selectedRun.eligible_for_observation ? 'status-completed' : 'status-pending'">
          {{ selectedRun.eligible_for_observation ? '可用于观察' : '不可用于观察' }}
        </span>
      </div>
      <div class="evidence-grid">
        <span>市场<strong>{{ selectedRun.market || '旧版 A 股记录' }}</strong></span>
        <span>实际数据截止<strong>{{ selectedRun.status === 'completed' ? selectedRun.data_end || '未记录' : '待执行验证' }}</strong></span>
        <span>执行模型<strong>{{ selectedRun.execution_model || '未记录' }}</strong></span>
        <span>日历版本<strong>{{ selectedRun.calendar_version || '未记录' }}</strong></span>
        <span v-if="selectedRun.market === 'a-share'">行情覆盖<strong>{{ formatDataCoverage(selectedRun) }}</strong></span>
        <span v-if="selectedRun.market === 'a-share' && selectedRun.data_manifest?.universe_count != null">实际股票池<strong>{{ selectedRun.data_manifest.universe_count }} 只 · {{ selectedRun.data_manifest.universe_selection === 'explicit_static' ? '显式固定池' : '起始成分固定池' }}</strong></span>
        <span v-if="selectedRun.market === 'cn-fund'">NAV 覆盖<strong>{{ formatFundCoverage(selectedRun) }}</strong></span>
        <span v-if="selectedRun.market === 'cn-fund'">纸面费率<strong>{{ formatFundFeeRate(selectedRun) }}</strong></span>
      </div>
      <p v-if="selectedRun.error_message" class="run-error" role="alert">{{ selectedRun.error_message }}</p>
      <button v-if="['pending', 'running'].includes(selectedRun.status)" class="btn-secondary cancel-run" :disabled="cancelling" @click="cancelRun(selectedRun.id)">{{ cancelling ? '正在请求停止…' : '停止回测' }}</button>
      <div v-if="selectedRun.strategy_fingerprint" class="fingerprint">
        <span>策略指纹</span><code>{{ selectedRun.strategy_fingerprint }}</code>
      </div>
      <details v-if="strategyOutputs.length" class="strategy-output-list">
        <summary>策略诊断（{{ strategyOutputTotal }}）</summary>
        <table class="data-table">
          <thead><tr><th>日期</th><th>字段</th><th>值</th></tr></thead>
          <tbody><tr v-for="(item, index) in strategyOutputs" :key="`${item.date}-${item.key}-${index}`"><td>{{ String(item.date).slice(0, 10) }}</td><td>{{ item.key }}</td><td>{{ item.value }}</td></tr></tbody>
        </table>
      </details>
      <p v-if="!selectedRun.eligible_for_observation" class="hint">该记录缺少完整证据或尚未完成，不能授权跨市场或自动策略观察。</p>
    </section>

    <PerformanceAnalysisPanel :runs="runs" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch, defineAsyncComponent, nextTick } from 'vue'
import { useApi } from '@/composables/useApi'
import type { Strategy, RunSummary } from '@/types/api'
import DateRangePicker from '@/components/common/DateRangePicker.vue'
import PerformanceAnalysisPanel from '@/components/backtest/PerformanceAnalysisPanel.vue'
import { apiErrorMessage } from '@/utils/market'

const ResearchComparisonChart = defineAsyncComponent(() => import('@/components/backtest/ResearchComparisonChart.vue'))

const { api } = useApi()
const strategies = ref<Strategy[]>([])
const runs = ref<RunSummary[]>([])
const selectedRun = ref<RunSummary | null>(null)
const running = ref(false)
const error = ref('')
const cancelling = ref(false)
const activeRuns = computed(() => runs.value.filter(r => ['pending', 'running'].includes(r.status)).length)
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const fundSymbolsInput = ref('110022')
const costScenarios = ref<Array<{ id: string; label: string; slippage_rate: number }>>([])
const parameterOverrides = ref<Record<string, unknown>>({})
const strategyOutputs = ref<Array<{ date: string; key: string; value: unknown }>>([])
const strategyOutputTotal = ref(0)

const config = ref({
  strategy_id: '',
  market: 'a-share' as 'a-share' | 'cn-fund' | 'us-equity',
  symbols: [] as string[],
  start_date: '2024-01-01',
  end_date: '2024-12-31',
  initial_capital: 1_000_000,
  fund_fee_rate: 0,
  cost_scenario: 'paper_baseline_v1',
  rebalance_frequency: '' as '' | 'daily' | 'weekly' | 'monthly',
})

const selectedStrategy = computed(() => strategies.value.find((item) => item.id === config.value.strategy_id))
const selectedStrategyMarket = computed(() => selectedStrategy.value?.market || '')
const runUnavailableReason = computed(() => {
  if (config.value.market === 'us-equity') return '美股回测所需的公司行动、调整价、交易日历和费用证据尚未完整，因此保持关闭。'
  if (selectedStrategy.value?.spec?.extensions?.research_engine_required) return '该策略是冻结研究候选，必须通过专用研究引擎运行，以绑定固定数据集、公司行动账本、实验版本和审计哈希；通用回测入口会拒绝它。你可以查看下方已有实验，或前往研究数据中心运行新的版本化实验。'
  return ''
})
watch(selectedStrategyMarket, (market) => {
  if (market === 'a-share' || market === 'cn-fund' || market === 'us-equity') config.value.market = market
})
watch(selectedStrategy, (strategy) => {
  parameterOverrides.value = strategy ? { ...strategy.params } : {}
  config.value.rebalance_frequency = ''
})
watch(() => config.value.market, (market) => {
  if (market !== 'cn-fund') config.value.fund_fee_rate = 0
})
watch(selectedRun, async (run) => {
  strategyOutputs.value = []
  strategyOutputTotal.value = 0
  if (!run || run.status !== 'completed') return
  try {
    const selectedId = run.id
    const collected: Array<{ date: string; key: string; value: unknown }> = []
    let page = 1
    let total = 0
    do {
      const { data } = await api.get<{ outputs: Array<{ date: string; key: string; value: unknown }>; total: number }>(`/backtest/runs/${selectedId}/strategy-outputs`, { params: { page, page_size: 500 } })
      collected.push(...data.outputs)
      total = data.total
      page += 1
      if (data.outputs.length === 0) break
    } while (collected.length < total)
    if (selectedRun.value?.id === selectedId) {
      strategyOutputs.value = collected
      strategyOutputTotal.value = total
    }
  } catch {
    // Diagnostics are optional; the run summary remains usable without them.
  }
})

function statusLabel(s: string): string {
  const map: Record<string, string> = { pending: '等待', running: '运行中', completed: '完成', failed: '失败', cancelled: '已取消' }
  return map[s] ?? s
}

async function loadData() {
  try {
    const [sRes, rRes] = await Promise.all([
      api.get<Strategy[]>('/strategies'),
      api.get<RunSummary[]>('/backtest/runs', { params: { limit: 100 } }),
    ])
    strategies.value = sRes.data
    runs.value = rRes.data
    if (!selectedRun.value) selectedRun.value = runs.value.find(r => r.status === 'completed' && r.strategy_id === 'research-low-volatility' && r.data_manifest?.research_period === 'full' && r.data_manifest?.cost_scenario === 'baseline')
      ?? runs.value.find(r => r.status === 'completed' && r.data_manifest?.research_experiment) ?? null
    const updated = runs.value.find(r => r.id === selectedRun.value?.id)
    if (updated && (updated.status !== selectedRun.value?.status || updated.error_message !== selectedRun.value?.error_message)) selectedRun.value = updated
  } catch (e: any) {
    error.value = apiErrorMessage(e, '记录加载失败，请重试')
  } finally {
    if (pollTimer) clearTimeout(pollTimer)
    if (!disposed && activeRuns.value) pollTimer = setTimeout(loadData, 5000)
  }
}

async function cancelRun(id: string) {
  cancelling.value = true
  try {
    await api.post(`/backtest/runs/${id}/cancel`)
    await loadData()
  } catch (e) {
    error.value = apiErrorMessage(e, '停止请求失败，请重试')
  } finally {
    cancelling.value = false
  }
}

function selectRun(run: RunSummary) {
  selectedRun.value = run
  if (run.data_manifest?.research_experiment) void nextTick(() => {
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    document.getElementById('selected-research')?.scrollIntoView({ behavior, block: 'start' })
  })
}

async function loadCostScenarios() {
  try {
    const { data } = await api.get<{ data: Array<{ id: string; label: string; slippage_rate: number }> }>('/backtest/cost-scenarios')
    costScenarios.value = data.data
  } catch (e) {
    // Keep the safe baseline selectable even if the catalog request is
    // temporarily unavailable; the server still validates the id.
    costScenarios.value = [{ id: 'paper_baseline_v1', label: '纸面基线（敏感性假设）', slippage_rate: 0.001 }]
  }
}

async function runBacktest() {
  if (!config.value.strategy_id) { error.value = '请选择策略'; return }
  if (runUnavailableReason.value) { error.value = runUnavailableReason.value; return }
  try {
    running.value = true
    error.value = ''
    const symbols = config.value.market === 'cn-fund'
      ? fundSymbolsInput.value.split(',').map((item) => item.trim()).filter(Boolean)
      : []
    if (config.value.market === 'cn-fund' && symbols.length === 0) { error.value = '请输入至少一个基金代码'; return }
    const { data: submitted } = await api.post<RunSummary>('/backtest/run', {
      ...config.value,
      symbols,
      parameter_overrides: parameterOverrides.value,
      rebalance_frequency: config.value.rebalance_frequency || undefined,
    })
    selectedRun.value = submitted
    await loadData()
  } catch (e: any) {
    error.value = apiErrorMessage(e, '回测提交失败，请检查参数与数据覆盖')
  } finally {
    running.value = false
  }
}

onMounted(() => { loadData(); loadCostScenarios() })
onUnmounted(() => { disposed = true; if (pollTimer) clearTimeout(pollTimer) })

function marketLabel(market?: string): string {
  return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || 'A 股'
}

function frequencyLabel(value?: string): string {
  return ({ daily: '每日', weekly: '每周', monthly: '每月' } as Record<string, string>)[value || ''] || '未声明'
}

function formatFundFeeRate(run: RunSummary): string {
  const manifest = run.data_manifest
  if (!manifest || !Object.prototype.hasOwnProperty.call(manifest, 'fund_fee_rate')) return '未记录'
  const rate = Number(manifest.fund_fee_rate)
  return Number.isFinite(rate) ? `${(rate * 100).toFixed(3)}%` : '无效'
}

function formatFundCoverage(run: RunSummary): string {
  const datasets = run.data_manifest?.dataset_manifests
  if (!Array.isArray(datasets) || datasets.length === 0) return '未记录'
  const valid = datasets.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
  if (valid.length === 0) return '未记录'
  const rows = valid.reduce((sum, item) => {
    const count = Number(item.row_count)
    return sum + (Number.isFinite(count) && count >= 0 ? count : 0)
  }, 0)
  const hash = valid.find((item) => typeof item.content_hash === 'string')?.content_hash
  const hashLabel = typeof hash === 'string' ? ` · ${hash.slice(0, 10)}` : ''
  return `${valid.length} 只 · ${rows} 条${hashLabel}`
}

function formatDataCoverage(run: RunSummary): string {
  const coverage = run.data_manifest?.daily_data_coverage as any
  if (!coverage) return '未记录'
  const count = Array.isArray(coverage.items) ? coverage.items.length : 0
  if (!coverage.complete) return `不完整 · ${count} 只`
  const hash = typeof coverage.dataset_hash === 'string' ? coverage.dataset_hash.slice(0, 10) : ''
  return hash ? `完整 · ${count} 只 · ${hash}` : `完整 · ${count} 只`
}
</script>

<style scoped>
.research-eyebrow { color: var(--accent); font-size: 10px; letter-spacing: .18em; }
.run-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 24px; border: 1px solid var(--border); border-radius: 12px; background: linear-gradient(120deg, #152334, var(--bg-primary)); }
.run-metrics article { display: flex; flex-direction: column; gap: 5px; padding: 20px 24px; }
.run-metrics article + article { border-left: 1px solid var(--border); }
.run-metrics span, .run-metrics small { color: var(--text-secondary); font-size: 12px; }
.run-metrics small { font-size: 11px; color: var(--text-tertiary); }
.run-metrics strong { font-size: 28px; font-weight: 550; }
.run-metrics .execution-label { font-size: 20px; line-height: 42px; }
.run-select { background: transparent; color: var(--accent); padding: 4px 0; }
.research-run-label { display: block; max-width: 320px; white-space: normal; text-align: left; color: var(--text-primary); font-size: 12px; }
.selected-research { margin: 24px 0; scroll-margin-top: 76px; }
.selected-research-label { margin: 0 0 10px; color: var(--text-secondary); font-size: 13px; }
.execution-note { margin-top: 14px; max-width: 820px; }
.run-block-reason { display: grid; gap: 5px; max-width: 820px; margin-top: 12px; padding: 12px 14px; border: 1px solid rgba(245,158,11,.4); border-radius: 8px; background: rgba(245,158,11,.08); color: var(--text-secondary); font-size: 12px; line-height: 1.6; }.run-block-reason strong { color: #f59e0b; }.run-block-reason a { width: fit-content; color: var(--accent); }
.run-error { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 240px; overflow-y: auto; margin-top: 16px; padding: 12px; background: var(--up-muted); color: var(--danger); font-size: 12px; }
.cancel-run { margin-top: 16px; }
.config-card .section-header h3 { margin-bottom: 0; }
.config-card .section-header .hint { margin: 0; font-size: 10px; letter-spacing: .12em; }
.config-card { margin-bottom: 24px; }
.config-card h3 { margin-bottom: 16px; }
.form-row { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 16px; }
.form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); }
.form-row input, .form-row select { font-size: 14px; min-width: 120px; }
.runtime-params { display: flex; flex-basis: 100%; flex-wrap: wrap; gap: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); }.runtime-title { flex-basis: 100%; color: var(--text-primary); font-size: 12px; font-weight: 600; }.runtime-params input[type='checkbox'] { min-width: 18px; width: 18px; height: 18px; }
.strategy-output-list { margin-top: 14px; color: var(--text-secondary); font-size: 12px; }.strategy-output-list .data-table { margin-top: 8px; }
.field-hint { color: var(--text-muted, var(--text-secondary)); font-size: 11px; }
.btn-primary { background: var(--accent); color: #fff; padding: 10px 24px; border-radius: 6px; font-size: 14px; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.error { color: var(--red); font-size: 13px; margin-top: 12px; }
.section { margin-top: 24px; }
.data-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
.data-table th, .data-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }
.data-table th { color: var(--text-secondary); font-weight: 600; }
.data-table tbody tr { cursor: pointer; }
.data-table tbody tr:hover { background: var(--bg-secondary); }
.data-table tbody tr.selected { background: var(--bg-card); border-left: 3px solid var(--accent); }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 12px; }
.status-completed { background: rgba(34,197,94,0.15); color: var(--green); }
.status-running { background: rgba(59,130,246,0.15); color: var(--accent); }
.status-failed { background: rgba(239,68,68,0.15); color: var(--red); }
.status-pending { background: rgba(139,143,163,0.15); color: var(--text-secondary); }
.positive { color: var(--green); }
.negative { color: var(--red); }
.hint { color: var(--text-secondary); margin-top: 12px; }
.evidence-card { margin-top: 24px; }
.evidence-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.evidence-header h2 { margin: 0; }
.evidence-header .hint { margin: 6px 0 0; }
.evidence-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }
.evidence-grid span { display: flex; flex-direction: column; gap: 4px; color: var(--text-secondary); font-size: 12px; }
.evidence-grid strong { color: var(--text-primary); font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.fingerprint { display: flex; gap: 10px; align-items: center; margin-top: 16px; color: var(--text-secondary); font-size: 12px; }
.fingerprint code { color: var(--text-primary); overflow-wrap: anywhere; }
@media (max-width: 720px) { .evidence-grid, .run-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .run-metrics article { padding: 16px; } .run-metrics article:nth-child(3) { border-left: 0; } .run-metrics article:nth-child(n+3) { border-top: 1px solid var(--border); } .form-row label { max-width: 100%; } .form-row input, .form-row select { width: 100%; min-width: 0; } .runtime-params { min-width: 0; } .evidence-header { flex-wrap: wrap; } .fingerprint { align-items: flex-start; flex-direction: column; } }
</style>
