<template>
  <div class="page">
    <h1>回测中心</h1>

    <!-- Run Config -->
    <div class="card config-card">
      <h3>运行回测</h3>
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
            <option value="daily">每日</option>
            <option value="weekly">每周</option>
            <option value="monthly">每月</option>
          </select>
        </label>
      </div>
      <button class="btn-primary" @click="runBacktest" :disabled="running">{{ running ? '运行中...' : '开始回测' }}</button>
      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <!-- Run History -->
    <div class="section">
      <h2>历史记录</h2>
      <table class="data-table" v-if="runs.length > 0">
        <thead>
          <tr><th>ID</th><th>市场</th><th>状态</th><th>区间</th><th>收益</th><th>Sharpe</th><th>MaxDD</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id" @click="selectedRun = r" :class="{ selected: selectedRun?.id === r.id }">
            <td><code>{{ r.id?.slice(0, 8) }}</code></td>
            <td>{{ marketLabel(r.market) }}</td>
            <td><span class="badge" :class="'status-' + r.status">{{ statusLabel(r.status) }}</span></td>
            <td>{{ r.start_date }} ~ {{ r.end_date }}</td>
            <td :class="(r.total_return ?? 0) >= 0 ? 'positive' : 'negative'">{{ ((r.total_return ?? 0) * 100).toFixed(1) }}%</td>
            <td>{{ r.sharpe_ratio?.toFixed(2) ?? '-' }}</td>
            <td>{{ r.max_drawdown ? (r.max_drawdown * 100).toFixed(1) + '%' : '-' }}</td>
            <td>{{ r.created_at?.slice(0, 10) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">暂无回测记录</p>
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
        <span>数据截止<strong>{{ selectedRun.data_end || selectedRun.end_date || '-' }}</strong></span>
        <span>执行模型<strong>{{ selectedRun.execution_model || '未记录' }}</strong></span>
        <span>日历版本<strong>{{ selectedRun.calendar_version || '未记录' }}</strong></span>
        <span v-if="selectedRun.market === 'a-share'">行情覆盖<strong>{{ formatDataCoverage(selectedRun) }}</strong></span>
        <span v-if="selectedRun.market === 'cn-fund'">NAV 覆盖<strong>{{ formatFundCoverage(selectedRun) }}</strong></span>
        <span v-if="selectedRun.market === 'cn-fund'">纸面费率<strong>{{ formatFundFeeRate(selectedRun) }}</strong></span>
      </div>
      <div v-if="selectedRun.strategy_fingerprint" class="fingerprint">
        <span>策略指纹</span><code>{{ selectedRun.strategy_fingerprint }}</code>
      </div>
      <p v-if="!selectedRun.eligible_for_observation" class="hint">该记录缺少完整证据或尚未完成，不能授权跨市场或自动策略观察。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { useApi } from '@/composables/useApi'
import type { Strategy, RunSummary } from '@/types/api'
import DateRangePicker from '@/components/common/DateRangePicker.vue'

const { api } = useApi()
const strategies = ref<Strategy[]>([])
const runs = ref<RunSummary[]>([])
const selectedRun = ref<RunSummary | null>(null)
const running = ref(false)
const error = ref('')
const fundSymbolsInput = ref('110022')
const costScenarios = ref<Array<{ id: string; label: string; slippage_rate: number }>>([])

const config = ref({
  strategy_id: '',
  market: 'a-share' as 'a-share' | 'cn-fund' | 'us-equity',
  symbols: [] as string[],
  start_date: '2024-01-01',
  end_date: '2024-12-31',
  initial_capital: 1_000_000,
  fund_fee_rate: 0,
  cost_scenario: 'paper_baseline_v1',
  rebalance_frequency: 'weekly',
})

const selectedStrategyMarket = computed(() => strategies.value.find((item) => item.id === config.value.strategy_id)?.market || '')
watch(selectedStrategyMarket, (market) => {
  if (market === 'a-share' || market === 'cn-fund' || market === 'us-equity') config.value.market = market
})
watch(() => config.value.market, (market) => {
  if (market !== 'cn-fund') config.value.fund_fee_rate = 0
})

function statusLabel(s: string): string {
  const map: Record<string, string> = { pending: '等待', running: '运行中', completed: '完成', failed: '失败' }
  return map[s] ?? s
}

async function loadData() {
  try {
    const [sRes, rRes] = await Promise.all([
      api.get<Strategy[]>('/strategies'),
      api.get<RunSummary[]>('/backtest/runs', { params: { limit: 30 } }),
    ])
    strategies.value = sRes.data
    runs.value = rRes.data
  } catch (e: any) {
    error.value = '加载失败: ' + e.message
  }
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
  try {
    running.value = true
    error.value = ''
    const symbols = config.value.market === 'cn-fund'
      ? fundSymbolsInput.value.split(',').map((item) => item.trim()).filter(Boolean)
      : []
    if (config.value.market === 'cn-fund' && symbols.length === 0) { error.value = '请输入至少一个基金代码'; return }
    await api.post('/backtest/run', { ...config.value, symbols })
    await loadData()
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    running.value = false
  }
}

onMounted(() => { loadData(); loadCostScenarios() })

function marketLabel(market?: string): string {
  return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || 'A 股'
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
.config-card { margin-bottom: 24px; }
.config-card h3 { margin-bottom: 16px; }
.form-row { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 16px; }
.form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); }
.form-row input, .form-row select { font-size: 14px; min-width: 120px; }
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
@media (max-width: 720px) { .evidence-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
