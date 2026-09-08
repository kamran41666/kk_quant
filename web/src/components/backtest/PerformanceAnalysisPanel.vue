<template>
  <section class="performance-analysis" aria-labelledby="performance-title">
    <div class="analysis-title-row">
      <div>
        <p class="eyebrow">回测结果</p>
        <h2 id="performance-title">绩效分析</h2>
      </div>
      <span v-if="selectedRunId" class="selected-state">已选择 1 组</span>
    </div>

    <div class="card selector-card">
      <label>查看回测绩效
        <select v-model="selectedRunId" @change="loadMetrics">
          <option value="">-- 选择已完成的回测 --</option>
          <option v-for="r in completedRuns" :key="r.id" :value="r.id">
            {{ r.data_manifest?.research_label || r.id?.slice(0, 8) }} · {{ ((r.total_return ?? 0) * 100).toFixed(1) }}%
          </option>
        </select>
      </label>
    </div>

    <section class="card compare-card" aria-labelledby="compare-title">
      <div class="compare-heading">
        <div>
          <h3 id="compare-title">回测对比</h3>
          <p class="hint">并排查看收益与风险</p>
        </div>
        <span v-if="comparison" class="compare-count">{{ comparison.meta.comparison_count }} 组</span>
      </div>
      <div class="compare-controls">
        <label>选择 2–8 组已完成回测
          <select v-model="compareIds" multiple size="4" :disabled="comparing">
            <option v-for="r in completedRuns" :key="r.id" :value="r.id">
              {{ r.data_manifest?.research_label || r.id?.slice(0, 8) }} · {{ marketLabel(r.market) }}
            </option>
          </select>
        </label>
        <button class="compare-button" type="button" :disabled="compareIds.length < 2 || comparing" @click="loadComparison">
          {{ comparing ? '对比中…' : '开始对比' }}
        </button>
      </div>
      <p v-if="compareError" class="error">{{ compareError }}</p>
      <div v-if="comparison" class="compare-table-wrap">
        <table class="compare-table">
          <thead>
            <tr>
              <th>回测</th><th>市场</th><th>成本情景</th><th>总收益</th><th>年化波动</th><th>夏普</th><th>最大回撤</th><th>数据证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in comparison.data" :key="item.run_id">
              <td><code>{{ item.run_id.slice(0, 8) }}</code><small>{{ item.created_at?.slice(0, 10) }}</small></td>
              <td>{{ marketLabel(item.evidence?.market) }}</td>
              <td>{{ costScenarioLabel(item.evidence?.market, item.evidence?.cost_scenario, item.evidence?.cost_model, item.evidence?.fund_fee_rate) }}</td>
              <td :class="(item.total_return ?? 0) >= 0 ? 'positive' : 'negative'">{{ ((item.total_return ?? 0) * 100).toFixed(2) }}%</td>
              <td>{{ ((item.metrics?.annual_volatility ?? 0) * 100).toFixed(2) }}%</td>
              <td>{{ item.metrics?.sharpe_ratio?.toFixed(2) ?? '-' }}</td>
              <td class="negative">{{ ((item.metrics?.max_drawdown ?? 0) * 100).toFixed(2) }}%</td>
              <td><span class="evidence-state" :class="item.evidence?.coverage_complete === false ? 'incomplete' : ''">{{ evidenceLabel(item) }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="metrics" class="metrics-grid">
      <div class="metric-card card">
        <div class="metric-label">年化收益</div>
        <div class="metric-value" :class="metrics.annual_return >= 0 ? 'positive' : 'negative'">{{ (metrics.annual_return * 100).toFixed(2) }}%</div>
      </div>
      <div class="metric-card card"><div class="metric-label">年化波动率</div><div class="metric-value">{{ (metrics.annual_volatility * 100).toFixed(2) }}%</div></div>
      <div class="metric-card card"><div class="metric-label">夏普比率</div><div class="metric-value">{{ metrics.sharpe_ratio?.toFixed(2) }}</div></div>
      <div class="metric-card card"><div class="metric-label">Sortino比率</div><div class="metric-value">{{ metrics.sortino_ratio?.toFixed(2) }}</div></div>
      <div class="metric-card card"><div class="metric-label">最大回撤</div><div class="metric-value negative">{{ (metrics.max_drawdown * 100).toFixed(2) }}%</div></div>
      <div class="metric-card card"><div class="metric-label">Calmar比率</div><div class="metric-value">{{ metrics.calmar_ratio?.toFixed(2) }}</div></div>
      <div class="metric-card card"><div class="metric-label">胜率</div><div class="metric-value">{{ metrics.win_rate ? (metrics.win_rate * 100).toFixed(1) + '%' : '-' }}</div></div>
      <div class="metric-card card"><div class="metric-label">盈亏比</div><div class="metric-value">{{ metrics.profit_loss_ratio?.toFixed(2) ?? '-' }}</div></div>
    </div>

    <div class="section" v-if="attribution">
      <h3>α/β 归因</h3>
      <div class="attribution-grid">
        <div class="attr-item"><span class="attr-label">Alpha (年化)</span><span class="attr-value positive">{{ (attribution.annual_alpha * 100).toFixed(2) }}%</span></div>
        <div class="attr-item"><span class="attr-label">Beta</span><span class="attr-value">{{ attribution.beta?.toFixed(2) }}</span></div>
        <div class="attr-item"><span class="attr-label">R²</span><span class="attr-value">{{ (attribution.r_squared * 100).toFixed(1) }}%</span></div>
        <div class="attr-item"><span class="attr-label">信息比率</span><span class="attr-value">{{ attribution.information_ratio?.toFixed(2) }}</span></div>
      </div>
    </div>
    <p v-else-if="!metrics" class="hint empty-hint">选择已完成的回测查看详细绩效指标</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useApi } from '@/composables/useApi'
import type { RunSummary } from '@/types/api'

const props = defineProps<{ runs: RunSummary[] }>()
const { api } = useApi()
const completedRuns = computed(() => props.runs.filter((run) => run.status === 'completed'))
const selectedRunId = ref('')
const metrics = ref<any>(null)
const attribution = ref<any>(null)
const compareIds = ref<string[]>([])
const comparison = ref<any>(null)
const comparing = ref(false)
const compareError = ref('')

async function loadMetrics() {
  if (!selectedRunId.value) { metrics.value = null; attribution.value = null; return }
  try {
    const [mRes, aRes] = await Promise.all([
      api.get(`/analytics/metrics/${selectedRunId.value}`),
      api.get(`/analytics/attribution/${selectedRunId.value}`),
    ])
    metrics.value = mRes.data
    attribution.value = aRes.data?.error ? null : aRes.data
  } catch (e) {
    metrics.value = null
    attribution.value = null
  }
}

async function loadComparison() {
  if (compareIds.value.length < 2) return
  comparing.value = true
  compareError.value = ''
  try {
    const { data } = await api.get('/analytics/compare', { params: { run_ids: compareIds.value.join(',') } })
    comparison.value = data
  } catch (e: any) {
    comparison.value = null
    compareError.value = e.response?.data?.detail?.message || e.response?.data?.detail || e.message || '对比失败'
  } finally {
    comparing.value = false
  }
}

function marketLabel(market?: string): string {
  return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || 'A 股'
}
function costScenarioLabel(market?: string, id?: string, model?: any, fundFeeRate?: unknown): string {
  if (market === 'cn-fund') {
    const rate = Number(fundFeeRate)
    return Number.isFinite(rate) ? `基金纸面费率 ${(rate * 100).toFixed(3)}%` : '基金纸面费率未记录'
  }
  if (model?.label) return String(model.label)
  return ({ paper_baseline_v1: '纸面基线', paper_low_impact_v1: '纸面低冲击', paper_high_impact_v1: '纸面高冲击' } as Record<string, string>)[id || ''] || (id ? String(id) : '未记录')
}
function evidenceLabel(item: any): string {
  if (item?.evidence?.coverage_complete === false) return '覆盖不完整'
  const datasetHashes = Array.isArray(item?.evidence?.dataset_hashes) ? item.evidence.dataset_hashes : []
  if (datasetHashes.length > 1 && item?.evidence?.data_content_hash) return `NAV ${datasetHashes.length} 只 · ${String(item.evidence.data_content_hash).slice(0, 8)}`
  if (datasetHashes.length === 1) return `NAV 已绑定 · ${String(datasetHashes[0]).slice(0, 8)}`
  if (item?.evidence?.data_content_hash) return `已绑定 · ${String(item.evidence.data_content_hash).slice(0, 8)}`
  if (item?.evidence?.calendar_version) return `日历 · ${item.evidence.calendar_version}`
  return '证据未记录'
}
</script>

<style scoped>
.performance-analysis { margin-top: 32px; }
.analysis-title-row { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.analysis-title-row h2 { margin: 0; }
.eyebrow { margin: 0 0 5px; color: var(--accent); font-size: 12px; font-weight: 600; }
.selected-state { color: var(--green); font-size: 12px; }
.selector-card { margin-bottom: 16px; }
.selector-card label { display: flex; flex-direction: column; gap: 8px; font-size: 13px; color: var(--text-secondary); }
.selector-card select { font-size: 14px; min-width: 300px; }
.compare-card { margin-bottom: 16px; }
.compare-heading { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
.compare-heading h3 { margin: 0; }
.compare-heading .hint { margin: 6px 0 0; text-align: left; }
.compare-count { color: var(--accent); font-size: 13px; white-space: nowrap; }
.compare-controls { display: flex; align-items: flex-end; gap: 16px; margin-top: 18px; }
.compare-controls label { display: flex; flex-direction: column; gap: 8px; color: var(--text-secondary); font-size: 13px; flex: 1; }
.compare-controls select { font-size: 13px; min-height: 104px; width: 100%; }
.compare-button { border: 1px solid var(--accent); color: var(--accent); background: transparent; border-radius: 6px; padding: 10px 18px; min-width: 104px; }
.compare-button:hover:not(:disabled) { background: rgba(59,130,246,0.12); }
.compare-button:disabled { opacity: 0.45; cursor: not-allowed; }
.compare-table-wrap { overflow-x: auto; margin-top: 18px; }
.compare-table { width: 100%; min-width: 900px; border-collapse: collapse; }
.compare-table th, .compare-table td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--border); font-size: 13px; white-space: nowrap; }
.compare-table th:first-child, .compare-table td:first-child { text-align: left; }
.compare-table td small { display: block; color: var(--text-secondary); margin-top: 3px; }
.evidence-state { color: var(--green); font-size: 12px; }
.evidence-state.incomplete { color: var(--red); }
.error { color: var(--red); font-size: 13px; margin-top: 12px; }
.metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
.metric-card { text-align: center; padding: 16px; }
.metric-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
.metric-value { font-size: 20px; font-weight: 700; }
.positive { color: var(--green); }
.negative { color: var(--red); }
.section { margin-top: 16px; }
.section h3 { margin-bottom: 10px; }
.attribution-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 12px; }
.attr-item { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; align-items: center; }
.attr-label { font-size: 12px; color: var(--text-secondary); }
.attr-value { font-size: 18px; font-weight: 600; }
.hint { color: var(--text-secondary); margin-top: 16px; text-align: center; }
.empty-hint { padding: 24px 0; }
@media (max-width: 720px) { .compare-controls { align-items: stretch; flex-direction: column; } .compare-button { width: 100%; } .metrics-grid { grid-template-columns: repeat(2, 1fr); } .selector-card select { min-width: 0; width: 100%; } }
</style>
