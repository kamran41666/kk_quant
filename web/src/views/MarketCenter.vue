<template>
  <div class="page market-page">
    <div class="page-header">
      <div><h1>市场行情</h1><p class="page-subtitle">查看 A 股实时快照与历史走势；本地样本不足时自动补充公开行情源。</p></div>
      <button class="btn-secondary" type="button" :disabled="quotesLoading" @click="loadQuotes">{{ quotesLoading ? '正在刷新' : '刷新行情' }}</button>
    </div>

    <section class="card overview-panel" aria-labelledby="market-overview-heading">
      <div class="section-header"><div><h2 id="market-overview-heading">市场总览</h2><p class="section-description">先看主要指数，再进入任意证券详情。</p></div><span v-if="indexesMeta || overviewMeta" class="overview-source">指数 {{ indexesMeta?.sources?.join('、') || '来源未知' }} · 宽度 {{ overviewMeta?.sources?.join('、') || '暂不可用' }}</span></div>
      <div v-if="indexesLoading" class="state-panel compact" aria-live="polite"><strong>正在读取指数</strong><p>连接公开行情源。</p></div>
      <div v-else-if="indexesError" class="state-panel compact"><strong>指数暂不可用</strong><p>{{ indexesError }}</p><button class="btn-secondary" @click="loadIndexes">重试</button></div>
      <div v-else class="index-grid">
        <button v-for="item in indexes" :key="item.code" type="button" class="index-card" @click="selectQuote(item.code)">
          <span class="index-name">{{ item.name }} <small>{{ item.code }}</small></span>
          <strong class="numeric">{{ item.price?.toFixed(2) ?? '—' }}</strong>
          <span class="numeric" :class="directionClass(item.change_pct)">{{ formatPercentPoints(item.change_pct) }} {{ directionLabel(item.change_pct) }}</span>
          <small class="index-status" :class="{ stale: isStale(item) }">{{ freshnessLabel(item.freshness) }}</small>
        </button>
      </div>
      <div v-if="breadthLoading" class="breadth-loading">正在读取市场宽度…</div>
      <div v-else-if="breadthError" class="breadth-loading">市场宽度暂不可用：{{ breadthError }}</div>
      <div v-else-if="breadth" class="breadth-grid">
        <div><span>上涨</span><strong class="market-up numeric">{{ breadth.advancers?.toLocaleString('zh-CN') ?? '—' }}</strong></div>
        <div><span>下跌</span><strong class="market-down numeric">{{ breadth.decliners?.toLocaleString('zh-CN') ?? '—' }}</strong></div>
        <div><span>平盘</span><strong class="numeric">{{ breadth.unchanged?.toLocaleString('zh-CN') ?? '—' }}</strong></div>
        <div><span>成交额</span><strong class="numeric">{{ compactNumber(breadth.total_amount) }}</strong></div>
      </div>
      <p v-if="breadth" class="breadth-note">统计样本 {{ breadth.quoted_count.toLocaleString('zh-CN') }} 只 · 以行情源实际返回为准</p>
    </section>

    <section class="card universe-panel" aria-labelledby="universe-heading">
      <div class="section-header"><div><h2 id="universe-heading">全市场证券</h2><p class="section-description">搜索代码或名称，按交易所和板块筛选。</p></div><span v-if="universeMeta" class="overview-source">{{ universeMeta.total_count ?? 0 }} 只 · {{ universeMeta.source || '来源未知' }}</span></div>
      <form class="universe-toolbar" @submit.prevent="submitUniverseSearch">
        <label class="search-box"><span class="sr-only">搜索证券</span><input v-model="universeSearch" type="search" placeholder="输入股票代码或名称" autocomplete="off" /></label>
        <select v-model="universeExchange" aria-label="交易所"><option value="">全部交易所</option><option value="SH">沪市</option><option value="SZ">深市</option><option value="BJ">北交所</option></select>
        <select v-model="universeBoard" aria-label="板块"><option value="">全部板块</option><option value="主板">主板</option><option value="创业板">创业板</option><option value="科创板">科创板</option><option value="北交所">北交所</option></select>
        <button class="btn-secondary" type="submit" :disabled="universeLoading">{{ universeLoading ? '搜索中' : '搜索' }}</button>
      </form>
      <div v-if="universeError" class="state-panel compact"><strong>证券列表暂不可用</strong><p>{{ universeError }}</p><button class="btn-secondary" @click="loadUniverse">重试</button></div>
      <div v-else-if="universeLoading" class="state-panel compact" aria-live="polite"><strong>正在读取证券列表</strong><p>首次加载可能需要同步主数据。</p></div>
      <div v-else-if="universe.length === 0" class="state-panel compact"><strong>没有匹配证券</strong><p>调整搜索条件后重试。</p></div>
      <div v-else class="universe-list">
        <button v-for="item in universe" :key="item.code" type="button" class="universe-item" @click="selectQuote(item.code, item)">
          <span><strong>{{ item.name }}</strong><small>{{ item.code }}</small></span><span><small>{{ item.exchange }} · {{ item.board }}</small><b>查看行情 →</b></span>
        </button>
      </div>
      <div v-if="universeMeta && (universeMeta.page ?? 1) > 1 || (universeMeta && (universeMeta.page ?? 1) * (universeMeta.page_size ?? 50) < (universeMeta.total_count ?? 0))" class="pagination-row">
        <button class="btn-secondary" type="button" :disabled="universeLoading || universePage <= 1" @click="changeUniversePage(-1)">上一页</button><span>第 {{ universePage }} 页</span><button class="btn-secondary" type="button" :disabled="universeLoading || universePage * (universeMeta?.page_size ?? 50) >= (universeMeta?.total_count ?? 0)" @click="changeUniversePage(1)">下一页</button>
      </div>
    </section>

    <div class="market-layout">
      <aside class="card watch-panel">
        <div class="panel-title"><h2>自选证券</h2><span>{{ quotes.length }} / {{ watchCodes.length }}</span></div>
        <div v-if="quotesLoading" class="state-panel small" aria-live="polite"><strong>正在读取行情</strong><p>连接可用行情源。</p></div>
        <div v-else-if="quotesError" class="state-panel small"><strong>行情不可用</strong><p>{{ quotesError }}</p><button class="btn-secondary" @click="loadQuotes">重试</button></div>
        <div v-else-if="quotes.length === 0" class="state-panel small"><strong>暂无行情</strong><p>数据源没有返回有效证券。</p></div>
        <div v-else>
          <p v-if="quotesMeta?.status === 'partial'" class="partial-notice">部分行情可用；缺少 {{ quotesMeta.missing_codes?.join('、') || '未知证券' }}。</p>
          <div class="watch-list">
            <button
              v-for="quote in quotes"
              :key="quote.code"
              type="button"
              class="watch-item"
              :class="{ active: selectedCode === quote.code }"
              @click="selectQuote(quote.code)"
            >
              <span><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></span>
              <span class="quote-numbers"><strong class="numeric">{{ quote.price?.toFixed(2) ?? '—' }}</strong><small class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }} · {{ directionLabel(quote.change_pct) }}</small></span>
            </button>
          </div>
        </div>
      </aside>

      <section class="card chart-panel">
        <div class="chart-header">
          <div>
            <div class="symbol-row"><h2>{{ selectedQuote?.name || selectedSecurity?.name || selectedCode }}</h2><code>{{ selectedCode }}</code></div>
            <div v-if="selectedQuoteLoading" class="selected-quote-state">正在读取最新报价…</div>
            <div v-else-if="selectedQuoteError" class="selected-quote-state error">{{ selectedQuoteError }}</div>
            <div class="price-row" v-else-if="selectedQuote">
              <strong class="numeric">{{ selectedQuote.price?.toFixed(2) ?? '—' }}</strong>
              <span class="numeric" :class="directionClass(selectedQuote.change_pct)">{{ formatPercentPoints(selectedQuote.change_pct) }} {{ directionLabel(selectedQuote.change_pct) }}</span>
            </div>
          </div>
          <div class="source-meta" v-if="selectedQuote">
            <span :class="{ stale: isStale(selectedQuote) }">{{ freshnessLabel(selectedQuote.freshness) }}</span>
            <small>{{ selectedQuote.source }} · 源时间 {{ selectedQuote.as_of ?? '未知' }} · 接收 {{ formatReceivedAt(selectedQuote.received_at) }}</small>
          </div>
        </div>

        <div class="chart-toolbar" aria-label="K线周期和指标">
          <div class="interval-tabs" role="tablist" aria-label="K线周期">
            <button v-for="item in intervalOptions" :key="item.value" type="button" role="tab" :aria-selected="candleInterval === item.value" :class="{ active: candleInterval === item.value }" @click="changeCandleInterval(item.value)">{{ item.label }}</button>
          </div>
          <div class="indicator-toggles" aria-label="推荐指标">
            <button v-for="item in indicatorOptions" :key="item.value" type="button" :class="{ active: enabledIndicators.includes(item.value) }" @click="toggleIndicator(item.value)">{{ item.label }}</button>
          </div>
        </div>
        <div v-if="candleLoading" class="state-panel chart-state" aria-live="polite"><strong>正在加载历史价格</strong><p>读取最近一年的{{ intervalLabel }} K 线。</p></div>
        <div v-else-if="candleError" class="state-panel chart-state"><strong>历史价格不可用</strong><p>{{ candleError }}</p><button class="btn-secondary" @click="loadCandles">重试</button></div>
        <div v-else-if="candleRows.length === 0" class="state-panel chart-state"><strong>暂无历史价格</strong><p>数据源尚未包含 {{ selectedCode }} 的{{ intervalLabel }}记录。</p></div>
        <CandlestickChart
          v-else
          class="price-chart"
          :title="`${selectedCode} ${intervalLabel} K 线`"
          :data="candleRows.map(item => ({ ...item, open: item.open ?? null, close: item.close ?? null, low: item.low ?? null, high: item.high ?? null }))"
          :indicator-keys="overlayIndicatorKeys"
          zoom
        />
        <div v-if="latestIndicatorValues.length" class="indicator-summary" aria-label="最新指标">
          <span v-for="item in latestIndicatorValues" :key="item.label"><small>{{ item.label }}</small><strong class="numeric">{{ item.value }}</strong></span>
        </div>
        <p class="chart-footnote">数据源：{{ candleMeta?.source || candleSource }} · 复权口径：{{ candleMeta?.adjust_applied || 'event_driven' }}<span v-if="candleMeta?.warning_codes?.includes('ADJUSTMENT_FALLBACK')">（公开源前复权回退）</span> · 截止 {{ candleMeta?.as_of || '未知' }} · 仅用于研究展示。</p>
      </section>
    </div>

    <section class="section card" aria-labelledby="snapshot-heading">
      <div class="section-header"><div><h2 id="snapshot-heading">行情快照明细</h2><p class="section-description">明确标注来源、接收时间和回退状态。</p></div></div>
      <div v-if="quotes.length" class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>证券</th><th>最新价</th><th>涨跌幅</th><th>成交量</th><th>成交额</th><th>状态</th><th>来源</th></tr></thead>
          <tbody><tr v-for="quote in quotes" :key="quote.code">
            <td><strong>{{ quote.name }}</strong> <code>{{ quote.code }}</code></td>
            <td class="numeric">{{ quote.price?.toFixed(2) ?? '—' }}</td>
            <td class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }} {{ directionLabel(quote.change_pct) }}</td>
            <td class="numeric">{{ compactNumber(quote.volume) }}</td>
            <td class="numeric">{{ compactNumber(quote.amount) }}</td>
            <td><span class="freshness" :class="{ stale: isStale(quote) }">{{ freshnessLabel(quote.freshness) }}</span></td>
            <td>{{ quote.source }}<small v-if="quote.is_fallback" class="fallback">回退源</small></td>
          </tr></tbody>
        </table>
      </div>
      <div v-else class="state-panel"><strong>无快照数据</strong><p>刷新后仍无数据，请先检查数据健康页。</p></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import CandlestickChart from '@/components/charts/CandlestickChart.vue'
import { useApi } from '@/composables/useApi'
import type { CandleInterval, CandlePoint, CandleResponse, IndexesResponse, MarketOverviewResponse, MarketQuote, QuotesResponse, UniverseResponse, UniverseSecurity } from '@/types/api'
import { apiErrorMessage, chronological, compactNumber, directionClass, directionLabel, formatPercentPoints, freshnessLabel } from '@/utils/market'

const watchCodes = ['000001.SZ', '600519.SH', '600036.SH']
const { api } = useApi()
const quotes = ref<MarketQuote[]>([])
const selectedCode = ref(watchCodes[0])
const candleRows = ref<CandlePoint[]>([])
const candleMeta = ref<CandleResponse['meta'] | null>(null)
const candleSource = ref('local:parquet')
const quotesMeta = ref<QuotesResponse['meta'] | null>(null)
const indexes = ref<MarketQuote[]>([])
const indexesMeta = ref<IndexesResponse['meta'] | null>(null)
const indexesLoading = ref(true)
const indexesError = ref('')
const breadth = ref<MarketOverviewResponse['data'] | null>(null)
const overviewMeta = ref<MarketOverviewResponse['meta'] | null>(null)
const breadthLoading = ref(true)
const breadthError = ref('')
const universe = ref<UniverseSecurity[]>([])
const universeMeta = ref<UniverseResponse['meta'] | null>(null)
const universeSearch = ref('')
const universeExchange = ref('')
const universeBoard = ref('')
const universePage = ref(1)
const universeLoading = ref(true)
const universeError = ref('')
const selectedSecurityContext = ref<UniverseSecurity | null>(null)
const selectedAdHocQuote = ref<MarketQuote | null>(null)
const selectedQuoteLoading = ref(false)
const selectedQuoteError = ref('')
const quotesLoading = ref(true)
const candleLoading = ref(true)
const quotesError = ref('')
const candleError = ref('')
const candleInterval = ref<CandleInterval>('1d')
const enabledIndicators = ref<string[]>(['ma', 'boll'])
const intervalOptions: Array<{ value: CandleInterval; label: string }> = [
  { value: '1d', label: '日 K' }, { value: '1w', label: '周 K' }, { value: '1mo', label: '月 K' },
]
const indicatorOptions = [
  { value: 'ma', label: '均线' }, { value: 'boll', label: '布林带' }, { value: 'macd', label: 'MACD' }, { value: 'rsi14', label: 'RSI' }, { value: 'kdj', label: 'KDJ' },
]
let refreshTimer: number | undefined
let candleRequest = 0
let selectedQuoteRequest = 0
let disposed = false

const selectedQuote = computed(() => quotes.value.find(item => item.code === selectedCode.value) ?? indexes.value.find(item => item.code === selectedCode.value) ?? selectedAdHocQuote.value)
const selectedSecurity = computed(() => universe.value.find(item => item.code === selectedCode.value) ?? selectedSecurityContext.value)
const intervalLabel = computed(() => intervalOptions.find(item => item.value === candleInterval.value)?.label.replace(' K', '') ?? '日')
const overlayIndicatorKeys = computed(() => enabledIndicators.value.flatMap(group => {
  if (group === 'ma') return ['ma5', 'ma20', 'ma60']
  if (group === 'boll') return ['boll_mid', 'boll_upper', 'boll_lower']
  if (group === 'ema') return ['ema12', 'ema26']
  if (group === 'macd') return ['macd', 'macd_signal', 'macd_hist']
  if (group === 'rsi14') return ['rsi14']
  if (group === 'kdj') return ['kdj_k', 'kdj_d', 'kdj_j']
  return []
}))
const latestIndicatorValues = computed(() => {
  const latest = candleRows.value[candleRows.value.length - 1]
  if (!latest) return []
  const items: Array<{ label: string; value: string }> = []
  const value = (number: number | null | undefined, digits = 2) => number == null || !Number.isFinite(number) ? '—' : number.toFixed(digits)
  if (enabledIndicators.value.includes('macd')) items.push({ label: 'MACD', value: value(latest.macd) }, { label: 'DEA', value: value(latest.macd_signal) }, { label: '柱', value: value(latest.macd_hist) })
  if (enabledIndicators.value.includes('rsi14')) items.push({ label: 'RSI14', value: value(latest.rsi14) })
  if (enabledIndicators.value.includes('kdj')) items.push({ label: 'KDJ K', value: value(latest.kdj_k) }, { label: 'KDJ D', value: value(latest.kdj_d) }, { label: 'KDJ J', value: value(latest.kdj_j) })
  return items
})

function isoDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function isStale(quote: MarketQuote): boolean {
  return quote.freshness === 'stale' || quote.freshness === 'unknown'
}

function formatReceivedAt(value: string | null | undefined): string {
  if (!value) return '未知'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '未知'
  return parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

async function loadQuotes() {
  quotesLoading.value = true
  quotesError.value = ''
  try {
    const { data } = await api.get<QuotesResponse>('/market/quotes', { params: { codes: watchCodes.join(',') } })
    quotes.value = data.data
    quotesMeta.value = data.meta
  } catch (error: unknown) {
    quotesError.value = apiErrorMessage(error, '行情源暂不可用。')
    quotes.value = []
    quotesMeta.value = null
  } finally {
    quotesLoading.value = false
  }
}

async function loadIndexes() {
  indexesLoading.value = true
  indexesError.value = ''
  try {
    const { data } = await api.get<IndexesResponse>('/market/indexes')
    indexes.value = data.data
    indexesMeta.value = data.meta
  } catch (error: unknown) {
    indexesError.value = apiErrorMessage(error, '指数行情源暂不可用。')
    indexes.value = []
    indexesMeta.value = null
  } finally {
    indexesLoading.value = false
  }
}

async function loadSelectedQuote(code: string) {
  const request = ++selectedQuoteRequest
  selectedQuoteLoading.value = true
  selectedQuoteError.value = ''
  try {
    const { data } = await api.get<QuotesResponse>('/market/quotes', { params: { codes: code } })
    const found = data.data.find(item => item.code === code)
    if (!found) throw new Error('行情源未返回该证券的有效报价。')
    if (disposed || request !== selectedQuoteRequest) return
    selectedAdHocQuote.value = found
  } catch (error: unknown) {
    if (disposed || request !== selectedQuoteRequest) return
    selectedAdHocQuote.value = null
    selectedQuoteError.value = apiErrorMessage(error, '该证券的最新报价暂不可用。')
  } finally {
    if (!disposed && request === selectedQuoteRequest) selectedQuoteLoading.value = false
  }
}

async function loadBreadth() {
  breadthLoading.value = true
  breadthError.value = ''
  try {
    const { data } = await api.get<MarketOverviewResponse>('/market/breadth')
    breadth.value = data.data
    overviewMeta.value = data.meta
  } catch (error: unknown) {
    breadthError.value = apiErrorMessage(error, '市场宽度暂不可用。')
    breadth.value = null
  } finally {
    breadthLoading.value = false
  }
}

async function loadUniverse() {
  universeLoading.value = true
  universeError.value = ''
  const params: Record<string, string | number> = { page: universePage.value, page_size: 50 }
  if (universeSearch.value.trim()) params.search = universeSearch.value.trim()
  if (universeExchange.value) params.exchange = universeExchange.value
  if (universeBoard.value) params.board = universeBoard.value
  try {
    const { data } = await api.get<UniverseResponse>('/market/universe', { params })
    universe.value = data.data
    universeMeta.value = data.meta
  } catch (error: unknown) {
    universeError.value = apiErrorMessage(error, '证券列表源暂不可用。')
    universe.value = []
    universeMeta.value = null
  } finally {
    universeLoading.value = false
  }
}

function submitUniverseSearch() {
  universePage.value = 1
  loadUniverse()
}

function changeUniversePage(delta: number) {
  universePage.value = Math.max(1, universePage.value + delta)
  loadUniverse()
}

async function loadCandles() {
  const request = ++candleRequest
  const code = selectedCode.value
  const interval = candleInterval.value
  candleLoading.value = true
  candleError.value = ''
  const end = new Date()
  const start = new Date(end)
  if (interval === '1d') start.setFullYear(end.getFullYear() - 1)
  else start.setDate(end.getDate() - 900)
  try {
    const { data } = await api.get<CandleResponse>(`/market/candles/${code}`, {
      params: { start_date: isoDate(start), end_date: isoDate(end), interval, adjust: 'event_driven', indicators: enabledIndicators.value.join(',') },
    })
    if (disposed || request !== candleRequest || code !== selectedCode.value || interval !== candleInterval.value) return
    candleRows.value = chronological(data.data)
    candleMeta.value = data.meta
    candleSource.value = data.meta.source ?? data.data[0]?.source ?? 'local:parquet'
  } catch (error: unknown) {
    if (disposed || request !== candleRequest) return
    candleError.value = apiErrorMessage(error, '历史行情源暂不可用。')
    candleRows.value = []
    candleMeta.value = null
    candleSource.value = 'unavailable'
  } finally {
    if (!disposed && request === candleRequest) candleLoading.value = false
  }
}

function changeCandleInterval(interval: CandleInterval) {
  if (candleInterval.value === interval && !candleError.value) return
  candleInterval.value = interval
  void loadCandles()
}

function toggleIndicator(indicator: string) {
  enabledIndicators.value = enabledIndicators.value.includes(indicator)
    ? enabledIndicators.value.filter(item => item !== indicator)
    : [...enabledIndicators.value, indicator]
  void loadCandles()
}

function selectQuote(code: string, security: UniverseSecurity | null = null) {
  const knownQuote = quotes.value.some(item => item.code === code) || indexes.value.some(item => item.code === code)
  if (selectedCode.value === code) {
    if (security) selectedSecurityContext.value = security
    if (!knownQuote && !selectedQuoteLoading.value) void loadSelectedQuote(code)
    return
  }
  selectedCode.value = code
  selectedQuoteRequest += 1
  selectedSecurityContext.value = security
  selectedAdHocQuote.value = null
  selectedQuoteError.value = ''
  selectedQuoteLoading.value = false
  if (!knownQuote) void loadSelectedQuote(code)
  void loadCandles()
}

onMounted(async () => {
  await Promise.allSettled([loadQuotes(), loadCandles(), loadIndexes(), loadBreadth(), loadUniverse()])
  if (disposed) return
  refreshTimer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    const tasks: Promise<unknown>[] = []
    if (!quotesLoading.value) tasks.push(loadQuotes())
    if (!indexesLoading.value) tasks.push(loadIndexes())
    if (!breadthLoading.value) tasks.push(loadBreadth())
    const selectedIsKnown = quotes.value.some(item => item.code === selectedCode.value)
      || indexes.value.some(item => item.code === selectedCode.value)
    if (!selectedIsKnown && !selectedQuoteLoading.value) tasks.push(loadSelectedQuote(selectedCode.value))
    if (tasks.length) void Promise.allSettled(tasks)
  }, 30_000)
})

onBeforeUnmount(() => {
  disposed = true
  candleRequest += 1
  window.clearInterval(refreshTimer)
})
</script>

<style scoped>
.market-layout { display: grid; grid-template-columns: minmax(240px, .6fr) minmax(0, 2fr); gap: 12px; }
.overview-panel, .universe-panel { margin-bottom: 12px; }
.overview-panel .section-header, .universe-panel .section-header { align-items: flex-start; }
.overview-source { color: var(--text-tertiary); font-size: 10px; }
.index-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 7px; }
.index-card { display: grid; gap: 5px; border: 1px solid var(--border-subtle); border-radius: 7px; background: var(--bg-muted); color: var(--text-primary); padding: 11px; text-align: left; }
.index-card:hover { border-color: var(--border-strong); background: var(--bg-secondary); }
.index-name { display: grid; gap: 2px; font-size: 11px; font-weight: 600; }
.index-name small, .index-status { color: var(--text-tertiary); font-size: 9px; font-weight: 400; }
.index-card > strong { font-size: 17px; }
.index-card > span:not(.index-name) { font-size: 10px; }
.index-status.stale { color: #edbd62; }
.breadth-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-subtle); }
.breadth-grid div { display: grid; gap: 3px; }
.breadth-grid span, .breadth-loading { color: var(--text-tertiary); font-size: 10px; }
.breadth-grid strong { font-size: 16px; }
.breadth-loading { margin-top: 10px; }
.breadth-note { margin: 8px 0 0; color: var(--text-tertiary); font-size: 9px; }
.universe-toolbar { display: flex; gap: 7px; margin-bottom: 10px; }
.search-box { flex: 1; }
.search-box input, .universe-toolbar select { width: 100%; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-muted); color: var(--text-primary); padding: 8px 10px; font: inherit; font-size: 11px; }
.universe-toolbar select { max-width: 120px; }
.universe-list { display: grid; gap: 4px; }
.universe-item { display: flex; justify-content: space-between; align-items: center; width: 100%; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--text-primary); padding: 8px 10px; text-align: left; }
.universe-item:hover { border-color: var(--border-subtle); background: var(--bg-muted); }
.universe-item span { display: grid; gap: 3px; }
.universe-item small { color: var(--text-tertiary); font-size: 10px; }
.universe-item b { color: var(--accent); font-size: 10px; font-weight: 500; }
.pagination-row { display: flex; justify-content: center; align-items: center; gap: 12px; margin-top: 10px; color: var(--text-tertiary); font-size: 10px; }
.state-panel.compact { min-height: 64px; }
.watch-panel, .chart-panel { min-height: 510px; }
.panel-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.panel-title h2, .chart-header h2 { font-size: 15px; }
.panel-title span { color: var(--text-tertiary); font-size: 10px; }
.state-panel.small { min-height: 390px; }
.watch-list { display: grid; gap: 5px; }
.watch-item {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-primary);
  padding: 11px;
  text-align: left;
}
.watch-item:hover { background: var(--bg-secondary); }
.watch-item.active { border-color: var(--border-strong); background: var(--bg-muted); }
.watch-item span, .quote-numbers { display: grid; gap: 2px; }
.partial-notice { margin: 0 0 9px; color: var(--warning); font-size: 10px; }
.watch-item small { color: var(--text-tertiary); font-size: 10px; }
.quote-numbers { text-align: right; }
.chart-header { display: flex; min-height: 76px; align-items: flex-start; justify-content: space-between; gap: 20px; }
.symbol-row { display: flex; align-items: center; gap: 9px; }
.symbol-row code { color: var(--text-tertiary); font-size: 10px; }
.price-row { display: flex; align-items: baseline; gap: 10px; margin-top: 8px; }
.price-row strong { font-size: 25px; letter-spacing: -.04em; }
.price-row span { font-size: 11px; }
.selected-quote-state { margin-top: 8px; color: var(--text-tertiary); font-size: 10px; }
.selected-quote-state.error { color: var(--warning); }
.source-meta { display: grid; justify-items: end; gap: 5px; color: var(--text-tertiary); }
.source-meta span, .freshness { border: 1px solid rgba(54,179,126,.3); border-radius: 999px; background: var(--down-muted); color: #69c99e; padding: 3px 8px; font-size: 10px; }
.source-meta span.stale, .freshness.stale { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.08); color: #edbd62; }
.source-meta small { font-size: 10px; }
.chart-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 5px 0 8px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
.interval-tabs, .indicator-toggles { display: flex; flex-wrap: wrap; gap: 4px; }
.interval-tabs button, .indicator-toggles button { border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--text-tertiary); padding: 5px 8px; font: inherit; font-size: 10px; cursor: pointer; }
.interval-tabs button:hover, .indicator-toggles button:hover { color: var(--text-primary); background: var(--bg-muted); }
.interval-tabs button.active, .indicator-toggles button.active { border-color: rgba(77,141,255,.35); background: rgba(77,141,255,.12); color: #8bb3ff; }
.indicator-summary { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 8px; padding: 7px 9px; border-radius: 6px; background: var(--bg-muted); }
.indicator-summary span { display: inline-flex; align-items: baseline; gap: 5px; }
.indicator-summary small { color: var(--text-tertiary); font-size: 9px; }
.indicator-summary strong { font-size: 11px; }
.price-chart { height: 370px; }
.chart-state { min-height: 370px; }
.chart-footnote, .section-description { color: var(--text-tertiary); font-size: 10px; }
.fallback { display: inline-block; margin-left: 6px; color: var(--warning); font-size: 9px; }
@media (max-width: 900px) {
  .market-layout { grid-template-columns: 1fr; }
  .index-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .watch-panel { min-height: auto; }
  .state-panel.small { min-height: 180px; }
  .watch-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .index-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .breadth-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .universe-toolbar { flex-wrap: wrap; }
  .search-box { flex-basis: 100%; }
  .universe-toolbar select { max-width: none; flex: 1; }
  .watch-list { grid-template-columns: 1fr; }
  .chart-header { align-items: stretch; flex-direction: column; }
  .source-meta { justify-items: start; }
  .chart-toolbar { align-items: flex-start; flex-direction: column; }
  .price-chart { height: 330px; }
}
</style>
