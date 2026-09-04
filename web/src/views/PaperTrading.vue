<template>
  <div class="page">
    <div class="page-header">
      <h1>模拟交易</h1>
      <p class="page-note">仅操作当前选中的持久化模拟账户；不会连接真实券商。</p>
    </div>

    <section class="card phase2-account-panel" aria-labelledby="phase2-account-title">
      <div class="section-header">
        <div><h2 id="phase2-account-title">持久化模拟账户</h2><p class="section-description">Phase 2 账户不会连接真实券商；订单会记录到本地账本并支持幂等重试。</p></div>
        <button class="btn-secondary" type="button" @click="loadAccounts">刷新账户</button>
      </div>
      <div class="account-toolbar">
        <label>当前账户
          <select v-model="selectedAccountId" :disabled="accounts.length === 0" @change="loadAccountSnapshot">
            <option v-if="accounts.length === 0" value="">尚未创建账户</option>
            <option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }} · ¥{{ formatNumber(account.equity ?? account.initial_capital) }}</option>
          </select>
        </label>
        <button class="btn-accent" type="button" @click="createPersistentAccount" :disabled="creatingAccount">{{ creatingAccount ? '创建中...' : '创建 100 万模拟账户' }}</button>
      </div>
      <div v-if="selectedAccount" class="account-summary">
        <span>现金 <strong>¥{{ formatNumber(selectedAccount.cash) }}</strong></span>
        <span>持仓市值 <strong>¥{{ formatNumber(selectedAccount.market_value) }}</strong></span>
        <span>总权益 <strong>¥{{ formatNumber(selectedAccount.equity) }}</strong></span>
      </div>
      <form v-if="selectedAccountId" class="order-form" @submit.prevent="submitPersistentOrder">
        <label>方向<select v-model="orderSide"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
        <label>代码<input v-model="orderCode" pattern="\d{6}\.(SH|SZ|BJ)" required /></label>
        <label>股数<input v-model.number="orderQuantity" type="number" min="100" step="100" required /></label>
        <label>价格<input v-model.number="orderPrice" type="number" min="0.01" step="0.01" required /></label>
        <button class="btn-secondary" type="submit" :disabled="submittingOrder">{{ submittingOrder ? '提交中...' : '提交模拟订单' }}</button>
      </form>
    </section>

    <div class="metrics-grid">
      <div class="metric-card card">
        <div class="metric-label">总权益</div>
        <div class="metric-value">¥{{ formatNumber(selectedAccount?.equity ?? store.totalValue) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">现金</div>
        <div class="metric-value">¥{{ formatNumber(selectedAccount?.cash ?? store.cash) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">持仓市值</div>
        <div class="metric-value">¥{{ formatNumber(selectedAccount?.market_value ?? store.marketValue) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">持仓数</div>
        <div class="metric-value">{{ accountPositions.length }}</div>
      </div>
    </div>

    <div class="section">
      <h2>当前持仓</h2>
      <table class="data-table" v-if="accountPositions.length > 0">
        <thead>
          <tr><th>代码</th><th>股数</th><th>均价</th><th>市值</th><th>权重</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in accountPositions" :key="p.code">
            <td><code>{{ p.code }}</code></td>
            <td>{{ p.shares }}</td>
            <td>¥{{ p.avg_cost?.toFixed(2) }}</td>
            <td>¥{{ formatNumber(p.market_value) }}</td>
            <td>{{ positionWeight(p) }}</td>
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
      <h2>账户估值历史</h2>
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
import { computed, ref, onMounted, watch } from 'vue'
import { useApi } from '@/composables/useApi'
import { useWebSocket } from '@/composables/useWebSocket'
import { usePaperStore } from '@/stores/paper'

const { api } = useApi()
const store = usePaperStore()
const { connected, lastMessage } = useWebSocket('dashboard')
const history = ref<any[]>([])
const status = ref('')
interface PaperAccountSnapshot { id: string; name: string; initial_capital: number; cash: number; market_value?: number | null; equity?: number | null; daily_return?: number | null; valuation_date?: string | null; positions?: any[] }
const accounts = ref<PaperAccountSnapshot[]>([])
const selectedAccountId = ref('')
const selectedAccount = ref<PaperAccountSnapshot | null>(null)
const accountPositions = computed(() => selectedAccount.value?.positions ?? [])
const creatingAccount = ref(false)
const submittingOrder = ref(false)
const orderSide = ref<'buy' | 'sell'>('buy')
const orderCode = ref('000001.SZ')
const orderQuantity = ref(100)
const orderPrice = ref(10)
const orderIdempotencyKey = ref<string | null>(null)

watch(lastMessage, (msg) => {
  if (msg) store.updateFromWS(msg)
})

function formatNumber(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return n?.toFixed(2) ?? '0'
}

function positionWeight(position: { market_value?: number; weight?: number }): string {
  const value = position.weight ?? (position.market_value != null && selectedAccount.value?.equity ? position.market_value / selectedAccount.value.equity : null)
  return value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(1)}%`
}

async function loadHistory() {
  if (!selectedAccountId.value) { history.value = []; return }
  try {
    const { data } = await api.get(`/paper/accounts/${selectedAccountId.value}/valuations`, { params: { limit: 60 } })
    history.value = data
  } catch (e) { history.value = [] }
}

async function loadAccounts() {
  try {
    const { data } = await api.get<PaperAccountSnapshot[]>('/paper/accounts')
    accounts.value = data
    if (!selectedAccountId.value && data[0]) selectedAccountId.value = data[0].id
    await loadAccountSnapshot()
  } catch (e) {
    status.value = '持久化账户暂不可用。'
  }
}

async function loadAccountSnapshot() {
  if (!selectedAccountId.value) { selectedAccount.value = null; return }
  try {
    const { data } = await api.get<PaperAccountSnapshot>(`/paper/accounts/${selectedAccountId.value}`)
    selectedAccount.value = data
    await loadHistory()
  } catch (e) {
    selectedAccount.value = null
  }
}

async function createPersistentAccount() {
  try {
    creatingAccount.value = true
    const { data } = await api.post<PaperAccountSnapshot>('/paper/accounts', { name: `模拟账户 ${new Date().toLocaleDateString('zh-CN')}`, initial_capital: 1_000_000 })
    selectedAccountId.value = data.id
    status.value = '持久化模拟账户已创建。'
    await loadAccounts()
  } catch (e: any) {
    status.value = '创建账户失败: ' + (e.response?.data?.detail || e.message)
  } finally { creatingAccount.value = false }
}

async function submitPersistentOrder() {
  if (!selectedAccountId.value) return
  try {
    submittingOrder.value = true
    const key = orderIdempotencyKey.value ?? `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    orderIdempotencyKey.value = key
    const { data } = await api.post(`/paper/accounts/${selectedAccountId.value}/orders`, { idempotency_key: key, code: orderCode.value.toUpperCase(), side: orderSide.value, quantity: orderQuantity.value, price: orderPrice.value, price_source: 'manual_input', price_freshness: 'manual' })
    status.value = data.status === 'filled' ? `模拟订单已成交：${data.code} ${data.quantity} 股` : `订单被拒绝：${data.reject_reason || '风控规则'}`
    orderIdempotencyKey.value = null
    await loadAccountSnapshot()
  } catch (e: any) {
    if (e.response) orderIdempotencyKey.value = null
    status.value = e.response ? ('提交订单失败: ' + (e.response?.data?.detail || e.message)) : '网络未确认订单状态；请点击重试，系统会使用同一幂等键。'
  } finally { submittingOrder.value = false }
}

watch([orderSide, orderCode, orderQuantity, orderPrice], () => {
  if (orderIdempotencyKey.value) orderIdempotencyKey.value = null
})
watch(selectedAccountId, () => { orderIdempotencyKey.value = null })

onMounted(() => { void loadAccounts() })
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.page-note { color: var(--text-secondary); font-size: 13px; margin: 0; }
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
.phase2-account-panel { margin-bottom: 24px; }
.account-toolbar, .account-summary, .order-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.account-toolbar label, .order-form label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; }
.account-toolbar select { min-width: 260px; }
.account-summary { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 14px; color: var(--text-secondary); font-size: 12px; }
.account-summary strong { margin-left: 5px; color: var(--text-primary); font-family: "SFMono-Regular", Consolas, monospace; }
.order-form { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 14px; }
.order-form input, .order-form select { min-width: 130px; }
@media (max-width: 700px) { .account-toolbar, .account-summary, .order-form { align-items: stretch; flex-direction: column; } .account-toolbar select, .order-form input, .order-form select { width: 100%; } }
</style>
