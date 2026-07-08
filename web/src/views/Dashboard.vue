<template>
  <div class="page">
    <h1>仪表盘</h1>
    <div class="metrics-grid">
      <div class="metric-card card">
        <div class="metric-label">总权益</div>
        <div class="metric-value">¥{{ formatNumber(store.totalValue) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">日收益</div>
        <div class="metric-value" :class="store.dailyReturn >= 0 ? 'positive' : 'negative'">
          {{ (store.dailyReturn * 100).toFixed(2) }}%
        </div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">夏普比率</div>
        <div class="metric-value">{{ store.sharpeRatio.toFixed(2) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">最大回撤</div>
        <div class="metric-value negative">{{ (store.maxDrawdown * 100).toFixed(2) }}%</div>
      </div>
    </div>

    <div class="section">
      <h2>最近成交</h2>
      <table class="data-table" v-if="store.recentTrades.length > 0">
        <thead>
          <tr><th>时间</th><th>代码</th><th>方向</th><th>股数</th><th>价格</th></tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in store.recentTrades" :key="i">
            <td>{{ t.date }}</td>
            <td>{{ t.code }}</td>
            <td :class="t.side === 'buy' ? 'positive' : 'negative'">{{ t.side === 'buy' ? '买入' : '卖出' }}</td>
            <td>{{ t.shares }}</td>
            <td>¥{{ t.price?.toFixed(2) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">暂无成交数据 — 连接 WebSocket 后实时更新</p>
    </div>

    <div class="section">
      <p class="ws-status">
        WS: <span :class="wsConnected ? 'positive' : 'negative'">{{ wsConnected ? '已连接' : '未连接' }}</span>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import { useDashboardStore } from '@/stores/dashboard'
import { useWebSocket } from '@/composables/useWebSocket'

const store = useDashboardStore()
const { connected: wsConnected, lastMessage } = useWebSocket('dashboard')

watch(lastMessage, (msg) => {
  if (msg) store.updateFromWS(msg)
})

function formatNumber(n: number): string {
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return n.toFixed(2)
}
</script>

<style scoped>
.metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.metric-card { text-align: center; padding: 24px; }
.metric-label { font-size: 13px; color: var(--text-secondary); margin-bottom: 8px; }
.metric-value { font-size: 24px; font-weight: 700; }
.positive { color: var(--green); }
.negative { color: var(--red); }
.section { margin-top: 24px; }
.hint { color: var(--text-secondary); margin-top: 12px; }
.ws-status { font-size: 12px; color: var(--text-secondary); margin-top: 16px; }
.data-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
.data-table th, .data-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }
.data-table th { color: var(--text-secondary); font-weight: 600; }
</style>
