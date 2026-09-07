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
            <option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }} · {{ account.market === 'us-equity' ? '$' : '¥' }}{{ formatNumber(account.equity ?? account.initial_capital) }} {{ marketLabel(account.market) }}</option>
          </select>
        </label>
        <label>新账户市场
          <select v-model="newAccountMarket" :disabled="creatingAccount">
            <option value="a-share">A 股（CNY）</option>
            <option value="cn-fund">国内基金（CNY）</option>
            <option value="us-equity">美股（USD）</option>
          </select>
        </label>
        <button class="btn-accent" type="button" @click="createPersistentAccount" :disabled="creatingAccount">{{ creatingAccount ? '创建中...' : '创建模拟账户' }}</button>
      </div>
      <div v-if="selectedAccount" class="account-summary">
        <span>市场 <strong>{{ marketLabel(selectedAccount.market) }} · {{ selectedAccount.currency || 'CNY' }}</strong></span>
        <span>现金 <strong>{{ formatMoney(selectedAccount.cash, selectedAccount.currency) }}</strong></span>
        <span>持仓市值 <strong>{{ formatMoney(selectedAccount.market_value, selectedAccount.currency) }}</strong></span>
        <span>总权益 <strong>{{ formatMoney(selectedAccount.equity, selectedAccount.currency) }}</strong></span>
      </div>
      <div v-if="schedulerRun" class="scheduler-status" :class="schedulerRun.status">
        自动任务：{{ schedulerStatusLabel }}<span v-if="schedulerRun.reason">（{{ schedulerRun.reason }}）</span>
      </div>
      <form v-if="selectedAccountId" class="order-form" @submit.prevent="submitPersistentOrder">
        <label>方向<select v-model="orderSide"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
        <label>{{ orderCodeLabel }}<input v-model="orderCode" :pattern="orderCodePattern" required @blur="loadOfficialFundNav" /></label>
        <label>{{ orderQuantityLabel }}<input v-model.number="orderQuantity" type="number" :min="orderQuantityMin" :step="orderQuantityStep" required /></label>
        <label>价格<input v-model.number="orderPrice" type="number" min="0.01" step="0.01" required /></label>
        <button class="btn-secondary" type="submit" :disabled="submittingOrder || !canSubmitOrder || (selectedMarket === 'cn-fund' && !fundNavReady)">{{ submittingOrder ? '提交中...' : !canSubmitOrder ? '非交易日' : selectedMarket === 'cn-fund' && !fundNavReady ? '等待官方净值' : '提交模拟订单' }}</button>
      </form>
      <p v-if="selectedAccountId && !canSubmitOrder" class="hint order-hint">{{ selectedAccount?.trading?.today || '今天' }} 为{{ marketLabel(selectedMarket) }}非交易日；模拟订单已暂停，下一交易日可继续。</p>
      <p v-if="selectedAccountId && selectedMarket === 'cn-fund'" class="hint fund-order-hint">基金只按 Eastmoney 官方单位净值成交，不接受手工价格；{{ fundNavReady ? `已载入 ${orderPrice.toFixed(4)}，来源时间 ${orderPriceAsOf || '未知'}` : '请先读取官方净值' }}。</p>
    </section>

    <section class="card observation-panel" aria-labelledby="observation-title">
      <div class="section-header">
        <div><h2 id="observation-title">策略观察期（模拟）</h2><p class="section-description">选择当前市场已完成并通过证据校验的策略，观察 7 天或 30 天。A 股按下一交易日执行；国内基金按下一有效 NAV 执行。自动订单和手动订单共用同一套模拟风控，不会连接真实券商。</p></div>
        <button class="btn-secondary" type="button" @click="loadObservationData">刷新观察</button>
      </div>
      <p class="observation-guardrail">策略只调仓它管理的{{ marketLabel(selectedMarket) }}批次；观察期间手动买入或加仓的批次不会被策略自动卖出。所有订单都标记为 paper_only，公开数据源断开或过期时会自动阻断。回测中的纸面费用率不会自动沿用到观察期，观察期使用当前市场规则并单独记录。</p>
      <form v-if="selectedAccountId && selectedMarket !== 'us-equity'" class="observation-form" @submit.prevent="createObservation">
        <label>回测策略
          <select v-model="observationForm.strategy_id" required :disabled="strategies.length === 0">
            <option value="" disabled>{{ strategies.length ? '请选择策略' : '暂无已完成回测' }}</option>
            <option v-for="strategy in strategies" :key="strategy.id" :value="strategy.id">{{ strategy.name }}</option>
          </select>
        </label>
        <label>观察时长
          <select v-model.number="observationForm.duration_days"><option :value="7">7 天</option><option :value="30">30 天</option></select>
        </label>
        <label>分配比例
          <input v-model.number="observationForm.allocation_pct" type="number" min="1" max="100" step="1" />
        </label>
        <label class="check-label"><input v-model="observationForm.auto_trade" type="checkbox" /> 自动模拟下单</label>
        <button class="btn-accent" type="submit" :disabled="creatingObservation || !observationForm.strategy_id">{{ creatingObservation ? '创建中...' : '开始观察' }}</button>
      </form>
      <p v-else-if="selectedAccountId" class="hint observation-hint">当前账户为{{ marketLabel(selectedMarket) }}；{{ selectedMarket === 'us-equity' ? '美股策略观察尚未开放，当前仅支持行情和日结估值。' : '只有同市场、同数据清单且策略指纹一致的回测才能进入观察。' }}</p>
      <p v-if="strategies.length === 0" class="hint observation-hint">请先在回测页面完成至少一个策略回测，系统才允许进入观察期。</p>
      <div v-if="observations.length" class="observation-list">
        <article v-for="observation in observations" :key="observation.id" class="observation-item">
          <div class="observation-main">
            <div><strong>{{ strategyName(observation.strategy_id) }}</strong><span class="observation-badge" :class="observation.status">{{ observationStatus(observation.status) }}</span></div>
            <p>{{ observation.start_date }} 至 {{ observation.end_date }} · {{ observation.duration_days }} 天 · 分配 ¥{{ formatNumber(observation.allocated_capital) }}（{{ (observation.allocation_pct * 100).toFixed(0) }}%）<span v-if="observation.pending_signal_count"> · {{ observation.pending_signal_date }} 待下一有效日执行</span></p>
            <small class="observation-scope">仅调仓策略买入的批次；观察期间手动买入或加仓的批次不会被自动卖出。所有自动订单均标记为 paper_only。</small>
            <small v-if="observation.last_error" class="observation-error">{{ observation.last_error }}</small>
          </div>
          <div class="observation-actions">
            <button v-if="observation.status === 'draft'" class="btn-secondary" type="button" @click="controlObservation(observation.id, 'start')">启动</button>
            <button v-if="observation.status === 'running'" class="btn-secondary" type="button" @click="controlObservation(observation.id, 'pause')">暂停</button>
            <button v-if="observation.status === 'paused'" class="btn-secondary" type="button" @click="controlObservation(observation.id, 'resume')">恢复</button>
            <button v-if="['draft', 'running', 'paused'].includes(observation.status)" class="btn-danger" type="button" @click="controlObservation(observation.id, 'stop')">停止</button>
            <button v-if="observation.status === 'running'" class="btn-secondary" type="button" @click="tickObservation(observation.id)">运行一次</button>
            <button v-if="observation.latest_rebalance_plan_status === 'blocked' && observation.latest_rebalance_plan_id" class="btn-secondary" type="button" @click="retryObservationPlan(observation)">复核后重试调仓</button>
            <button class="btn-quiet" type="button" @click="toggleObservationEvents(observation.id)">{{ expandedObservationId === observation.id ? '收起事件' : '查看事件' }}</button>
          </div>
          <div v-if="expandedObservationId === observation.id" class="observation-events">
            <div v-for="event in observationEvents[observation.id] || []" :key="event.id" class="observation-event"><span>{{ event.created_at }}</span><strong>{{ eventTypeLabel(event.event_type) }}</strong><span>{{ event.reason || `${event.signal_count} 个信号 · ${event.order_count} 个订单` }}</span></div>
            <span v-if="!(observationEvents[observation.id] || []).length" class="hint">暂无事件</span>
          </div>
        </article>
      </div>
      <p v-else class="hint observation-hint">当前账户还没有策略观察任务。</p>
    </section>

    <div class="metrics-grid">
      <div class="metric-card card">
        <div class="metric-label">总权益</div>
        <div class="metric-value">{{ formatMoney(selectedAccount?.equity ?? store.totalValue, selectedAccount?.currency) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">现金</div>
        <div class="metric-value">{{ formatMoney(selectedAccount?.cash ?? store.cash, selectedAccount?.currency) }}</div>
      </div>
      <div class="metric-card card">
        <div class="metric-label">持仓市值</div>
        <div class="metric-value">{{ formatMoney(selectedAccount?.market_value ?? store.marketValue, selectedAccount?.currency) }}</div>
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
          <tr><th>代码</th><th>{{ selectedMarket === 'cn-fund' ? '份额' : '股数' }}</th><th>均价</th><th>市值</th><th>权重</th><th>报价证据</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in accountPositions" :key="p.code">
            <td><code>{{ p.code }}</code></td>
            <td>{{ p.shares }}</td>
            <td>{{ formatMoney(p.avg_cost, selectedAccount?.currency) }}</td>
            <td>{{ formatMoney(p.market_value, selectedAccount?.currency) }}</td>
            <td>{{ positionWeight(p) }}</td>
            <td class="quote-evidence">
              <strong>{{ p.price_source || 'manual_input' }}</strong>
              <span>{{ p.price_freshness || 'manual' }}</span>
              <small>{{ p.price_as_of ? formatDateTime(p.price_as_of) : '无时间戳' }}</small>
            </td>
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
            <td>{{ formatMoney(h.total_value, selectedAccount?.currency) }}</td>
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
import { computed, ref, onMounted, onUnmounted, reactive, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useApi } from '@/composables/useApi'
import { useWebSocket } from '@/composables/useWebSocket'
import { usePaperStore } from '@/stores/paper'

const { api } = useApi()
const route = useRoute()
const store = usePaperStore()
const { connected, lastMessage } = useWebSocket('dashboard')
const history = ref<any[]>([])
const status = ref('')
interface PaperAccountSnapshot { id: string; name: string; market?: 'a-share' | 'cn-fund' | 'us-equity'; currency?: string; asset_type?: string; initial_capital: number; cash: number; market_value?: number | null; equity?: number | null; daily_return?: number | null; valuation_date?: string | null; positions?: any[]; trading?: { today?: string; is_trading_day?: boolean } }
interface RealtimeMessage { account_id?: string; type?: string; data?: any }
interface SchedulerRun { run_date: string; status: string; reason?: string | null; valuation_count: number }
interface StrategyOption { id: string; name: string; market?: string }
interface Observation { id: string; account_id: string; strategy_id: string; duration_days: number; allocation_pct: number; allocated_capital: number; start_date: string; end_date: string; status: string; auto_trade: boolean; managed_codes?: string[]; manual_positions_protected?: boolean; backtest_run_id?: string | null; market?: string | null; strategy_fingerprint?: string | null; pending_signal_date?: string | null; pending_signal_count?: number; last_error?: string | null; latest_rebalance_plan_id?: string | null; latest_rebalance_plan_status?: string | null }
interface ObservationEvent { id: string; event_type: string; created_at: string; reason?: string | null; signal_count: number; order_count: number }
const accounts = ref<PaperAccountSnapshot[]>([])
const selectedAccountId = ref('')
const selectedAccount = ref<PaperAccountSnapshot | null>(null)
const accountPositions = computed(() => selectedAccount.value?.positions ?? [])
const selectedMarket = computed(() => selectedAccount.value?.market || 'a-share')
const newAccountMarket = ref<'a-share' | 'cn-fund' | 'us-equity'>('a-share')
const orderCodeLabel = computed(() => selectedMarket.value === 'cn-fund' ? '基金代码' : selectedMarket.value === 'us-equity' ? '股票代码' : '证券代码')
const orderQuantityLabel = computed(() => selectedMarket.value === 'cn-fund' ? '份额' : '股数')
const orderQuantityMin = computed(() => selectedMarket.value === 'cn-fund' ? 0.01 : selectedMarket.value === 'us-equity' ? 1 : 100)
const orderQuantityStep = computed(() => selectedMarket.value === 'cn-fund' ? 0.01 : selectedMarket.value === 'us-equity' ? 1 : 100)
const orderCodePattern = computed(() => selectedMarket.value === 'cn-fund' ? '(FUND:)?[0-9]{6}' : selectedMarket.value === 'us-equity' ? '[A-Za-z][A-Za-z0-9.-]{0,11}' : '[0-9]{6}\.(SH|SZ|BJ)')
const creatingAccount = ref(false)
const submittingOrder = ref(false)
const orderSide = ref<'buy' | 'sell'>('buy')
const orderCode = ref('000001.SZ')
const orderQuantity = ref(100)
const orderPrice = ref(10)
const orderPriceSource = ref('manual_input')
const orderPriceAsOf = ref<string | null>(null)
const orderPriceFreshness = ref('manual')
const orderIdempotencyKey = ref<string | null>(null)
const schedulerRun = ref<SchedulerRun | null>(null)
const strategies = ref<StrategyOption[]>([])
const observations = ref<Observation[]>([])
const observationEvents = reactive<Record<string, ObservationEvent[]>>({})
const expandedObservationId = ref('')
const creatingObservation = ref(false)
const observationForm = reactive({ strategy_id: '', duration_days: 7, allocation_pct: 25, auto_trade: true })
const fundNavReady = computed(() => selectedMarket.value !== 'cn-fund' || (orderPriceSource.value.toLowerCase() === 'eastmoney:fund_nav' && !!orderPriceAsOf.value && orderPriceFreshness.value !== 'unknown'))
const canSubmitOrder = computed(() => selectedAccount.value?.trading?.is_trading_day !== false)
let observationPoll: ReturnType<typeof setInterval> | undefined
let accountPoll: ReturnType<typeof setInterval> | undefined
// Account selection, snapshot loading, and observation loading can overlap
// (the selection watcher fires while the account snapshot is still resolving).
// Keep only the latest response so a stale A-share response cannot overwrite a
// newly selected fund account's eligible strategy list.
let observationLoadSeq = 0
let accountSnapshotSeq = 0
let historyLoadSeq = 0
let fundNavLoadSeq = 0
let orderSubmitSeq = 0
let retryPlanSeq = 0
const schedulerStatusLabel = computed(() => {
  const labels: Record<string, string> = { completed: '已完成', skipped: '已跳过', failed: '失败', running: '运行中' }
  if (!schedulerRun.value) return ''
  const partial = schedulerRun.value.reason?.startsWith('deferred_market_adapters:')
  return `${schedulerRun.value.run_date} · ${partial ? '部分完成' : labels[schedulerRun.value.status] ?? schedulerRun.value.status}`
})

watch(lastMessage, (msg) => {
  if (!msg || typeof msg !== 'object') return
  const event = msg as RealtimeMessage
  if (event.account_id && event.account_id !== selectedAccountId.value) return
  store.updateFromWS(event)
  if (event.account_id === selectedAccountId.value && (event.type === 'order_fill' || event.type === 'portfolio_update')) {
    void loadAccountSnapshot()
  }
})

function formatNumber(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万'
  return n?.toFixed(2) ?? '0'
}

function marketLabel(market?: string): string {
  return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || 'A 股'
}

function formatMoney(value: number | null | undefined, currency?: string): string {
  const prefix = currency === 'USD' ? '$' : '¥'
  return `${prefix}${formatNumber(value)}`
}

function formatDateTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? '时间未知'
    : parsed.toLocaleString('zh-CN', { hour12: false })
}

async function loadOfficialFundNav() {
  const seq = ++fundNavLoadSeq
  const accountId = selectedAccountId.value
  const market = selectedMarket.value
  const code = orderCode.value.trim().toUpperCase()
  if (market !== 'cn-fund' || !code) return
  try {
    const { data } = await api.get(`/market/markets/cn-fund/quotes`, { params: { symbols: code } })
    const quote = data?.data?.[0]
    if (!quote || !Number.isFinite(Number(quote.price)) || Number(quote.price) <= 0 || !quote.as_of || String(quote.source || '').toLowerCase() !== 'eastmoney:fund_nav') {
      throw new Error('官方净值不可用')
    }
    if (seq !== fundNavLoadSeq || accountId !== selectedAccountId.value || selectedMarket.value !== market || orderCode.value.trim().toUpperCase() !== code) return
    orderPrice.value = Number(quote.price)
    orderPriceSource.value = String(quote.source)
    orderPriceAsOf.value = String(quote.as_of)
    orderPriceFreshness.value = String(quote.freshness || 'stale')
    status.value = `已读取基金官方净值：${code}`
  } catch (e: any) {
    if (seq !== fundNavLoadSeq || accountId !== selectedAccountId.value || selectedMarket.value !== market || orderCode.value.trim().toUpperCase() !== code) return
    orderPriceSource.value = 'manual_input'
    orderPriceAsOf.value = null
    orderPriceFreshness.value = 'manual'
    status.value = '基金官方净值暂不可用，请稍后重试。'
  }
}

function positionWeight(position: { market_value?: number; weight?: number }): string {
  const value = position.weight ?? (position.market_value != null && selectedAccount.value?.equity ? position.market_value / selectedAccount.value.equity : null)
  return value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(1)}%`
}

async function loadHistory(accountId = selectedAccountId.value) {
  const seq = ++historyLoadSeq
  if (!accountId) { if (seq === historyLoadSeq) history.value = []; return }
  try {
    const { data } = await api.get(`/paper/accounts/${accountId}/valuations`, { params: { limit: 60 } })
    if (seq !== historyLoadSeq || accountId !== selectedAccountId.value) return
    history.value = data
  } catch (e) {
    if (seq === historyLoadSeq && accountId === selectedAccountId.value) history.value = []
  }
}

async function loadAccounts() {
  try {
    const { data } = await api.get<PaperAccountSnapshot[]>('/paper/accounts')
    accounts.value = data
    const requestedMarket = String(route.query.market || '')
    const preferred = requestedMarket ? data.find((item) => item.market === requestedMarket) : undefined
    if (!selectedAccountId.value && (preferred || data[0])) selectedAccountId.value = (preferred || data[0]).id
    await loadAccountSnapshot()
    const requestedSymbol = String(route.query.symbol || '').trim()
    if (requestedSymbol) {
      orderCode.value = requestedSymbol.toUpperCase()
      const requestedPrice = Number(route.query.price)
      if (Number.isFinite(requestedPrice) && requestedPrice > 0 && selectedMarket.value !== 'cn-fund') orderPrice.value = requestedPrice
      if (selectedMarket.value === 'cn-fund') await loadOfficialFundNav()
    }
    await loadObservationData()
  } catch (e) {
    status.value = '持久化账户暂不可用。'
  }
}

async function loadObservationData() {
  const loadSeq = ++observationLoadSeq
  const accountId = selectedAccountId.value
  const market = selectedMarket.value
  if (!accountId) return
  if (market === 'us-equity') {
    strategies.value = []
    observations.value = []
    return
  }
  try {
    const [strategyResponse, runResponse, observationResponse] = await Promise.all([
      api.get<any[]>('/strategies'),
      api.get<any[]>('/backtest/runs', { params: { run_type: 'backtest', limit: 100 } }),
      api.get<Observation[]>(`/paper/accounts/${accountId}/observations`),
    ])
    // Ignore a response that belongs to an account/market that is no longer
    // selected. This is especially important when switching between A-share
    // and domestic-fund accounts on a slow network.
    if (loadSeq !== observationLoadSeq || accountId !== selectedAccountId.value || market !== selectedMarket.value) return
    const completed = new Set((runResponse.data || []).filter((run) => run.status === 'completed' && (run.eligible_for_observation || market === 'a-share') && (run.market || 'a-share') === market).map((run) => run.strategy_id))
    strategies.value = (strategyResponse.data || []).filter((strategy) => completed.has(strategy.id) && (strategy.market || 'a-share') === market).map((strategy) => ({ id: strategy.id, name: strategy.name, market: strategy.market }))
    observations.value = observationResponse.data || []
    if (!strategies.value.some((item) => item.id === observationForm.strategy_id)) observationForm.strategy_id = strategies.value[0]?.id || ''
    const running = observations.value.filter((item) => item.status === 'running')
    if (running.length) await Promise.allSettled(running.map((item) => tickObservation(item.id, false)))
  } catch (e) {
    if (loadSeq === observationLoadSeq && accountId === selectedAccountId.value && market === selectedMarket.value) {
      observations.value = []
      strategies.value = []
      observationForm.strategy_id = ''
    }
  }
}

async function createObservation() {
  if (!selectedAccountId.value || !observationForm.strategy_id) return
  try {
    creatingObservation.value = true
    await api.post(`/paper/accounts/${selectedAccountId.value}/observations`, {
      strategy_id: observationForm.strategy_id,
      duration_days: observationForm.duration_days,
      allocation_pct: observationForm.allocation_pct / 100,
      auto_trade: observationForm.auto_trade,
      idempotency_key: `ui-observation-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    })
    status.value = '策略观察任务已创建，请点击启动。'
    await loadObservationData()
  } catch (e: any) {
    status.value = '创建观察失败: ' + (e.response?.data?.detail || e.message)
  } finally { creatingObservation.value = false }
}

async function controlObservation(id: string, action: 'start' | 'pause' | 'resume' | 'stop') {
  if (!selectedAccountId.value) return
  try {
    await api.post(`/paper/accounts/${selectedAccountId.value}/observations/${id}/${action}`)
    status.value = action === 'stop' ? '观察任务已停止。' : '观察任务状态已更新。'
    await loadObservationData()
  } catch (e: any) { status.value = '观察操作失败: ' + (e.response?.data?.detail || e.message) }
}

async function tickObservation(id: string, reload = true) {
  if (!selectedAccountId.value) return
  try {
    const { data } = await api.post(`/paper/accounts/${selectedAccountId.value}/observations/${id}/tick`, {})
    if (data.status === 'blocked') status.value = '观察 tick 已阻断：数据或策略上下文不可用，未产生模拟订单。'
    if (reload) await loadObservationData()
  } catch (e: any) { if (reload) status.value = '观察 tick 失败: ' + (e.response?.data?.detail || e.message) }
}

async function retryObservationPlan(observation: Observation) {
  const planId = observation.latest_rebalance_plan_id
  if (!selectedAccountId.value || !planId) return
  const retrySeq = ++retryPlanSeq
  const accountId = selectedAccountId.value
  const market = selectedMarket.value
  try {
    await api.post(`/paper/accounts/${accountId}/observations/${observation.id}/plans/${planId}/retry`)
    if (retrySeq !== retryPlanSeq || accountId !== selectedAccountId.value || market !== selectedMarket.value) return
    status.value = '已提交再平衡重试；下一次有效行情会继续执行。'
    await loadObservationData()
  } catch (e: any) {
    if (retrySeq !== retryPlanSeq || accountId !== selectedAccountId.value || market !== selectedMarket.value) return
    status.value = '再平衡重试失败: ' + (e.response?.data?.detail || e.message)
  }
}

async function toggleObservationEvents(id: string) {
  if (expandedObservationId.value === id) { expandedObservationId.value = ''; return }
  expandedObservationId.value = id
  try {
    const { data } = await api.get<ObservationEvent[]>(`/paper/accounts/${selectedAccountId.value}/observations/${id}/events`)
    observationEvents[id] = data
  } catch { observationEvents[id] = [] }
}

function strategyName(id: string) { return strategies.value.find((item) => item.id === id)?.name || '策略' }
function observationStatus(value: string) { return ({ draft: '待启动', running: '观察中', paused: '已暂停', stopped: '已停止', completed: '已完成', partial: '待恢复', blocked: '已阻断' } as Record<string, string>)[value] || value }
function eventTypeLabel(value: string) { return ({ created: '已创建', started: '已启动', paused: '已暂停', resumed: '已恢复', stopped: '已停止', tick_completed: 'tick完成', tick_blocked: 'tick阻断', tick_skipped: '非交易日跳过', rebalance_partial: '再平衡待恢复', expired: '已到期' } as Record<string, string>)[value] || value }

async function loadSchedulerRun() {
  try {
    const { data } = await api.get<SchedulerRun[]>('/paper/scheduler/runs', { params: { limit: 1 } })
    schedulerRun.value = data[0] ?? null
  } catch (e) { schedulerRun.value = null }
}

async function loadAccountSnapshot() {
  const seq = ++accountSnapshotSeq
  const accountId = selectedAccountId.value
  if (!accountId) { if (seq === accountSnapshotSeq) selectedAccount.value = null; return }
  try {
    const { data } = await api.get<PaperAccountSnapshot>(`/paper/accounts/${accountId}`)
    if (seq !== accountSnapshotSeq || accountId !== selectedAccountId.value) return
    selectedAccount.value = data
    await loadHistory(accountId)
  } catch (e) {
    if (seq === accountSnapshotSeq && accountId === selectedAccountId.value) selectedAccount.value = null
  }
}

async function createPersistentAccount() {
  try {
    creatingAccount.value = true
    const { data } = await api.post<PaperAccountSnapshot>('/paper/accounts', { name: `${marketLabel(newAccountMarket.value)}模拟账户 ${new Date().toLocaleDateString('zh-CN')}`, market: newAccountMarket.value, initial_capital: 1_000_000 })
    selectedAccountId.value = data.id
    status.value = '持久化模拟账户已创建。'
    await loadAccounts()
  } catch (e: any) {
    status.value = '创建账户失败: ' + (e.response?.data?.detail || e.message)
  } finally { creatingAccount.value = false }
}

async function submitPersistentOrder() {
  if (!selectedAccountId.value) return
  const accountId = selectedAccountId.value
  const market = selectedMarket.value
  const submitSeq = ++orderSubmitSeq
  const code = orderCode.value.toUpperCase()
  if (selectedMarket.value === 'cn-fund' && !fundNavReady.value) {
    status.value = '基金订单需要先读取官方单位净值。'
    return
  }
  let requestKey: string | null = null
  try {
    submittingOrder.value = true
    const key = orderIdempotencyKey.value ?? `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    requestKey = key
    orderIdempotencyKey.value = key
    const { data } = await api.post(`/paper/accounts/${accountId}/orders`, { market, idempotency_key: key, code, side: orderSide.value, quantity: orderQuantity.value, price: orderPrice.value, price_source: orderPriceSource.value, price_as_of: orderPriceAsOf.value, price_freshness: orderPriceFreshness.value })
    // The user may switch accounts while the request is in flight. Keep the
    // completed order in the old account, but never overwrite the new account's
    // status, form key, or snapshot with stale response data.
    if (accountId !== selectedAccountId.value || market !== selectedMarket.value) return
    status.value = data.status === 'filled' ? `模拟订单已成交：${data.code} ${data.quantity} ${market === 'cn-fund' ? '份' : '股'}` : `订单被拒绝：${data.reject_reason || '风控规则'}`
    if (orderIdempotencyKey.value === key) orderIdempotencyKey.value = null
    await loadAccountSnapshot()
  } catch (e: any) {
    if (accountId !== selectedAccountId.value || market !== selectedMarket.value) return
    if (e.response && requestKey && orderIdempotencyKey.value === requestKey) orderIdempotencyKey.value = null
    status.value = e.response ? ('提交订单失败: ' + (e.response?.data?.detail || e.message)) : '网络未确认订单状态；请点击重试，系统会使用同一幂等键。'
  } finally {
    if (submitSeq === orderSubmitSeq) submittingOrder.value = false
  }
}

watch([orderSide, orderCode, orderQuantity, orderPrice], () => {
  if (orderIdempotencyKey.value) orderIdempotencyKey.value = null
})
function resetOrderDefaults(market: string) {
  orderPriceSource.value = 'manual_input'
  orderPriceAsOf.value = null
  orderPriceFreshness.value = 'manual'
  if (market === 'cn-fund') { orderCode.value = '110022'; orderQuantity.value = 1; orderPrice.value = 2; void loadOfficialFundNav() }
  else if (market === 'us-equity') { orderCode.value = 'AAPL'; orderQuantity.value = 1; orderPrice.value = 100 }
  else { orderCode.value = '000001.SZ'; orderQuantity.value = 100; orderPrice.value = 10 }
}
watch(selectedMarket, (market) => {
  orderSubmitSeq += 1
  retryPlanSeq += 1
  submittingOrder.value = false
  orderIdempotencyKey.value = null
  observationForm.strategy_id = ''
  resetOrderDefaults(market)
  // `selectedMarket` is derived from the asynchronously loaded account
  // snapshot.  Reload strategy/observation data after that snapshot settles,
  // otherwise an account switch can briefly leave the previous market's
  // strategy list on screen.
  void loadObservationData()
}, { immediate: true })
watch(selectedAccountId, () => {
  orderSubmitSeq += 1
  retryPlanSeq += 1
  submittingOrder.value = false
  orderIdempotencyKey.value = null
  void loadObservationData()
})

onMounted(() => {
  void Promise.allSettled([loadAccounts(), loadSchedulerRun()])
  observationPoll = setInterval(() => { if (observations.value.some((item) => item.status === 'running')) void loadObservationData() }, 60_000)
  // The scheduler runs in the API worker and may finish while this page is
  // open. Polling keeps the displayed equity/provenance current even when no
  // WebSocket client is connected; the UI never labels this as exchange-live.
  accountPoll = setInterval(() => {
    if (!selectedAccountId.value) return
    void Promise.allSettled([loadAccountSnapshot(), loadSchedulerRun()])
  }, 30_000)
})
onUnmounted(() => {
  if (observationPoll) clearInterval(observationPoll)
  if (accountPoll) clearInterval(accountPoll)
})
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
.quote-evidence { min-width: 150px; font-size: 12px; color: var(--text-secondary); }
.quote-evidence strong, .quote-evidence span, .quote-evidence small { display: block; }
.quote-evidence strong { color: var(--text-primary); font-size: 11px; }
.quote-evidence small { margin-top: 2px; font-size: 10px; }
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
.scheduler-status { margin-top: 10px; color: var(--text-secondary); font-size: 12px; }
.scheduler-status.completed { color: var(--green); }
.scheduler-status.failed { color: var(--red); }
.order-form { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 14px; }
.order-form input, .order-form select { min-width: 130px; }
.observation-panel { margin-bottom: 24px; }
.observation-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
.observation-form label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; }
.observation-form input, .observation-form select { min-width: 150px; }
.observation-form input[type="number"] { width: 110px; min-width: 110px; }
.check-label { display: flex !important; align-items: center; gap: 7px !important; padding-bottom: 10px; }
.check-label input { min-width: auto; }
.observation-hint { margin: 14px 0 0; }
.observation-guardrail { margin: 10px 0 0; color: var(--text-secondary); font-size: 12px; line-height: 1.55; }
.observation-list { display: grid; gap: 10px; margin-top: 16px; }
.observation-item { border: 1px solid var(--border); border-radius: 10px; padding: 14px; background: var(--bg-secondary); }
.observation-main { min-width: 0; }
.observation-main strong { font-size: 15px; }
.observation-main p { margin: 7px 0 0; color: var(--text-secondary); font-size: 12px; }
.observation-scope { display: block; margin-top: 7px; color: var(--text-secondary); line-height: 1.55; }
.observation-badge { display: inline-flex; margin-left: 9px; padding: 3px 7px; border-radius: 999px; background: rgba(148,163,184,.14); color: var(--text-secondary); font-size: 11px; }
.observation-badge.running { color: var(--green); background: rgba(34,197,94,.12); }
.observation-badge.blocked { color: var(--yellow, #eab308); }
.observation-error { display: block; margin-top: 7px; color: var(--yellow, #eab308); overflow-wrap: anywhere; }
.observation-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 13px; }
.btn-quiet { border: 0; background: transparent; color: var(--text-secondary); padding: 8px 4px; }
.observation-events { display: grid; gap: 7px; margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border); }
.observation-event { display: grid; grid-template-columns: 150px 90px 1fr; gap: 10px; color: var(--text-secondary); font-size: 11px; align-items: center; }
.observation-event strong { color: var(--text-primary); }
@media (max-width: 700px) { .account-toolbar, .account-summary, .order-form { align-items: stretch; flex-direction: column; } .account-toolbar select, .order-form input, .order-form select { width: 100%; } }
</style>
