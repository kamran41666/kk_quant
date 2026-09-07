<template>
  <section class="cross-market" :class="{ 'cross-market--us': marketId === 'us-equity' }" aria-labelledby="cross-market-title">
    <div class="cross-market-head">
      <div>
        <p class="eyebrow">研究行情 · {{ marketLabel }}</p>
        <h2 id="cross-market-title">{{ marketLabel }}工作台</h2>
        <p class="cross-market-subtitle">独立数据源、独立币种和独立市场规则；仅用于研究与模拟观察。</p>
      </div>
      <button class="btn-secondary" type="button" :disabled="loading || indexLoading || candleLoading" @click="refresh">{{ loading || indexLoading || candleLoading ? '正在刷新' : '刷新行情' }}</button>
    </div>

    <section class="card cross-overview-card" aria-labelledby="cross-overview-title">
      <div class="section-header compact"><div><h3 id="cross-overview-title">市场总览</h3><p>当前观察池的涨跌与数据状态。</p></div><span class="source-note">{{ sourceLabel }}</span></div>
      <div class="cross-overview-stats">
        <div><span>标的数</span><strong class="numeric">{{ loading ? '—' : quotes.length }}</strong></div>
        <div><span>上涨</span><strong class="numeric market-up">{{ loading ? '—' : advancerCount }}</strong></div>
        <div><span>下跌</span><strong class="numeric market-down">{{ loading ? '—' : declinerCount }}</strong></div>
        <div><span>数据状态</span><strong>{{ loading ? '读取中' : '研究模式' }}</strong></div>
      </div>
    </section>

    <section v-if="marketId === 'us-equity' || marketId === 'gold'" class="card global-index-card" aria-labelledby="global-index-title">
        <div class="section-header compact"><div><h3 id="global-index-title">{{ marketId === 'gold' ? '金价基准' : '主要指数' }}</h3><p>点击标的查看详情。</p></div><span class="source-note">{{ indexMeta?.sources?.join('、') || sourceLabel }}</span></div>
      <div v-if="indexLoading" class="state-panel compact" aria-live="polite"><strong>正在读取指数</strong><p>连接 Yahoo 公开行情源。</p></div>
      <div v-else-if="indexError" class="state-panel compact"><strong>指数暂不可用</strong><p>{{ indexError }}</p><button class="btn-secondary" type="button" @click="loadIndexQuotes">重试</button></div>
      <div v-else class="global-index-grid">
        <button v-for="quote in indexQuotes" :key="quote.code" type="button" class="global-index-item" @click="openDetail(quote.code)">
          <span><strong>{{ quote.name }}</strong><small>{{ quote.code }} · {{ quote.currency || currency }}</small></span>
          <span><strong class="numeric">{{ formatPrice(quote.price) }}</strong><small class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }}</small></span>
          <span class="freshness" :class="{ stale: quote.freshness === 'stale' || quote.freshness === 'unknown' }">{{ freshnessLabel(quote.freshness) }}</span>
        </button>
      </div>
    </section>

    <section class="card cross-ranking-card" aria-labelledby="cross-ranking-title">
      <div class="section-header compact"><div><h3 id="cross-ranking-title">当日热门</h3><p>{{ marketId === 'us-equity' ? '按当前关注池成交量与涨幅排序。' : '按当前观察池涨幅排序。' }}</p></div><span class="source-note">{{ sourceLabel }}</span></div>
      <div class="cross-ranking-grid">
        <article class="cross-ranking-column">
          <div class="cross-ranking-head"><h4>活跃度</h4><span>{{ marketId === 'us-equity' ? '成交量' : '涨幅' }}</span></div>
          <div class="cross-ranking-list">
            <button v-for="quote in activeRankings" :key="`cross-active-${quote.code}`" type="button" class="cross-ranking-item" @click="openDetail(quote.code)">
              <span><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></span><span class="numeric">{{ rankingMetricValue(quote) }}</span><span class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }}</span>
            </button>
          </div>
        </article>
        <article class="cross-ranking-column">
          <div class="cross-ranking-head"><h4>涨幅排行</h4><span>当日涨跌幅</span></div>
          <div class="cross-ranking-list">
            <button v-for="quote in gainRankings" :key="`cross-gainer-${quote.code}`" type="button" class="cross-ranking-item" @click="openDetail(quote.code)">
              <span><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></span><span class="numeric">{{ formatPrice(quote.price) }}</span><span class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }}</span>
            </button>
          </div>
        </article>
      </div>
    </section>

    <section class="card global-watch-card" aria-labelledby="global-watch-title">
      <div class="section-header compact"><div><h3 id="global-watch-title">关注列表</h3><p>点击任意标的立即打开详情。</p></div><span class="source-note">{{ sourceLabel }}</span></div>
      <form class="symbol-form" @submit.prevent="addSymbol">
        <label class="global-symbol-input"><span class="sr-only">添加标的</span><input v-model="symbolInput" :placeholder="marketId === 'us-equity' ? '输入美股代码，如 AAPL' : marketId === 'gold' ? '输入黄金代码，如 GLD' : '输入基金代码，如 110022'" autocomplete="off" /></label>
        <button class="btn-primary" type="submit" :disabled="loading || !symbolInput.trim()">添加并查询</button>
      </form>
      <p v-if="error" class="global-error" role="alert">{{ error }}</p>
      <div class="global-watch-grid">
        <button v-for="quote in quotes" :key="quote.code" type="button" class="global-watch-item" :class="{ active: quote.code === selectedSymbol }" @click="openDetail(quote.code)">
          <span class="global-watch-name"><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }} · {{ quote.currency || currency }}</small></span>
          <span class="global-watch-price"><strong class="numeric">{{ formatPrice(quote.price) }}</strong><small class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }}</small></span>
          <span class="freshness" :class="{ stale: quote.freshness === 'stale' || quote.freshness === 'unknown' }">{{ freshnessLabel(quote.freshness) }}</span>
        </button>
      </div>
      <p v-if="meta" class="global-meta">{{ meta.returned_count ?? 0 }}/{{ meta.requested_count ?? 0 }} 个标的已返回 · {{ meta.sources?.join('、') || sourceLabel }} · {{ meta.research_only ? '研究模式' : '' }}</p>
    </section>

    <section ref="detailRef" class="card global-detail-card" aria-labelledby="global-detail-title">
      <div class="global-detail-head">
        <div>
          <p class="eyebrow">标的详情</p>
          <h3 id="global-detail-title">{{ selectedQuote?.name || selectedSymbol }}</h3>
          <code>{{ selectedSymbol }}</code>
        </div>
        <div v-if="selectedQuote" class="global-detail-price"><strong class="numeric">{{ formatPrice(selectedQuote.price) }}</strong><span class="numeric" :class="directionClass(selectedQuote.change_pct)">{{ formatPercentPoints(selectedQuote.change_pct) }}</span><small>{{ selectedQuote.currency || currency }} · {{ freshnessLabel(selectedQuote.freshness) }}</small></div>
      </div>
      <div class="global-chart-toolbar" aria-label="跨市场 K 线周期">
        <div class="interval-tabs" role="tablist"><button v-for="item in intervalOptions" :key="item.value" type="button" role="tab" :aria-selected="interval === item.value" :class="{ active: interval === item.value }" @click="changeInterval(item.value)">{{ item.label }}</button></div>
        <span class="source-note">{{ candleMeta?.source || sourceLabel }} · 截止 {{ candleMeta?.as_of || '未知' }}</span>
      </div>
      <div v-if="candleLoading" class="state-panel compact"><strong>正在加载历史行情</strong><p>读取最近一年可用数据。</p></div>
      <div v-else-if="candleError" class="state-panel compact"><strong>历史行情暂不可用</strong><p>{{ candleError }}</p><button class="btn-secondary" type="button" @click="loadCandles">重试</button></div>
      <CandlestickChart v-else-if="candleRows.length" class="global-chart" :title="`${selectedSymbol} ${intervalLabel} K 线`" :data="candleRows.map(item => ({ ...item, open: item.open ?? null, close: item.close ?? null, low: item.low ?? null, high: item.high ?? null }))" :indicator-keys="['ma5', 'ma20']" zoom />
      <div v-else class="state-panel compact"><strong>暂无历史行情</strong><p>该标的暂未返回可用历史数据。</p></div>
      <p v-if="candleMeta?.note" class="global-note">{{ candleMeta.note }}</p>
      <p class="global-footnote">数据源和时效仅代表公开研究接口状态，不代表交易所授权实时行情，也不能作为真实下单确认依据。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
const CandlestickChart = defineAsyncComponent(() => import('@/components/charts/CandlestickChart.vue'))
import { useApi } from '@/composables/useApi'
import type { CandleInterval, CandlePoint, CandleResponse, MarketQuote, QuotesResponse } from '@/types/api'
import { apiErrorMessage, chronological, directionClass, formatPercentPoints, freshnessLabel } from '@/utils/market'

type MarketId = 'cn-fund' | 'us-equity' | 'gold'
const props = defineProps<{ marketId: MarketId }>()
const { api } = useApi()
const router = useRouter()
const catalogs: Record<MarketId, Array<{ code: string; name: string }>> = {
  'cn-fund': [
    { code: '110022', name: '易方达消费行业股票' },
    { code: '161725', name: '招商中证白酒指数' },
    { code: '005827', name: '易方达蓝筹精选混合' },
  ],
  'us-equity': [
    { code: 'AAPL', name: 'Apple' },
    { code: 'MSFT', name: 'Microsoft' },
    { code: 'NVDA', name: 'NVIDIA' },
    { code: 'SPY', name: 'SPDR S&P 500 ETF' },
  ],
  gold: [
    { code: 'AU0', name: '沪金主连' },
    { code: 'XAU', name: '国际现货黄金' },
    { code: 'GC=F', name: 'COMEX黄金期货' },
    { code: 'GLD', name: 'SPDR黄金ETF' },
    { code: 'IAU', name: 'iShares黄金ETF' },
  ],
}
const usIndexCatalog = [
  { code: '^NDX', name: '纳斯达克100' },
  { code: '^DJI', name: '道琼斯工业指数' },
  { code: '^GSPC', name: '标普500' },
]
const intervalOptions: Array<{ value: CandleInterval; label: string }> = [
  { value: '1d', label: '日 K' }, { value: '1w', label: '周 K' }, { value: '1mo', label: '月 K' },
]
const initialCatalogItem = props.marketId === 'gold'
  ? catalogs.gold.find(item => item.code === 'GC=F') || catalogs.gold[0]
  : catalogs[props.marketId][0]
const initialCode = initialCatalogItem.code
const initialName = initialCatalogItem.name
const quotes = ref<MarketQuote[]>([])
const meta = ref<QuotesResponse['meta'] | null>(null)
const indexQuotes = ref<MarketQuote[]>([])
const indexMeta = ref<QuotesResponse['meta'] | null>(null)
const selectedSymbol = ref(initialCode)
const selectedName = ref(initialName)
const symbolInput = ref('')
const loading = ref(false)
const indexLoading = ref(false)
const error = ref('')
const indexError = ref('')
const candleRows = ref<CandlePoint[]>([])
const candleMeta = ref<CandleResponse['meta'] | null>(null)
const candleLoading = ref(false)
const candleError = ref('')
const interval = ref<CandleInterval>('1d')
const detailRef = ref<HTMLElement | null>(null)
let quoteRequest = 0
let indexRequest = 0
let candleRequest = 0
let disposed = false

const marketLabel = computed(() => props.marketId === 'us-equity' ? '美股' : props.marketId === 'gold' ? '黄金' : '国内基金')
const currency = computed(() => props.marketId === 'us-equity' ? 'USD' : props.marketId === 'gold' ? 'CNY/USD' : 'CNY')
const sourceLabel = computed(() => props.marketId === 'us-equity' ? 'yahoo:chart' : props.marketId === 'gold' ? 'sina:gold + yahoo:chart' : 'eastmoney:fund_nav')
const selectedQuote = computed(() => quotes.value.find(item => item.code === selectedSymbol.value) ?? { code: selectedSymbol.value, name: selectedName.value, price: null, change_pct: null, volume: null, amount: null, source: sourceLabel.value, as_of: null, received_at: '', freshness: 'unknown', is_fallback: false, currency: currency.value, market: props.marketId, asset_type: props.marketId === 'us-equity' ? 'equity' : props.marketId === 'gold' ? 'commodity' : 'fund' } as MarketQuote)
const intervalLabel = computed(() => intervalOptions.find(item => item.value === interval.value)?.label.replace(' K', '') ?? '日')
const activeRankings = computed(() => [...quotes.value].sort((left, right) => (right.amount ?? right.volume ?? right.change_pct ?? -Infinity) - (left.amount ?? left.volume ?? left.change_pct ?? -Infinity)).slice(0, 5))
const gainRankings = computed(() => [...quotes.value].filter(item => item.change_pct != null).sort((left, right) => (right.change_pct ?? -Infinity) - (left.change_pct ?? -Infinity)).slice(0, 5))
const advancerCount = computed(() => quotes.value.filter(item => (item.change_pct ?? 0) > 0).length)
const declinerCount = computed(() => quotes.value.filter(item => (item.change_pct ?? 0) < 0).length)

function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(props.marketId === 'cn-fund' ? 4 : 2)
}

function rankingMetricValue(quote: MarketQuote): string {
  const value = props.marketId === 'us-equity' || props.marketId === 'gold' ? quote.volume : quote.amount ?? quote.volume
  if (value == null || !Number.isFinite(value)) return formatPercentPoints(quote.change_pct)
  return value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(2)}B` : value >= 1_000_000 ? `${(value / 1_000_000).toFixed(2)}M` : value.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

async function loadQuotes() {
  const request = ++quoteRequest
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get<QuotesResponse>(`/market/markets/${props.marketId}/quotes`, { params: { symbols: catalogs[props.marketId].map(item => item.code).join(',') } })
    if (disposed || request !== quoteRequest) return
    quotes.value = data.data
    meta.value = data.meta
    const selected = data.data.find(item => item.code === selectedSymbol.value)
    if (selected) selectedName.value = selected.name
  } catch (err: unknown) {
    if (disposed || request !== quoteRequest) return
    quotes.value = []
    meta.value = null
    error.value = apiErrorMessage(err, '该市场行情暂不可用。')
  } finally {
    if (!disposed && request === quoteRequest) loading.value = false
  }
}

async function loadIndexQuotes() {
  if (props.marketId !== 'us-equity' && props.marketId !== 'gold') {
    indexQuotes.value = []
    indexMeta.value = null
    indexError.value = ''
    return
  }
  const request = ++indexRequest
  indexLoading.value = true
  indexError.value = ''
  try {
    const indexSymbols = props.marketId === 'gold' ? catalogs.gold.slice(0, 3).map(item => item.code) : usIndexCatalog.map(item => item.code)
    const indexMarket = props.marketId === 'gold' ? 'gold' : 'us-equity'
    const { data } = await api.get<QuotesResponse>(`/market/markets/${indexMarket}/quotes`, { params: { symbols: indexSymbols.join(',') } })
    if (disposed || request !== indexRequest) return
    indexQuotes.value = data.data
    indexMeta.value = data.meta
  } catch (err: unknown) {
    if (disposed || request !== indexRequest) return
    indexQuotes.value = []
    indexMeta.value = null
    indexError.value = apiErrorMessage(err, props.marketId === 'gold' ? '金价基准暂不可用。' : '美股指数行情暂不可用。')
  } finally {
    if (!disposed && request === indexRequest) indexLoading.value = false
  }
}

async function loadCandles() {
  const request = ++candleRequest
  candleLoading.value = true
  candleError.value = ''
  const end = new Date()
  const start = new Date(end)
  start.setFullYear(end.getFullYear() - 1)
  const iso = (value: Date) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
  try {
    const { data } = await api.get<CandleResponse>(`/market/markets/${props.marketId}/candles/${encodeURIComponent(selectedSymbol.value)}`, { params: { start_date: iso(start), end_date: iso(end), interval: interval.value, indicators: 'ma' } })
    if (disposed || request !== candleRequest) return
    candleRows.value = chronological(data.data)
    candleMeta.value = data.meta
  } catch (err: unknown) {
    if (disposed || request !== candleRequest) return
    candleRows.value = []
    candleMeta.value = null
    candleError.value = apiErrorMessage(err, '历史行情暂不可用。')
  } finally {
    if (!disposed && request === candleRequest) candleLoading.value = false
  }
}

function selectSymbol(symbol: string) {
  selectedSymbol.value = symbol
  const found = quotes.value.find(item => item.code === symbol)
  selectedName.value = found?.name || catalogs[props.marketId].find(item => item.code === symbol)?.name || symbol
  void loadCandles()
  void nextTick().then(() => detailRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

function openDetail(symbol: string) {
  const found = quotes.value.find(item => item.code === symbol)
  const name = found?.name || indexQuotes.value.find(item => item.code === symbol)?.name || catalogs[props.marketId].find(item => item.code === symbol)?.name
  void router.push({ name: 'MarketDetail', query: { market: props.marketId, symbol, ...(name ? { name } : {}) } })
}

function addSymbol() {
  const normalized = symbolInput.value.trim().toUpperCase()
  if (!normalized) return
  const exists = catalogs[props.marketId].some(item => item.code === normalized)
  if (!exists) catalogs[props.marketId].push({ code: normalized, name: normalized })
  symbolInput.value = ''
  selectSymbol(normalized)
  void loadQuotes()
}

function changeInterval(value: CandleInterval) {
  if (interval.value === value && candleRows.value.length) return
  interval.value = value
  void loadCandles()
}

async function refresh() {
  await Promise.allSettled([loadQuotes(), loadIndexQuotes(), loadCandles()])
}

watch(() => props.marketId, () => {
  const first = catalogs[props.marketId][0]
  selectedSymbol.value = first.code
  selectedName.value = first.name
  void refresh()
})

onMounted(() => { void refresh() })
onBeforeUnmount(() => { disposed = true; quoteRequest += 1; indexRequest += 1; candleRequest += 1 })
</script>

<style scoped>
.cross-market { display: grid; gap: 12px; }
.cross-market--us { grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr); align-items: start; }
.cross-market--us .cross-market-head,
.cross-market--us .global-watch-card,
.cross-market--us .global-detail-card { grid-column: 1 / -1; }
.cross-market--us .cross-overview-card,
.cross-market--us .global-index-card { grid-column: 1; }
.cross-market--us .cross-ranking-card { grid-column: 2; grid-row: 2 / span 2; }
.cross-market-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; }
.eyebrow { color: var(--accent-hover); font-size: 10px; font-weight: 680; letter-spacing: .08em; text-transform: uppercase; }
.cross-market-head h2 { margin-top: 3px; font-size: 19px; letter-spacing: -.02em; }
.cross-market-subtitle { margin-top: 4px; color: var(--text-secondary); font-size: 11px; }
.global-watch-card, .global-detail-card { min-width: 0; }
.cross-overview-card { min-width: 0; }
.cross-overview-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.cross-overview-stats div { display: grid; gap: 3px; min-width: 0; padding: 9px; border-radius: 6px; background: var(--bg-muted); }
.cross-overview-stats span { color: var(--text-tertiary); font-size: 9px; }
.cross-overview-stats strong { font-size: 14px; }
.global-index-card { min-width: 0; }
.section-header.compact { margin-bottom: 10px; }
.section-header h3, .global-detail-head h3 { font-size: 15px; }
.section-header p { margin-top: 3px; color: var(--text-tertiary); font-size: 10px; }
.source-note, .global-meta, .global-footnote, .global-note { color: var(--text-tertiary); font-size: 10px; }
.symbol-form { display: flex; gap: 8px; margin-bottom: 12px; }
.global-symbol-input { flex: 1; }
.global-symbol-input input { width: 100%; }
.global-watch-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }
.global-index-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
.global-index-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 10px; align-items: center; min-width: 0; border: 1px solid var(--border-subtle); border-radius: 7px; background: var(--bg-muted); color: var(--text-primary); padding: 11px; text-align: left; }
.global-index-item:hover { border-color: rgba(77,141,255,.52); background: rgba(77,141,255,.10); }
.global-index-item > span { display: grid; min-width: 0; gap: 2px; }
.global-index-item > span:nth-child(2) { justify-items: end; text-align: right; }
.global-index-item > .freshness { grid-column: 1 / -1; justify-self: start; }
.global-index-item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.global-index-item > span:nth-child(2) strong { font-size: 15px; }
.global-index-item small { color: var(--text-tertiary); font-size: 9px; }
.cross-ranking-card { min-width: 0; }
.cross-ranking-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.cross-ranking-column { min-width: 0; }
.cross-ranking-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.cross-ranking-head h4 { font-size: 11px; }
.cross-ranking-head span { color: var(--text-tertiary); font-size: 9px; }
.cross-ranking-list { display: grid; gap: 3px; }
.cross-ranking-item { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; align-items: center; width: 100%; border: 1px solid transparent; border-radius: 6px; background: var(--bg-muted); color: var(--text-primary); padding: 7px 9px; text-align: left; }
.cross-ranking-item:hover { border-color: var(--border-subtle); background: var(--bg-secondary); }
.cross-ranking-item > span:first-child { display: grid; min-width: 0; gap: 2px; }
.cross-ranking-item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; }
.cross-ranking-item small { color: var(--text-tertiary); font-size: 9px; }
.global-watch-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 10px; align-items: center; min-width: 0; border: 1px solid var(--border-subtle); border-radius: 7px; background: var(--bg-muted); color: var(--text-primary); padding: 11px; text-align: left; }
.global-watch-item:hover, .global-watch-item.active { border-color: rgba(77,141,255,.52); background: rgba(77,141,255,.10); }
.global-watch-item > .freshness { grid-column: 1 / -1; justify-self: start; }
.global-watch-name, .global-watch-price { display: grid; min-width: 0; gap: 2px; }
.global-watch-name strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.global-watch-name small, .global-watch-price small { color: var(--text-tertiary); font-size: 9px; }
.global-watch-price { justify-items: end; text-align: right; }
.global-watch-price strong { font-size: 15px; }
.freshness { width: max-content; border: 1px solid rgba(54,179,126,.3); border-radius: 999px; background: var(--down-muted); color: #69c99e; padding: 2px 7px; font-size: 9px; }
.freshness.stale { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.08); color: #edbd62; }
.global-error { margin: 0 0 9px; color: var(--warning); font-size: 11px; }
.global-meta { margin-top: 9px; }
.global-detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.global-detail-head code { display: inline-block; margin-top: 4px; color: var(--text-tertiary); font-size: 10px; }
.global-detail-price { display: grid; justify-items: end; gap: 3px; }
.global-detail-price strong { font-size: 25px; }
.global-detail-price span { font-size: 11px; }
.global-detail-price small { color: var(--text-tertiary); font-size: 10px; }
.global-chart-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 13px 0 8px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
.interval-tabs { display: flex; gap: 4px; }
.interval-tabs button { border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--text-tertiary); padding: 5px 9px; font-size: 10px; }
.interval-tabs button.active { border-color: rgba(77,141,255,.35); background: rgba(77,141,255,.12); color: #8bb3ff; }
.global-chart { height: 500px; }
.global-note { margin-top: 8px; color: var(--warning); }
.global-footnote { margin-top: 7px; line-height: 1.5; }
@media (max-width: 900px) { .cross-market--us { grid-template-columns: 1fr; } .cross-market--us .cross-market-head, .cross-market--us .cross-overview-card, .cross-market--us .global-index-card, .cross-market--us .cross-ranking-card, .cross-market--us .global-watch-card, .cross-market--us .global-detail-card { grid-column: 1; grid-row: auto; } .global-watch-grid, .global-index-grid, .cross-ranking-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 620px) { .cross-market-head, .global-detail-head, .global-chart-toolbar { align-items: stretch; flex-direction: column; } .cross-market-head .btn-secondary { width: 100%; } .global-detail-price { justify-items: start; } .global-watch-grid, .global-index-grid, .cross-ranking-grid { grid-template-columns: 1fr; } .cross-overview-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } .global-chart { height: 430px; } }
</style>
