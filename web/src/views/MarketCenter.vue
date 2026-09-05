<template>
  <div class="page market-page">
    <div class="page-header">
      <div><h1>市场行情</h1><p class="page-subtitle">查看 A 股实时快照与历史走势；本地样本不足时自动补充公开行情源。</p></div>
      <button class="btn-secondary" type="button" :disabled="quotesLoading" @click="loadQuotes">{{ quotesLoading ? '正在刷新' : '刷新行情' }}</button>
    </div>

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
            <div class="symbol-row"><h2>{{ selectedQuote?.name || selectedCode }}</h2><code>{{ selectedCode }}</code></div>
            <div class="price-row" v-if="selectedQuote">
              <strong class="numeric">{{ selectedQuote.price?.toFixed(2) ?? '—' }}</strong>
              <span class="numeric" :class="directionClass(selectedQuote.change_pct)">{{ formatPercentPoints(selectedQuote.change_pct) }} {{ directionLabel(selectedQuote.change_pct) }}</span>
            </div>
          </div>
          <div class="source-meta" v-if="selectedQuote">
            <span :class="{ stale: isStale(selectedQuote) }">{{ freshnessLabel(selectedQuote.freshness) }}</span>
            <small>{{ selectedQuote.source }} · 源时间 {{ selectedQuote.as_of ?? '未知' }} · 接收 {{ formatReceivedAt(selectedQuote.received_at) }}</small>
          </div>
        </div>

        <div v-if="dailyLoading" class="state-panel chart-state" aria-live="polite"><strong>正在加载历史价格</strong><p>读取最近一年的本地复权日线。</p></div>
        <div v-else-if="dailyError" class="state-panel chart-state"><strong>历史价格不可用</strong><p>{{ dailyError }}</p><button class="btn-secondary" @click="loadDaily">重试</button></div>
        <div v-else-if="dailyPrices.length === 0" class="state-panel chart-state"><strong>暂无历史价格</strong><p>本地数据尚未包含 {{ selectedCode }} 的日线记录。</p></div>
        <CandlestickChart
          v-else
          class="price-chart"
          :title="`${selectedCode} 日 K 线`"
          :data="dailyPrices.map(item => ({ date: item.date, open: item.open ?? null, close: item.close ?? null, low: item.low ?? null, high: item.high ?? null }))"
          zoom
        />
        <p class="chart-footnote">数据源：{{ dailySource }} · 复权口径：{{ dailySource === 'tencent:kline' ? 'qfq（前复权）' : 'event_driven' }} · 仅用于研究展示。</p>
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
import type { DailyPrice, MarketQuote, QuotesResponse } from '@/types/api'
import { apiErrorMessage, chronological, compactNumber, directionClass, directionLabel, formatPercentPoints, freshnessLabel } from '@/utils/market'

const watchCodes = ['000001.SZ', '600519.SH', '600036.SH']
const { api } = useApi()
const quotes = ref<MarketQuote[]>([])
const selectedCode = ref(watchCodes[0])
const dailyPrices = ref<DailyPrice[]>([])
const dailySource = ref('local:parquet')
const quotesMeta = ref<QuotesResponse['meta'] | null>(null)
const quotesLoading = ref(true)
const dailyLoading = ref(true)
const quotesError = ref('')
const dailyError = ref('')
let refreshTimer: number | undefined
let dailyRequest = 0
let disposed = false

const selectedQuote = computed(() => quotes.value.find(item => item.code === selectedCode.value))

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

async function loadDaily() {
  const request = ++dailyRequest
  dailyLoading.value = true
  dailyError.value = ''
  const end = new Date()
  const start = new Date(end)
  start.setFullYear(end.getFullYear() - 1)
  try {
    const { data } = await api.get<DailyPrice[]>(`/market/daily/${selectedCode.value}`, {
      params: { start_date: isoDate(start), end_date: isoDate(end), fields: 'open,high,low,close,volume' },
    })
    if (disposed || request !== dailyRequest) return
    dailyPrices.value = chronological(data)
    dailySource.value = data[0]?.source ?? 'local:parquet'
  } catch (error: unknown) {
    if (disposed || request !== dailyRequest) return
    dailyError.value = apiErrorMessage(error, '历史行情源暂不可用。')
    dailyPrices.value = []
    dailySource.value = 'unavailable'
  } finally {
    if (!disposed && request === dailyRequest) dailyLoading.value = false
  }
}

function selectQuote(code: string) {
  if (selectedCode.value === code) return
  selectedCode.value = code
  loadDaily()
}

onMounted(async () => {
  await Promise.allSettled([loadQuotes(), loadDaily()])
  if (disposed) return
  refreshTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !quotesLoading.value) loadQuotes()
  }, 30_000)
})

onBeforeUnmount(() => {
  disposed = true
  dailyRequest += 1
  window.clearInterval(refreshTimer)
})
</script>

<style scoped>
.market-layout { display: grid; grid-template-columns: minmax(240px, .6fr) minmax(0, 2fr); gap: 12px; }
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
.source-meta { display: grid; justify-items: end; gap: 5px; color: var(--text-tertiary); }
.source-meta span, .freshness { border: 1px solid rgba(54,179,126,.3); border-radius: 999px; background: var(--down-muted); color: #69c99e; padding: 3px 8px; font-size: 10px; }
.source-meta span.stale, .freshness.stale { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.08); color: #edbd62; }
.source-meta small { font-size: 10px; }
.price-chart { height: 370px; }
.chart-state { min-height: 370px; }
.chart-footnote, .section-description { color: var(--text-tertiary); font-size: 10px; }
.fallback { display: inline-block; margin-left: 6px; color: var(--warning); font-size: 9px; }
@media (max-width: 900px) {
  .market-layout { grid-template-columns: 1fr; }
  .watch-panel { min-height: auto; }
  .state-panel.small { min-height: 180px; }
  .watch-list { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .watch-list { grid-template-columns: 1fr; }
  .chart-header { align-items: stretch; flex-direction: column; }
  .source-meta { justify-items: start; }
  .price-chart { height: 330px; }
}
</style>
