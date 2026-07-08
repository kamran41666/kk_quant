<template>
  <div class="page">
    <h1>绩效分析</h1>

    <div class="card selector-card">
      <label>选择回测
        <select v-model="selectedRunId" @change="loadMetrics">
          <option value="">-- 选择已完成的回测 --</option>
          <option v-for="r in completedRuns" :key="r.id" :value="r.id">
            {{ r.id?.slice(0, 8) }} - {{ r.created_at?.slice(0, 10) }} ({{ ((r.total_return ?? 0) * 100).toFixed(1) }}%)
          </option>
        </select>
      </label>
    </div>

    <div v-if="metrics">
      <div class="metrics-grid">
        <div class="metric-card card">
          <div class="metric-label">年化收益</div>
          <div class="metric-value" :class="metrics.annual_return >= 0 ? 'positive' : 'negative'">{{ (metrics.annual_return * 100).toFixed(2) }}%</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">年化波动率</div>
          <div class="metric-value">{{ (metrics.annual_volatility * 100).toFixed(2) }}%</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">夏普比率</div>
          <div class="metric-value">{{ metrics.sharpe_ratio?.toFixed(2) }}</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">Sortino比率</div>
          <div class="metric-value">{{ metrics.sortino_ratio?.toFixed(2) }}</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">最大回撤</div>
          <div class="metric-value negative">{{ (metrics.max_drawdown * 100).toFixed(2) }}%</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">Calmar比率</div>
          <div class="metric-value">{{ metrics.calmar_ratio?.toFixed(2) }}</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">胜率</div>
          <div class="metric-value">{{ metrics.win_rate ? (metrics.win_rate * 100).toFixed(1) + '%' : '-' }}</div>
        </div>
        <div class="metric-card card">
          <div class="metric-label">盈亏比</div>
          <div class="metric-value">{{ metrics.profit_loss_ratio?.toFixed(2) ?? '-' }}</div>
        </div>
      </div>

      <div class="section" v-if="attribution">
        <h2>α/β 归因</h2>
        <div class="attribution-grid">
          <div class="attr-item"><span class="attr-label">Alpha (年化)</span><span class="attr-value positive">{{ (attribution.annual_alpha * 100).toFixed(2) }}%</span></div>
          <div class="attr-item"><span class="attr-label">Beta</span><span class="attr-value">{{ attribution.beta?.toFixed(2) }}</span></div>
          <div class="attr-item"><span class="attr-label">R²</span><span class="attr-value">{{ (attribution.r_squared * 100).toFixed(1) }}%</span></div>
          <div class="attr-item"><span class="attr-label">信息比率</span><span class="attr-value">{{ attribution.information_ratio?.toFixed(2) }}</span></div>
        </div>
      </div>
    </div>

    <p v-else class="hint">选择已完成的回测查看详细绩效指标</p>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import type { RunSummary } from '@/types/api'

const { api } = useApi()
const completedRuns = ref<RunSummary[]>([])
const selectedRunId = ref('')
const metrics = ref<any>(null)
const attribution = ref<any>(null)

async function loadRuns() {
  try {
    const { data } = await api.get<RunSummary[]>('/backtest/runs', { params: { run_type: 'backtest', limit: 50 } })
    completedRuns.value = data.filter(r => r.status === 'completed')
  } catch (e) {}
}

async function loadMetrics() {
  if (!selectedRunId.value) { metrics.value = null; attribution.value = null; return }
  try {
    const [mRes, aRes] = await Promise.all([
      api.get(`/analytics/metrics/${selectedRunId.value}`),
      api.get(`/analytics/attribution/${selectedRunId.value}`),
    ])
    metrics.value = mRes.data
    if (!aRes.data.error) attribution.value = aRes.data
  } catch (e) {}
}

onMounted(loadRuns)
</script>

<style scoped>
.selector-card { margin-bottom: 24px; }
.selector-card label { display: flex; flex-direction: column; gap: 8px; font-size: 13px; color: var(--text-secondary); }
.selector-card select { font-size: 14px; min-width: 300px; }
.metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.metric-card { text-align: center; padding: 20px; }
.metric-label { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
.metric-value { font-size: 22px; font-weight: 700; }
.positive { color: var(--green); }
.negative { color: var(--red); }
.section { margin-top: 24px; }
.attribution-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-top: 12px; }
.attr-item { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 8px; padding: 16px; display: flex; justify-content: space-between; align-items: center; }
.attr-label { font-size: 13px; color: var(--text-secondary); }
.attr-value { font-size: 20px; font-weight: 600; }
.hint { color: var(--text-secondary); margin-top: 24px; text-align: center; }
</style>
