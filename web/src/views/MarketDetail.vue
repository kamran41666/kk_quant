<template>
  <div class="page market-detail-page">
    <div class="detail-breadcrumb">
      <button class="back-link" type="button" @click="goBack">← 返回市场行情</button>
      <span>市场详情</span>
    </div>

    <section class="detail-hero card" aria-labelledby="detail-title">
      <div class="detail-identity">
        <p class="eyebrow">{{ marketLabel }} · 研究行情</p>
        <h1 id="detail-title">{{ quote?.name || routeName || symbol }}</h1>
        <div class="symbol-line"><code>{{ symbol }}</code><span>{{ quote?.asset_type === 'fund' ? '基金净值' : quote?.asset_type === 'index' || indexSymbols.has(symbol) ? '指数行情' : '证券行情' }}</span></div>
      </div>
      <div v-if="quote" class="hero-price">
        <strong class="numeric">{{ formatPrice(quote.price) }}</strong>
        <span class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }} {{ directionLabel(quote.change_pct) }}</span>
        <small>{{ quote.currency || currency }} · {{ freshnessLabel(quote.freshness) }}</small>
      </div>
      <div v-else-if="quoteLoading" class="hero-price"><span>正在读取报价…</span></div>
      <div v-else class="hero-price"><span class="warning-text">报价暂不可用</span></div>
      <div class="detail-actions">
        <button v-if="isTradable" class="btn-secondary" type="button" @click="openPaperOrder">进入模拟交易</button>
        <span v-else class="non-tradable-note">{{ market === 'gold' ? '黄金行情仅供观察，不支持直接交易' : '指数仅供观察，不支持直接交易' }}</span>
      </div>
    </section>

    <div v-if="quoteError" class="state-panel compact error-panel"><strong>最新报价暂不可用</strong><p>{{ quoteError }}</p><button class="btn-secondary" type="button" @click="loadQuote">重试</button></div>

    <section class="detail-grid">
      <article class="card quote-facts" aria-labelledby="facts-title">
        <div class="section-header compact"><div><h2 id="facts-title">行情摘要</h2></div><span v-if="quote" class="freshness" :class="{ stale: isStale }">{{ freshnessLabel(quote.freshness) }}</span></div>
        <dl class="facts-list">
          <div><dt>最新价</dt><dd class="numeric">{{ formatPrice(quote?.price) }}</dd></div>
          <div><dt>涨跌幅</dt><dd class="numeric" :class="directionClass(quote?.change_pct)">{{ formatPercentPoints(quote?.change_pct) }}</dd></div>
          <div><dt>成交量</dt><dd class="numeric">{{ compactNumber(quote?.volume) }}</dd></div>
          <div><dt>成交额</dt><dd class="numeric">{{ compactNumber(quote?.amount) }}</dd></div>
          <div><dt>源时间</dt><dd>{{ formatDateTime(quote?.as_of) }}</dd></div>
          <div><dt>数据源</dt><dd>{{ quote?.source || sourceLabel }}</dd></div>
        </dl>
      </article>

    </section>

    <section class="card performance-card" aria-labelledby="performance-title">
      <div class="section-header compact"><div><p class="eyebrow">区间表现</p><h2 id="performance-title">涨跌幅</h2></div><span class="chart-source">{{ rangeLabel }} · {{ intervalLabel }} K</span></div>
      <div class="performance-grid">
        <div class="performance-item"><span>当日</span><strong class="numeric" :class="directionClass(dayChange)" >{{ formatPercentPoints(dayChange) }}</strong></div>
        <div class="performance-item"><span>{{ rangeLabel }}</span><strong class="numeric" :class="directionClass(rangeReturn)">{{ formatPercentPoints(rangeReturn) }}</strong></div>
        <div class="performance-item"><span>区间高点</span><strong class="numeric">{{ formatPrice(rangeHigh) }}</strong></div>
        <div class="performance-item"><span>区间低点</span><strong class="numeric">{{ formatPrice(rangeLow) }}</strong></div>
      </div>
    </section>

    <section class="card chart-card" aria-labelledby="chart-title">
      <div class="section-header chart-section-header">
        <div><p class="eyebrow">价格走势</p><h2 id="chart-title">{{ symbol }} K 线与指标</h2></div>
        <span class="chart-source">{{ candleMeta?.source || sourceLabel }} · 截止 {{ candleMeta?.as_of || '未知' }}</span>
      </div>
      <div class="chart-toolbar" aria-label="K线周期和指标">
        <div class="toolbar-group"><div class="chart-view-toggle" role="tablist" aria-label="图表类型"><button type="button" role="tab" :aria-selected="chartType === 'candlestick'" :class="{ active: chartType === 'candlestick' }" @click="chartType = 'candlestick'">K线</button><button type="button" role="tab" :aria-selected="chartType === 'line'" :class="{ active: chartType === 'line' }" @click="chartType = 'line'">走势</button></div><div class="interval-tabs" role="tablist" aria-label="K线周期"><button v-for="item in intervalOptions" :key="item.value" type="button" role="tab" :aria-selected="interval === item.value" :class="{ active: interval === item.value }" @click="changeInterval(item.value)">{{ item.label }}</button></div></div>
        <div class="indicator-toggles" aria-label="技术指标"><button v-for="item in indicatorOptions" :key="item.value" type="button" :class="{ active: indicators.includes(item.value) }" @click="toggleIndicator(item.value)">{{ item.label }}</button></div>
      </div>
      <div class="range-toolbar" role="tablist" aria-label="时间范围"><span>时间范围</span><button v-for="item in rangeOptions" :key="item.value" type="button" role="tab" :aria-selected="rangeKey === item.value" :class="{ active: rangeKey === item.value }" @click="changeRange(item.value)">{{ item.label }}</button></div>
      <div v-if="candleLoading" class="state-panel chart-state" aria-live="polite"><strong>正在加载历史价格</strong><p>读取{{ rangeLabel }}可用数据。</p></div>
      <div v-else-if="candleError" class="state-panel chart-state"><strong>历史价格暂不可用</strong><p>{{ candleError }}</p><button class="btn-secondary" type="button" @click="loadCandles">重试</button></div>
      <div v-else-if="!candleRows.length" class="state-panel chart-state"><strong>暂无历史价格</strong><p>该标的目前没有可用的历史 K 线。</p></div>
      <LineChart v-else-if="chartType === 'line'" class="detail-chart line-detail-chart" :labels="lineLabels" :series="lineSeries" :title="`${symbol} ${rangeLabel} 走势`" percent zoom />
      <CandlestickChart v-else class="detail-chart" :class="chartDensity" :title="`${symbol} ${intervalLabel} K 线`" :data="candleRows.map(item => ({ ...item, open: item.open ?? null, close: item.close ?? null, low: item.low ?? null, high: item.high ?? null }))" :indicator-keys="overlayIndicatorKeys" zoom />
      <div v-if="latestIndicators.length" class="indicator-summary"><span v-for="item in latestIndicators" :key="item.label"><small>{{ item.label }}</small><strong class="numeric">{{ item.value }}</strong></span></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
const CandlestickChart = defineAsyncComponent(() => import('@/components/charts/CandlestickChart.vue'))
import { useApi } from '@/composables/useApi'
import type { CandleInterval, CandlePoint, CandleResponse, MarketQuote, QuotesResponse } from '@/types/api'
import { apiErrorMessage, chronological, compactNumber, directionClass, directionLabel, formatPercentPoints, freshnessLabel } from '@/utils/market'

type MarketId = 'a-share' | 'cn-fund' | 'us-equity' | 'gold'
const route = useRoute()
const router = useRouter()
const { api } = useApi()
const market = computed<MarketId>(() => route.query.market === 'cn-fund' || route.query.market === 'us-equity' || route.query.market === 'gold' ? route.query.market : 'a-share')
const symbol = computed(() => String(route.query.symbol || '').trim().toUpperCase())
const routeName = computed(() => String(route.query.name || '').trim())
const marketLabel = computed(() => market.value === 'a-share' ? 'A 股' : market.value === 'cn-fund' ? '国内基金' : market.value === 'gold' ? '黄金' : '美股')
const currency = computed(() => market.value === 'us-equity' ? 'USD' : market.value === 'gold' ? 'CNY/USD' : 'CNY')
const sourceLabel = computed(() => market.value === 'a-share' ? 'tencent:qt / local:parquet' : market.value === 'cn-fund' ? 'eastmoney:fund_nav' : market.value === 'gold' ? 'sina:gold + yahoo:chart' : 'yahoo:chart')
const indexSymbols = new Set(['000001.SH', '399001.SZ', '399006.SZ', '000300.SH', '000016.SH', '000905.SH', '^NDX', '^DJI', '^GSPC'])
const isTradable = computed(() => market.value !== 'gold' && quote.value?.asset_type !== 'index' && quote.value?.asset_type !== 'commodity' && !indexSymbols.has(symbol.value))
const quote = ref<MarketQuote | null>(null)
const quoteLoading = ref(false)
const quoteError = ref('')
const candleRows = ref<CandlePoint[]>([])
const candleMeta = ref<CandleResponse['meta'] | null>(null)
const candleLoading = ref(false)
const candleError = ref('')
const interval = ref<CandleInterval>('1d')
type RangeKey = '1m' | '3m' | '6m' | '1y' | '3y'
const rangeKey = ref<RangeKey>('1y')
const rangeOptions: Array<{ value: RangeKey; label: string }> = [{ value: '1m', label: '近1月' }, { value: '3m', label: '近3月' }, { value: '6m', label: '近6月' }, { value: '1y', label: '近1年' }, { value: '3y', label: '近3年' }]
const indicators = ref<string[]>(['ma', 'boll'])
const chartType = ref<'candlestick' | 'line'>('candlestick')
const LineChart = defineAsyncComponent(() => import('@/components/charts/LineChart.vue'))
const intervalOptions: Array<{ value: CandleInterval; label: string }> = [{ value: '1d', label: '日 K' }, { value: '1w', label: '周 K' }, { value: '1mo', label: '月 K' }]
const indicatorOptions = [{ value: 'ma', label: '均线' }, { value: 'boll', label: '布林带' }, { value: 'macd', label: 'MACD' }, { value: 'rsi14', label: 'RSI' }, { value: 'kdj', label: 'KDJ' }]
const intervalLabel = computed(() => intervalOptions.find(item => item.value === interval.value)?.label.replace(' K', '') || '日')
const overlayIndicatorKeys = computed(() => indicators.value.flatMap(group => group === 'ma' ? ['ma5', 'ma20', 'ma60'] : group === 'boll' ? ['boll_mid', 'boll_upper', 'boll_lower'] : group === 'macd' ? ['macd', 'macd_signal', 'macd_hist'] : group === 'rsi14' ? ['rsi14'] : group === 'kdj' ? ['kdj_k', 'kdj_d', 'kdj_j'] : []))
const chartDensity = computed(() => `price-chart-density-${Math.min(4, Math.max(1, 1 + indicators.value.filter(item => ['macd', 'rsi14', 'kdj'].includes(item)).length))}`)
const latestIndicators = computed(() => {
  const latest = candleRows.value[candleRows.value.length - 1]
  if (!latest) return []
  const value = (input: number | null | undefined) => input == null || !Number.isFinite(input) ? '—' : input.toFixed(2)
  const items: Array<{ label: string; value: string }> = []
  if (indicators.value.includes('macd')) items.push({ label: 'MACD', value: value(latest.macd) }, { label: 'DEA', value: value(latest.macd_signal) }, { label: '柱', value: value(latest.macd_hist) })
  if (indicators.value.includes('rsi14')) items.push({ label: 'RSI14', value: value(latest.rsi14) })
  if (indicators.value.includes('kdj')) items.push({ label: 'KDJ K', value: value(latest.kdj_k) }, { label: 'KDJ D', value: value(latest.kdj_d) }, { label: 'KDJ J', value: value(latest.kdj_j) })
  return items
})
const rangeLabel = computed(() => rangeOptions.find(item => item.value === rangeKey.value)?.label || '近1年')
const validCloseRows = computed(() => candleRows.value.filter(item => typeof item.close === 'number' && Number.isFinite(item.close)))
const rangeReturn = computed(() => {
  const rows = validCloseRows.value
  if (rows.length < 2) return null
  const first = rows[0].close as number
  const last = rows[rows.length - 1].close as number
  return first === 0 ? null : ((last / first) - 1) * 100
})
const dayChange = computed(() => {
  if (quote.value?.change_pct != null && Number.isFinite(quote.value.change_pct)) return quote.value.change_pct
  const rows = validCloseRows.value
  if (rows.length < 2) return null
  const previous = rows[rows.length - 2].close as number
  const last = rows[rows.length - 1].close as number
  return previous === 0 ? null : ((last / previous) - 1) * 100
})
const rangeHigh = computed(() => {
  const values = validCloseRows.value.map(item => item.close as number)
  return values.length ? Math.max(...values) : null
})
const rangeLow = computed(() => {
  const values = validCloseRows.value.map(item => item.close as number)
  return values.length ? Math.min(...values) : null
})
const lineLabels = computed(() => candleRows.value.map(item => item.date))
const lineSeries = computed(() => {
  const first = validCloseRows.value[0]?.close
  const base = typeof first === 'number' && Number.isFinite(first) && first !== 0 ? first : null
  return [{ name: quote.value?.name || symbol.value, color: '#4d8dff', area: true, values: candleRows.value.map(item => {
    if (base == null || typeof item.close !== 'number' || !Number.isFinite(item.close)) return null
    return ((item.close / base) - 1) * 100
  }) }]
})
let disposed = false
let candleRequest = 0

const isStale = computed(() => quote.value?.freshness === 'stale' || quote.value?.freshness === 'unknown')
function formatPrice(value: number | null | undefined): string { if (value == null || !Number.isFinite(value)) return '—'; return value.toFixed(market.value === 'cn-fund' ? 4 : 2) }
function formatDateTime(value: string | null | undefined): string { if (!value) return '未知'; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? '未知' : parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) }
function isoDate(value: Date): string { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}` }
function subtractRange(value: Date, selected: RangeKey): Date {
  const result = new Date(value)
  if (selected === '1m') result.setMonth(result.getMonth() - 1)
  else if (selected === '3m') result.setMonth(result.getMonth() - 3)
  else if (selected === '6m') result.setMonth(result.getMonth() - 6)
  else if (selected === '3y') result.setFullYear(result.getFullYear() - 3)
  else result.setFullYear(result.getFullYear() - 1)
  return result
}

async function loadQuote() {
  if (!symbol.value) { quoteError.value = '缺少证券代码。'; return }
  quoteLoading.value = true; quoteError.value = ''
  try {
    const endpoint = market.value === 'a-share' ? '/market/quotes' : `/market/markets/${market.value}/quotes`
    const params = market.value === 'a-share' ? { codes: symbol.value } : { symbols: symbol.value }
    const { data } = await api.get<QuotesResponse>(endpoint, { params })
    const found = data.data.find(item => item.code.toUpperCase() === symbol.value)
    if (!found) throw new Error('行情源未返回该标的的有效报价。')
    if (!disposed) quote.value = found
  } catch (error: unknown) { if (!disposed) { quote.value = null; quoteError.value = apiErrorMessage(error, '最新报价暂不可用。') } }
  finally { if (!disposed) quoteLoading.value = false }
}

async function loadCandles() {
  if (!symbol.value) return
  const request = ++candleRequest; candleLoading.value = true; candleError.value = ''
  const end = new Date(); const start = subtractRange(end, rangeKey.value)
  try {
    const endpoint = market.value === 'a-share' ? `/market/candles/${encodeURIComponent(symbol.value)}` : `/market/markets/${market.value}/candles/${encodeURIComponent(symbol.value)}`
    const { data } = await api.get<CandleResponse>(endpoint, { params: { start_date: isoDate(start), end_date: isoDate(end), interval: interval.value, ...(market.value === 'a-share' ? { adjust: 'event_driven' } : {}), indicators: indicators.value.join(',') } })
    if (disposed || request !== candleRequest) return
    candleRows.value = chronological(data.data); candleMeta.value = data.meta
  } catch (error: unknown) { if (!disposed && request === candleRequest) { candleRows.value = []; candleMeta.value = null; candleError.value = apiErrorMessage(error, '历史行情暂不可用。') } }
  finally { if (!disposed && request === candleRequest) candleLoading.value = false }
}

function changeInterval(value: CandleInterval) { if (interval.value === value && candleRows.value.length) return; interval.value = value; void loadCandles() }
function changeRange(value: RangeKey) { if (rangeKey.value === value && candleRows.value.length) return; rangeKey.value = value; void loadCandles() }
function toggleIndicator(value: string) { indicators.value = indicators.value.includes(value) ? indicators.value.filter(item => item !== value) : [...indicators.value, value]; void loadCandles() }
function goBack() {
  // Keep the selected cross-market tab when returning from a direct detail URL.
  void router.push({ name: 'Market', query: market.value === 'a-share' ? {} : { market: market.value } })
}
function openPaperOrder() {
  void router.push({ name: 'Paper', query: {
    market: market.value,
    symbol: symbol.value,
    name: routeName.value || quote.value?.name || symbol.value,
    price: quote.value?.price != null ? String(quote.value.price) : undefined,
  } })
}

onMounted(() => { void Promise.allSettled([loadQuote(), loadCandles()]) })
onBeforeUnmount(() => { disposed = true; candleRequest += 1 })
</script>

<style scoped>
.market-detail-page { display: grid; gap: 12px; }
.detail-breadcrumb { display: flex; align-items: center; gap: 10px; color: var(--text-tertiary); font-size: 11px; }
.back-link { border: 0; background: transparent; color: var(--accent-hover); padding: 0; font-size: 11px; cursor: pointer; }
.back-link:hover { color: var(--text-primary); }
.detail-hero { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 20px 22px; }
.eyebrow { color: var(--accent-hover); font-size: 10px; font-weight: 680; letter-spacing: .08em; text-transform: uppercase; }
.detail-identity h1 { margin-top: 5px; font-size: clamp(22px, 3vw, 32px); letter-spacing: -.03em; }
.symbol-line { display: flex; align-items: center; gap: 9px; margin-top: 7px; color: var(--text-tertiary); font-size: 11px; }
.symbol-line code { color: var(--text-secondary); }
.hero-price { display: grid; justify-items: end; gap: 5px; }
.hero-price strong { font-size: clamp(26px, 4vw, 42px); letter-spacing: -.03em; }
.hero-price span { font-size: 13px; }
.hero-price small { color: var(--text-tertiary); font-size: 10px; }
.detail-actions { display: flex; align-items: center; }
.non-tradable-note { color: var(--text-tertiary); font-size: 11px; white-space: nowrap; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
.quote-facts { min-width: 0; }
.section-header.compact { margin-bottom: 10px; }
.section-header h2 { font-size: 15px; }
.section-description { margin-top: 3px; }
.facts-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px 16px; margin: 0; }
.facts-list div { display: grid; gap: 3px; min-width: 0; }
.facts-list dt { color: var(--text-tertiary); font-size: 10px; }
.facts-list dd { margin: 0; overflow: hidden; color: var(--text-primary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.performance-card { min-width: 0; }
.performance-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; }
.performance-item { display: grid; gap: 5px; min-width: 0; padding: 11px 12px; border: 1px solid var(--border-subtle); border-radius: 7px; background: var(--bg-muted); }
.performance-item span { color: var(--text-tertiary); font-size: 10px; }
.performance-item strong { font-size: 17px; letter-spacing: -.02em; }
.chart-card { min-width: 0; }
.chart-section-header { align-items: flex-start; }
.chart-source { color: var(--text-tertiary); font-size: 10px; }
.chart-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 12px 0 8px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
.toolbar-group { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.chart-view-toggle, .interval-tabs, .indicator-toggles, .range-toolbar { display: flex; flex-wrap: wrap; gap: 4px; }
.chart-view-toggle { padding-right: 8px; border-right: 1px solid var(--border-subtle); }
.interval-tabs button, .indicator-toggles button { border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--text-tertiary); padding: 5px 9px; font-size: 10px; cursor: pointer; }
.chart-view-toggle button, .range-toolbar button { border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--text-tertiary); padding: 5px 9px; font-size: 10px; cursor: pointer; }
.interval-tabs button.active, .indicator-toggles button.active, .chart-view-toggle button.active, .range-toolbar button.active { border-color: rgba(77,141,255,.35); background: rgba(77,141,255,.12); color: #8bb3ff; }
.range-toolbar { align-items: center; gap: 5px; margin: 0 0 8px; }
.range-toolbar > span { margin-right: 3px; color: var(--text-tertiary); font-size: 10px; }
.detail-chart { height: 420px; }
.line-detail-chart { height: 350px; }
.detail-chart.price-chart-density-2 { height: 510px; }
.detail-chart.price-chart-density-3 { height: 590px; }
.detail-chart.price-chart-density-4 { height: 670px; }
.indicator-summary { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; padding: 9px 11px; border: 1px solid var(--border-subtle); border-radius: 7px; background: var(--bg-muted); }
.indicator-summary span { display: inline-flex; align-items: baseline; gap: 5px; }
.indicator-summary small { color: var(--text-tertiary); font-size: 9px; }
.indicator-summary strong { font-size: 11px; }
.error-panel { border-color: rgba(228, 168, 58, .35); }
@media (max-width: 720px) { .detail-hero, .chart-toolbar { align-items: stretch; flex-direction: column; } .hero-price { justify-items: start; } .detail-actions { align-items: stretch; } .detail-actions button { width: 100%; } .non-tradable-note { white-space: normal; } .detail-grid { grid-template-columns: 1fr; } .facts-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } .performance-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .toolbar-group { align-items: stretch; } .chart-view-toggle { padding-right: 0; border-right: 0; } .detail-chart { height: 360px; } .detail-chart.price-chart-density-2 { height: 440px; } .detail-chart.price-chart-density-3 { height: 520px; } .detail-chart.price-chart-density-4 { height: 600px; } .line-detail-chart { height: 320px; } }
</style>
