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
          <tr><th>ID</th><th>状态</th><th>区间</th><th>收益</th><th>Sharpe</th><th>MaxDD</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id" @click="selectedRun = r" :class="{ selected: selectedRun?.id === r.id }">
            <td><code>{{ r.id?.slice(0, 8) }}</code></td>
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import type { Strategy, RunSummary } from '@/types/api'
import DateRangePicker from '@/components/common/DateRangePicker.vue'

const { api } = useApi()
const strategies = ref<Strategy[]>([])
const runs = ref<RunSummary[]>([])
const selectedRun = ref<RunSummary | null>(null)
const running = ref(false)
const error = ref('')

const config = ref({
  strategy_id: '',
  start_date: '2024-01-01',
  end_date: '2024-12-31',
  initial_capital: 1_000_000,
  rebalance_frequency: 'weekly',
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

async function runBacktest() {
  if (!config.value.strategy_id) { error.value = '请选择策略'; return }
  try {
    running.value = true
    error.value = ''
    await api.post('/backtest/run', config.value)
    await loadData()
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    running.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.config-card { margin-bottom: 24px; }
.config-card h3 { margin-bottom: 16px; }
.form-row { display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 16px; }
.form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); }
.form-row input, .form-row select { font-size: 14px; min-width: 120px; }
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
</style>
