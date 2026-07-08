<template>
  <div class="page">
    <div class="page-header">
      <h1>模拟交易</h1>
      <div class="actions">
        <button class="btn-accent" @click="triggerRun" :disabled="triggering">
          {{ triggering ? '运行中...' : '手动触发' }}
        </button>
        <button class="btn-danger" @click="resetAccount">重置账户</button>
      </div>
    </div>

    <div class="metrics-grid">
      <div class="metric-card card">
        <div class="metric-label">总权益</div>
        <div class="metric-value">¥{{ formatNumber(store.totalValue) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">现金</div>
        <div class="metric-value">¥{{ formatNumber(store.cash) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">持仓市值</div>
        <div class="metric-value">¥{{ formatNumber(store.marketValue) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">持仓数</div>
        <div class="metric-value">{{ store.positions.length }}</div>
      </div>
    </div>

    <div class="section">
      <h2>当前持仓</h2>
      <table class="data-table" v-if="store.positions.length > 0">
        <thead>
          <tr><th>代码</th><th>股数</th><th>均价</th><th>市值</th><th>权重</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in store.positions" :key="p.code">
            <td><code>{{ p.code }}</code></td>
            <td>{{ p.shares }}</td>
            <td>¥{{ p.avg_cost?.toFixed(2) }}</td>
            <td>¥{{ formatNumber(p.market_value) }}</td>
            <td>{{ (p.weight * 100).toFixed(1) }}%</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">暂无持仓</p>
    </div>

    <div class="section">
      <h2>最新信号</h2>
      <div class="signals-grid" v-if="store.signals.length > 0">
        <div v-for="s in store.signals.slice(0, 10)" :key="s.code" class="signal-chip">
          <code>{{ s.code }}</code>
          <span>{{ (s.weight * 100).toFixed(1) }}%</span>
        </div>
      </div>
      <p v-else class="hint">暂无信号 — 在调仓日自动生成</p>
    </div>

    <div class="section">
      <h2>历史快照</h2>
      <table class="data-table" v-if="history.length > 0">
        <thead>
          <tr><th>日期</th><th>总权益</th><th>日收益</th><th>持仓数</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.date">
            <td>{{ h.date }}</td>
            <td>¥{{ formatNumber(h.total_value) }}</td>
            <td :class="h.daily_return >= 0 ? 'positive' : 'negative'">{{ (h.daily_return * 100).toFixed(2) }}%</td>
            <td>{{ h.n_positions }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="status" class="ws-status">{{ status }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useApi } from '@/composables/useApi'
import { useWebSocket } from '@/composables/useWebSocket'
import { usePaperStore } from '@/stores/paper'

const { api } = useApi()
const store = usePaperStore()
const { connected, lastMessage } = useWebSocket('dashboard')
const history = ref<any[]>([])
const triggering = ref(false)
const status = ref('')

watch(lastMessage, (msg) => {
  if (msg) store.updateFromWS(msg)
})

function formatNumber(n: number): string {
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return n?.toFixed(2) ?? '0'
}

async function loadHistory() {
  try {
    const { data } = await api.get('/paper/history', { params: { limit: 30 } })
    history.value = data
  } catch (e) {}
}

async function triggerRun() {
  try {
    triggering.value = true
    const { data } = await api.post('/paper/trigger')
    status.value = '触发成功: ' + JSON.stringify(data)
    await loadHistory()
    // Also fetch latest status
    const { data: s } = await api.get('/paper/status')
    if (s.cash) { store.cash = s.cash; store.marketValue = s.market_value; store.totalValue = s.total_value; store.positions = s.positions || [] }
  } catch (e: any) {
    status.value = '触发失败: ' + (e.response?.data?.detail || e.message)
  } finally {
    triggering.value = false
  }
}

async function resetAccount() {
  if (!confirm('确认重置模拟账户？所有持仓和记录将被清除。')) return
  try {
    await api.post('/paper/reset')
    store.$reset()
    history.value = []
    status.value = '账户已重置'
  } catch (e: any) {
    status.value = '重置失败: ' + e.message
  }
}

onMounted(loadHistory)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.actions { display: flex; gap: 12px; }
.btn-accent { background: var(--accent); color: #fff; padding: 10px 20px; border-radius: 6px; font-size: 14px; }
.btn-accent:hover { background: var(--accent-hover); }
.btn-accent:disabled { opacity: 0.5; }
.btn-danger { background: transparent; color: var(--red); border: 1px solid var(--red); padding: 10px 20px; border-radius: 6px; font-size: 14px; }
.btn-danger:hover { background: rgba(239,68,68,0.1); }
.metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.metric-card { text-align: center; padding: 24px; }
.metric-label { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
.metric-value { font-size: 24px; font-weight: 700; }
.positive { color: var(--green); }
.negative { color: var(--red); }
.section { margin-top: 24px; }
.data-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
.data-table th, .data-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }
.data-table th { color: var(--text-secondary); font-weight: 600; }
.signals-grid { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.signal-chip { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; display: flex; gap: 8px; font-size: 13px; }
.hint { color: var(--text-secondary); margin-top: 12px; }
.ws-status { font-size: 12px; color: var(--text-secondary); margin-top: 16px; }
</style>
