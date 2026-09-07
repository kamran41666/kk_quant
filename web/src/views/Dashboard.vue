<template>
  <div class="page dashboard-page">
    <div class="page-header">
      <div>
        <h1>资产总览</h1>
        <p class="page-subtitle">组合、行情与数据状态集中在一个可信视图中。</p>
      </div>
      <div class="header-actions">
        <span class="updated-at">{{ updatedLabel }}</span>
        <button class="btn-secondary" type="button" :disabled="loading" @click="loadDashboard">
          {{ loading ? '正在刷新' : '刷新数据' }}
        </button>
      </div>
    </div>

    <section class="metrics-grid" aria-label="账户核心指标">
      <article class="metric-card card">
        <div class="metric-top"><span>总权益</span><span class="metric-code">NAV</span></div>
        <div class="metric-value numeric">{{ formatMoney(store.totalValue, true) }}</div>
        <p>模拟账户最近一次快照</p>
      </article>
      <article class="metric-card card">
        <div class="metric-top"><span>当日收益</span><span class="metric-code">1D</span></div>
        <div class="metric-value numeric" :class="directionClass(store.dailyReturn)">
          {{ formatRatioPercent(store.dailyReturn) }}
          <span class="direction-text">{{ directionLabel(store.dailyReturn) }}</span>
        </div>
        <p>相对上一交易日收盘</p>
      </article>
      <article class="metric-card card">
        <div class="metric-top"><span>夏普比率</span><span class="metric-code">SR</span></div>
        <div class="metric-value numeric">{{ store.sharpeRatio?.toFixed(2) ?? '—' }}</div>
        <p>历史风险调整后收益</p>
      </article>
      <article class="metric-card card">
        <div class="metric-top"><span>最大回撤</span><span class="metric-code">MDD</span></div>
        <div class="metric-value numeric market-down">{{ formatRatioPercent(store.maxDrawdown == null ? null : -Math.abs(store.maxDrawdown)) }}</div>
        <p>峰值至谷值的最大跌幅</p>
      </article>
    </section>

    <div class="overview-grid">
      <section class="card equity-card" aria-labelledby="equity-title">
        <div class="section-header compact">
          <div>
            <h2 id="equity-title">模拟账户净值</h2>
            <p>最近 30 个账户快照</p>
          </div>
          <span class="live-label"><span class="status-dot" :class="{ 'is-live': wsConnected }"></span>{{ wsConnected ? '实时连接' : '等待连接' }}</span>
        </div>
        <div v-if="portfolioLoading" class="state-panel" aria-live="polite"><strong>正在加载净值</strong><p>读取模拟账户历史快照。</p></div>
        <div v-else-if="portfolioError" class="state-panel"><strong>净值暂不可用</strong><p>{{ portfolioError }}</p></div>
        <div v-else-if="equityLabels.length === 0" class="state-panel"><strong>暂无净值记录</strong><p>在模拟交易页创建账户并完成一次估值后，这里会显示净值曲线。</p></div>
        <LineChart
          v-else
          class="chart-frame"
          title="模拟账户净值"
          :labels="equityLabels"
          :series="[{ name: '总权益', values: equityValues, color: '#4d8dff', area: true }]"
        />
      </section>

      <aside class="card health-card" aria-labelledby="health-title">
        <div class="section-header compact">
          <div><h2 id="health-title">数据健康</h2><p>本地数据与实时源</p></div>
          <span class="health-badge" :class="healthTone">{{ healthStatusLabel }}</span>
        </div>
        <div v-if="healthLoading" class="state-panel small" aria-live="polite"><strong>正在检查</strong><p>核对行情源与本地数据。</p></div>
        <div v-else-if="healthError" class="state-panel small"><strong>健康检查失败</strong><p>{{ healthError }}</p></div>
        <div v-else-if="!marketHealth" class="state-panel small"><strong>无健康数据</strong><p>服务未返回数据状态。</p></div>
        <dl v-else class="health-list">
          <div><dt>本地最新交易日</dt><dd class="numeric">{{ marketHealth.latest_local_date ?? '未知' }}</dd></div>
          <div><dt>股票基础数据</dt><dd class="numeric">{{ marketHealth.stock_count.toLocaleString('zh-CN') }} 只</dd></div>
          <div><dt>可用数据源</dt><dd class="numeric">{{ providerCount }} 个</dd></div>
          <div><dt>实时连接</dt><dd>{{ wsConnected ? '已连接' : '未连接' }}</dd></div>
        </dl>
        <p v-if="marketHealth && marketHealth.status !== 'ok'" class="health-note">数据源尚未确认健康；执行研究或模拟前请先核对时间与来源。</p>
      </aside>
    </div>

    <section class="section card" aria-labelledby="watch-title">
      <div class="section-header">
        <div><h2 id="watch-title">市场快照</h2><p class="section-description">价格为接口最近一次返回值，不代表可成交价格。</p></div>
        <router-link class="text-link" to="/market">打开行情中心</router-link>
      </div>
      <div v-if="quotesLoading" class="state-panel" aria-live="polite"><strong>正在加载行情</strong><p>从可用行情源读取自选快照。</p></div>
      <div v-else-if="quotesError" class="state-panel"><strong>行情加载失败</strong><p>{{ quotesError }}</p><button class="btn-secondary" type="button" @click="loadQuotes">重试</button></div>
      <div v-else-if="quotes.length === 0" class="state-panel"><strong>暂无行情</strong><p>行情源未返回自选证券数据。</p></div>
      <div v-else class="data-table-wrap">
        <table class="data-table market-table">
          <thead><tr><th>证券</th><th>最新价</th><th>涨跌幅</th><th>成交量</th><th>来源 / 时效</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="quote in quotes" :key="quote.code">
              <td><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></td>
              <td class="numeric">{{ quote.price?.toFixed(2) ?? '—' }}</td>
              <td class="numeric" :class="directionClass(quote.change_pct)">
                {{ formatPercentPoints(quote.change_pct) }} <span class="direction-text">{{ directionLabel(quote.change_pct) }}</span>
              </td>
              <td class="numeric">{{ compactNumber(quote.volume) }}</td>
              <td><span class="freshness" :class="{ stale: quote.freshness === 'stale' || quote.freshness === 'unknown' }">{{ quote.source }} · {{ freshnessLabel(quote.freshness) }}</span></td>
              <td class="numeric muted">{{ quote.as_of ?? quote.received_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section card" aria-labelledby="trade-title">
      <div class="section-header"><h2 id="trade-title">最近成交</h2><span class="section-description">最多显示 20 条</span></div>
      <div v-if="store.recentTrades.length === 0" class="state-panel"><strong>暂无成交</strong><p>模拟账户产生成交后将通过实时连接更新。</p></div>
      <div v-else class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>股数</th><th>价格</th></tr></thead>
          <tbody>
            <tr v-for="(trade, index) in store.recentTrades" :key="`${trade.code}-${trade.date}-${index}`">
              <td class="numeric muted">{{ trade.date }}</td>
              <td><code>{{ trade.code }}</code></td>
              <td :class="trade.side === 'buy' ? 'market-up' : 'market-down'">{{ trade.side === 'buy' ? '买入 ↑' : '卖出 ↓' }}</td>
              <td class="numeric">{{ trade.shares }}</td>
              <td class="numeric">{{ formatMoney(trade.price) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref, watch } from 'vue'
const LineChart = defineAsyncComponent(() => import('@/components/charts/LineChart.vue'))
import { useApi } from '@/composables/useApi'
import { useWebSocket } from '@/composables/useWebSocket'
import { useDashboardStore } from '@/stores/dashboard'
import type { MarketHealth, MarketQuote, QuotesResponse } from '@/types/api'
import {
  compactNumber,
  directionClass,
  directionLabel,
  formatMoney,
  formatPercentPoints,
  formatRatioPercent,
  freshnessLabel,
  apiErrorMessage,
  chronological,
  availableProviderCount,
} from '@/utils/market'

interface PaperHistoryPoint {
  date: string
  total_value: number
  daily_return: number
  n_positions?: number
}

interface PersistentAccount {
  id: string
  equity?: number | null
  daily_return?: number | null
}

interface RealtimeMessage {
  account_id?: string
  type?: string
  data?: any
}

const WATCH_CODES = ['000001.SZ', '600519.SH', '600036.SH']
const { api } = useApi()
const store = useDashboardStore()
const { connected: wsConnected, lastMessage } = useWebSocket('dashboard')

const loading = ref(false)
const portfolioLoading = ref(true)
const healthLoading = ref(true)
const quotesLoading = ref(true)
const portfolioError = ref('')
const healthError = ref('')
const quotesError = ref('')
const history = ref<PaperHistoryPoint[]>([])
const activeAccountId = ref<string | null>(null)
const marketHealth = ref<MarketHealth | null>(null)
const quotes = ref<MarketQuote[]>([])
const lastUpdatedAt = ref<Date | null>(null)

const equityLabels = computed(() => history.value.map(item => item.date))
const equityValues = computed(() => history.value.map(item => item.total_value))
const providerCount = computed(() => {
  if (!marketHealth.value) return 0
  return availableProviderCount(marketHealth.value.providers)
})
const healthStatusLabel = computed(() => {
  if (healthLoading.value) return '检查中'
  if (healthError.value) return '不可用'
  const labels: Record<string, string> = { ok: '健康', unknown: '尚未检查', degraded: '降级', unavailable: '不可用' }
  return labels[marketHealth.value?.status ?? ''] ?? marketHealth.value?.status ?? '未知'
})
const healthTone = computed(() => ({
  healthy: marketHealth.value?.status === 'ok',
  warning: marketHealth.value?.status === 'degraded',
  danger: Boolean(healthError.value) || marketHealth.value?.status === 'unavailable',
}))
const updatedLabel = computed(() => lastUpdatedAt.value
  ? `更新于 ${lastUpdatedAt.value.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
  : '尚未更新')

watch(lastMessage, message => {
  if (!message || typeof message !== 'object') return
  const event = message as RealtimeMessage
  if (activeAccountId.value && event.account_id !== activeAccountId.value) return
  store.updateFromWS(event)
  if (event.type === 'portfolio_update' && event.data?.date) {
    history.value = chronological([...history.value, {
      date: event.data.date,
      total_value: event.data.total_value,
      daily_return: event.data.daily_return,
    }])
  }
  if (event.type === 'order_fill') void loadPortfolio()
})

async function loadPortfolio() {
  portfolioLoading.value = true
  portfolioError.value = ''
  try {
    const { data: accounts } = await api.get<PersistentAccount[]>('/paper/accounts')
    const account = accounts[0]
    if (!account) {
      activeAccountId.value = null
      store.totalValue = null
      store.dailyReturn = null
      history.value = []
      return
    }
    activeAccountId.value = account.id
    const [snapshotResponse, historyResponse] = await Promise.all([
      api.get<PersistentAccount>(`/paper/accounts/${account.id}`),
      api.get<PaperHistoryPoint[]>(`/paper/accounts/${account.id}/valuations`, { params: { limit: 30 } }),
    ])
    const snapshot = snapshotResponse.data
    store.totalValue = snapshot.equity ?? null
    store.dailyReturn = snapshot.daily_return ?? null
    history.value = chronological(historyResponse.data)
  } catch (error: unknown) {
    portfolioError.value = apiErrorMessage(error, '无法读取持久化模拟账户。')
    store.totalValue = null
    store.dailyReturn = null
    store.sharpeRatio = null
    store.maxDrawdown = null
    history.value = []
  } finally {
    portfolioLoading.value = false
  }
}

async function loadHealth() {
  healthLoading.value = true
  healthError.value = ''
  try {
    const { data } = await api.get<MarketHealth>('/market/health')
    marketHealth.value = data
  } catch (error: unknown) {
    healthError.value = apiErrorMessage(error, '无法读取数据健康状态。')
    marketHealth.value = null
  } finally {
    healthLoading.value = false
  }
}

async function loadQuotes() {
  quotesLoading.value = true
  quotesError.value = ''
  try {
    const { data } = await api.get<QuotesResponse>('/market/quotes', { params: { codes: WATCH_CODES.join(',') } })
    quotes.value = data.data
  } catch (error: unknown) {
    quotesError.value = apiErrorMessage(error, '实时行情源暂不可用。')
    quotes.value = []
  } finally {
    quotesLoading.value = false
  }
}

async function loadDashboard() {
  loading.value = true
  await Promise.allSettled([loadPortfolio(), loadHealth(), loadQuotes()])
  lastUpdatedAt.value = new Date()
  loading.value = false
}

onMounted(loadDashboard)
</script>

<style scoped>
.header-actions { display: flex; align-items: center; gap: 12px; }
.updated-at { color: var(--text-tertiary); font-size: 11px; }
.metrics-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric-card { min-width: 0; padding: 17px 18px; }
.metric-top { display: flex; align-items: center; justify-content: space-between; color: var(--text-secondary); font-size: 12px; }
.metric-code { color: var(--text-tertiary); font-family: "SFMono-Regular", Consolas, monospace; font-size: 9px; }
.metric-value { margin-top: 12px; font-size: clamp(22px, 2vw, 29px); font-weight: 670; letter-spacing: -0.045em; }
.metric-card p { margin-top: 4px; color: var(--text-tertiary); font-size: 10px; }
.direction-text { margin-left: 5px; font-family: inherit; font-size: 10px; font-weight: 650; letter-spacing: 0; }
.overview-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, .8fr); gap: 12px; margin-top: 12px; }
.equity-card, .health-card { min-height: 372px; }
.section-header.compact { margin-bottom: 4px; }
.section-header p, .section-description { margin-top: 3px; color: var(--text-tertiary); font-size: 11px; }
.live-label { display: inline-flex; align-items: center; gap: 7px; color: var(--text-secondary); font-size: 10px; }
.chart-frame { height: 300px; }
.health-badge, .freshness {
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  color: var(--text-secondary);
  padding: 3px 8px;
  font-size: 10px;
}
.health-badge.healthy { border-color: rgba(54,179,126,.3); background: var(--down-muted); color: #69c99e; }
.health-badge.warning, .freshness.stale { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.08); color: #edbd62; }
.health-badge.danger { border-color: rgba(239,91,100,.3); background: var(--up-muted); color: #f5838a; }
.state-panel.small { min-height: 245px; }
.health-list { margin-top: 22px; }
.health-list div { display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--border); padding: 13px 0; }
.health-list dt { color: var(--text-secondary); font-size: 12px; }
.health-list dd { font-size: 12px; font-weight: 620; }
.health-note { margin-top: 14px; border-left: 2px solid var(--warning); color: var(--text-secondary); padding-left: 10px; font-size: 11px; }
.text-link { font-size: 12px; font-weight: 620; }
.market-table td:first-child strong, .market-table td:first-child small { display: block; }
.market-table td:first-child small { margin-top: 2px; color: var(--text-tertiary); font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; }
@media (max-width: 1120px) {
  .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .overview-grid { grid-template-columns: 1fr; }
  .health-card { min-height: auto; }
}
@media (max-width: 620px) {
  .header-actions { align-items: stretch; flex-direction: column-reverse; }
  .updated-at { text-align: right; }
  .metrics-grid { grid-template-columns: 1fr 1fr; }
  .metric-card { padding: 14px; }
  .metric-value { font-size: 20px; }
  .metric-card p { display: none; }
  .direction-text { display: block; margin: 2px 0 0; }
  .equity-card { min-height: 340px; }
  .chart-frame { height: 270px; }
}
</style>
