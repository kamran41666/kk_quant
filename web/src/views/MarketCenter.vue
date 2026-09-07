<template>
  <div class="page market-page">
    <div class="page-header">
      <div><h1>市场行情</h1><p class="page-subtitle">统一查看 A 股、国内基金与美股的研究行情；每条数据均显示来源和时效。</p></div>
      <button v-if="activeMarket === 'a-share'" class="btn-secondary" type="button" :disabled="quotesLoading || indexesLoading || breadthLoading || universeQuotesLoading" @click="refreshMarket">{{ quotesLoading || indexesLoading || breadthLoading || universeQuotesLoading ? '正在刷新' : '刷新行情' }}</button>
    </div>

    <nav class="market-switcher" aria-label="市场切换" role="tablist">
      <button v-for="item in marketOptions" :key="item.id" type="button" role="tab" :aria-selected="activeMarket === item.id" :class="{ active: activeMarket === item.id }" @click="changeMarket(item.id)">
        <span>{{ item.label }}</span><small>{{ item.currency }}</small>
      </button>
    </nav>

    <template v-if="activeMarket === 'a-share'">
    <div class="market-primary-grid">
    <section class="card overview-panel" aria-labelledby="market-overview-heading">
      <div class="section-header"><div><h2 id="market-overview-heading">市场总览</h2><p class="section-description">先看主要指数，再进入任意证券详情。</p></div><span v-if="indexesMeta || overviewMeta" class="overview-source">指数 {{ indexesMeta?.sources?.join('、') || '来源未知' }} · 宽度 {{ overviewMeta?.sources?.join('、') || '暂不可用' }}<template v-if="overviewMeta"> · {{ overviewStatusLabel(overviewMeta.status) }} · {{ freshnessLabel(overviewMeta.freshness || 'unknown') }} · 接收 {{ formatReceivedAt(overviewMeta.received_at) }}</template></span></div>
      <div v-if="indexesLoading" class="state-panel compact" aria-live="polite"><strong>正在读取指数</strong><p>连接公开行情源。</p></div>
      <div v-else-if="indexesError" class="state-panel compact"><strong>指数暂不可用</strong><p>{{ indexesError }}</p><button class="btn-secondary" @click="loadIndexes">重试</button></div>
      <div v-else class="index-grid">
        <button v-for="item in indexes" :key="item.code" type="button" class="index-card" @click="openDetail(item.code, item.name)">
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
      <p v-if="breadth" class="breadth-note">统计样本 {{ breadth.quoted_count.toLocaleString('zh-CN') }} 只 · 涨跌数据 {{ overviewMeta?.valid_change_count ?? 0 }}/{{ breadth.quoted_count }} · 成交额数据 {{ overviewMeta?.valid_amount_count ?? 0 }}/{{ breadth.quoted_count }} · 以行情源实际返回为准</p>
    </section>

    <section class="card ranking-panel" aria-labelledby="ranking-heading">
      <div class="section-header"><div><h2 id="ranking-heading">当日热门</h2><p class="section-description">成交额活跃度与当日涨幅排行。</p></div><span class="overview-source">{{ overviewMeta?.sources?.join('、') || '来源未知' }}</span></div>
      <div class="ranking-grid">
        <article class="ranking-column">
          <div class="ranking-column-head"><h3>活跃度</h3><span>成交额 / 成交量</span></div>
          <div v-if="breadth?.top_active?.length" class="ranking-list">
            <button v-for="quote in breadth.top_active" :key="`active-${quote.code}`" type="button" class="ranking-item" @click="openDetail(quote.code, quote.name)">
              <span><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></span><span class="ranking-value numeric">{{ rankingValue(quote) }}</span><span class="numeric" :class="directionClass(quote.change_pct)">{{ formatPercentPoints(quote.change_pct) }}</span>
            </button>
          </div>
          <div v-else class="ranking-empty">暂无可用成交数据</div>
        </article>
        <article class="ranking-column">
          <div class="ranking-column-head"><h3>涨幅排行</h3><span>当日涨跌幅</span></div>
          <div v-if="breadth?.top_gainers?.length" class="ranking-list">
            <button v-for="quote in breadth.top_gainers" :key="`gainer-${quote.code}`" type="button" class="ranking-item" @click="openDetail(quote.code, quote.name)">
              <span><strong>{{ quote.name || quote.code }}</strong><small>{{ quote.code }}</small></span><span class="ranking-value numeric">{{ quote.price?.toFixed(2) ?? '—' }}</span><span class="numeric market-up">{{ formatPercentPoints(quote.change_pct) }}</span>
            </button>
          </div>
          <div v-else class="ranking-empty">暂无可用涨幅数据</div>
        </article>
      </div>
    </section>
    </div>

    <section class="card universe-panel" aria-labelledby="universe-heading">
      <div class="section-header"><div><h2 id="universe-heading">全市场证券</h2><p class="section-description">筛选后点击标的查看详情。</p></div><span v-if="universeMeta" class="overview-source">{{ universeMeta.total_count ?? 0 }} 只 · {{ universeMeta.source || '来源未知' }}</span></div>
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
        <button v-for="item in universe" :key="item.code" type="button" class="universe-item" @click="openDetail(item.code, item.name)">
          <span><strong>{{ item.name }}</strong><small>{{ item.code }}</small></span><span><small>{{ item.exchange }} · {{ item.board }}</small><b>查看行情 →</b></span>
          <span class="universe-quote" aria-label="当前行情">
            <template v-if="universeQuotes[item.code]"><strong class="numeric">{{ universeQuotes[item.code].price?.toFixed(2) ?? '—' }}</strong><small class="numeric" :class="directionClass(universeQuotes[item.code].change_pct)">{{ formatPercentPoints(universeQuotes[item.code].change_pct) }}</small></template>
            <small v-else-if="universeQuotesLoading">读取中…</small>
            <small v-else>—</small>
          </span>
        </button>
      </div>
      <p v-if="universeQuoteMeta" class="universe-quote-note">当前页行情：{{ universeQuoteMeta.sources?.join('、') || '来源未知' }}<span v-if="universeQuoteMeta.status === 'partial'"> · 部分证券未返回</span></p>
      <p v-if="universeQuoteError" class="universe-quote-note warning-text">当前页行情暂不可用：{{ universeQuoteError }}</p>
      <div v-if="universeMeta && (universeMeta.page ?? 1) > 1 || (universeMeta && (universeMeta.page ?? 1) * (universeMeta.page_size ?? 5) < (universeMeta.total_count ?? 0))" class="pagination-row">
        <button class="btn-secondary" type="button" :disabled="universeLoading || universePage <= 1" @click="changeUniversePage(-1)">上一页</button><span>第 {{ universePage }} 页</span><button class="btn-secondary" type="button" :disabled="universeLoading || universePage * (universeMeta?.page_size ?? 5) >= (universeMeta?.total_count ?? 0)" @click="changeUniversePage(1)">下一页</button>
      </div>
    </section>

    <section class="section card" aria-labelledby="snapshot-heading">
      <div class="section-header"><div><h2 id="snapshot-heading">行情快照明细</h2><p class="section-description">明确标注来源、接收时间和回退状态。</p></div></div>
      <div v-if="quotes.length" class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>证券</th><th>最新价</th><th>涨跌幅</th><th>成交量</th><th>成交额</th><th>状态</th><th>来源</th></tr></thead>
          <tbody><tr v-for="quote in quotes" :key="quote.code" class="snapshot-row" tabindex="0" @click="openDetail(quote.code, quote.name)" @keydown.enter="openDetail(quote.code, quote.name)">
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
    </template>

    <CrossMarketPanel v-else :market-id="activeMarket" />
  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
const CrossMarketPanel = defineAsyncComponent(() => import('@/components/market/CrossMarketPanel.vue'))
import { useApi } from '@/composables/useApi'
import type { IndexesResponse, MarketOverviewResponse, MarketQuote, QuotesResponse, UniverseResponse, UniverseSecurity } from '@/types/api'
import { apiErrorMessage, compactNumber, directionClass, directionLabel, formatPercentPoints, freshnessLabel } from '@/utils/market'

const watchCodes = ['000001.SZ', '600519.SH', '600036.SH']
type MarketId = 'a-share' | 'cn-fund' | 'us-equity'
const marketOptions: Array<{ id: MarketId; label: string; currency: string }> = [
  { id: 'a-share', label: 'A 股', currency: 'CNY' },
  { id: 'cn-fund', label: '国内基金', currency: 'CNY' },
  { id: 'us-equity', label: '美股', currency: 'USD' },
]
const activeMarket = ref<MarketId>('a-share')
const { api } = useApi()
const router = useRouter()
const quotes = ref<MarketQuote[]>([])
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
const universeQuotes = ref<Record<string, MarketQuote>>({})
const universeQuoteMeta = ref<QuotesResponse['meta'] | null>(null)
const universeQuotesLoading = ref(false)
const universeQuoteError = ref('')
const quotesLoading = ref(true)
const quotesError = ref('')
let refreshTimer: number | undefined
let universeQuoteRequest = 0
let disposed = false

function isStale(quote: MarketQuote): boolean {
  return quote.freshness === 'stale' || quote.freshness === 'unknown'
}

function formatReceivedAt(value: string | null | undefined): string {
  if (!value) return '未知'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '未知'
  return parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

function overviewStatusLabel(value: unknown): string {
  return value === 'ok' ? '完整统计' : value === 'partial' ? '部分统计' : '统计状态未知'
}

function rankingValue(quote: MarketQuote): string {
  return compactNumber(quote.amount ?? quote.volume)
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

async function refreshMarket() {
  const tasks: Promise<unknown>[] = [loadQuotes(), loadIndexes(), loadBreadth()]
  if (universe.value.length) tasks.push(loadUniverseQuotes(universe.value))
  await Promise.allSettled(tasks)
}

function changeMarket(market: MarketId) {
  activeMarket.value = market
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function openDetail(code: string, name?: string) {
  void router.push({ name: 'MarketDetail', query: { market: activeMarket.value, symbol: code, ...(name ? { name } : {}) } })
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
    overviewMeta.value = null
  } finally {
    breadthLoading.value = false
  }
}

async function loadUniverse() {
  universeLoading.value = true
  universeError.value = ''
  const params: Record<string, string | number> = { page: universePage.value, page_size: 5 }
  if (universeSearch.value.trim()) params.search = universeSearch.value.trim()
  if (universeExchange.value) params.exchange = universeExchange.value
  if (universeBoard.value) params.board = universeBoard.value
  try {
    const { data } = await api.get<UniverseResponse>('/market/universe', { params })
    universe.value = data.data
    universeMeta.value = data.meta
    void loadUniverseQuotes(data.data)
  } catch (error: unknown) {
    universeError.value = apiErrorMessage(error, '证券列表源暂不可用。')
    universe.value = []
    universeMeta.value = null
    universeQuoteRequest += 1
    universeQuotes.value = {}
    universeQuoteMeta.value = null
  } finally {
    universeLoading.value = false
  }
}

async function loadUniverseQuotes(rows: UniverseSecurity[]) {
  const request = ++universeQuoteRequest
  const codes = rows.map(item => item.code)
  universeQuotesLoading.value = Boolean(codes.length)
  universeQuoteMeta.value = null
  universeQuoteError.value = ''
  universeQuotes.value = {}
  if (!codes.length) {
    universeQuotesLoading.value = false
    return
  }
  try {
    const { data } = await api.get<QuotesResponse>('/market/quotes', { params: { codes: codes.join(',') } })
    if (disposed || request !== universeQuoteRequest) return
    universeQuotes.value = Object.fromEntries(data.data.map(item => [item.code, item]))
    universeQuoteMeta.value = data.meta
  } catch (error: unknown) {
    if (disposed || request !== universeQuoteRequest) return
    universeQuotes.value = {}
    universeQuoteMeta.value = null
    universeQuoteError.value = apiErrorMessage(error, '行情源暂不可用。')
  } finally {
    if (!disposed && request === universeQuoteRequest) universeQuotesLoading.value = false
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

onMounted(async () => {
  await Promise.allSettled([loadQuotes(), loadIndexes(), loadBreadth(), loadUniverse()])
  if (disposed) return
  refreshTimer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    const tasks: Promise<unknown>[] = []
    if (!quotesLoading.value) tasks.push(loadQuotes())
    if (!indexesLoading.value) tasks.push(loadIndexes())
    if (!breadthLoading.value) tasks.push(loadBreadth())
    if (universe.value.length && !universeQuotesLoading.value) tasks.push(loadUniverseQuotes(universe.value))
    if (tasks.length) void Promise.allSettled(tasks)
  }, 30_000)
})

onBeforeUnmount(() => {
  disposed = true
  window.clearInterval(refreshTimer)
})
</script>

<style scoped>
.market-switcher { display: inline-flex; gap: 4px; margin: -8px 0 14px; padding: 4px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-secondary); }
.market-switcher button { display: inline-flex; align-items: baseline; gap: 7px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--text-secondary); padding: 8px 13px; font-size: 12px; font-weight: 620; }
.market-switcher button small { color: var(--text-tertiary); font-family: "SFMono-Regular", Consolas, monospace; font-size: 9px; font-weight: 500; }
.market-switcher button:hover { color: var(--text-primary); background: var(--bg-muted); }
.market-switcher button.active { border-color: rgba(77,141,255,.35); background: rgba(77,141,255,.14); color: #a9c5ff; }
.snapshot-row { cursor: pointer; }
.snapshot-row:hover, .snapshot-row:focus-visible { background: rgba(77,141,255,.08); outline: none; }
.market-primary-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr); gap: 12px; align-items: start; }
.overview-panel, .universe-panel { margin-bottom: 12px; }
.ranking-panel { margin-bottom: 12px; }
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
.ranking-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.ranking-column { min-width: 0; }
.ranking-column-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.ranking-column-head h3 { font-size: 12px; }
.ranking-column-head span { color: var(--text-tertiary); font-size: 9px; }
.ranking-list { display: grid; gap: 3px; }
.ranking-item { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; align-items: center; width: 100%; border: 1px solid transparent; border-radius: 6px; background: var(--bg-muted); color: var(--text-primary); padding: 7px 9px; text-align: left; }
.ranking-item:hover { border-color: var(--border-subtle); background: var(--bg-secondary); }
.ranking-item > span:first-child { display: grid; min-width: 0; gap: 2px; }
.ranking-item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; }
.ranking-item small { color: var(--text-tertiary); font-size: 9px; }
.ranking-value { color: var(--text-secondary); font-size: 10px; }
.ranking-empty { padding: 16px 9px; border-radius: 6px; background: var(--bg-muted); color: var(--text-tertiary); font-size: 10px; }
.universe-toolbar { display: flex; gap: 7px; margin-bottom: 10px; }
.search-box { flex: 1; }
.search-box input, .universe-toolbar select { width: 100%; border: 1px solid var(--border-subtle); border-radius: 6px; background: var(--bg-muted); color: var(--text-primary); padding: 8px 10px; font: inherit; font-size: 11px; }
.universe-toolbar select { max-width: 120px; }
.universe-list { display: grid; gap: 4px; }
.universe-item { display: grid; grid-template-columns: minmax(180px, 1.35fr) minmax(150px, 1fr) minmax(90px, .5fr); align-items: center; width: 100%; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--text-primary); padding: 8px 10px; text-align: left; }
.universe-item:hover { border-color: var(--border-subtle); background: var(--bg-muted); }
.universe-item span { display: grid; min-width: 0; gap: 3px; }
.universe-item > span:nth-child(2) { justify-items: start; }
.universe-item > span:nth-child(3) { justify-items: end; }
.universe-item small { color: var(--text-tertiary); font-size: 10px; }
.universe-item b { color: var(--accent); font-size: 10px; font-weight: 500; }
.universe-quote { min-width: 68px; justify-items: end; text-align: right; }
.universe-quote strong { font-size: 12px; }
.universe-quote small { font-size: 9px; }
.universe-quote-note { margin: 8px 0 0; color: var(--text-tertiary); font-size: 9px; }
.warning-text { color: var(--warning); }
.pagination-row { display: flex; justify-content: center; align-items: center; gap: 12px; margin-top: 10px; color: var(--text-tertiary); font-size: 10px; }
.state-panel.compact { min-height: 64px; }
.freshness { border: 1px solid rgba(54,179,126,.3); border-radius: 999px; background: var(--down-muted); color: #69c99e; padding: 3px 8px; font-size: 10px; }
.freshness.stale { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.08); color: #edbd62; }
.section-description { color: var(--text-tertiary); font-size: 10px; }
.fallback { display: inline-block; margin-left: 6px; color: var(--warning); font-size: 9px; }
@media (max-width: 900px) {
  .market-primary-grid { grid-template-columns: 1fr; }
  .index-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .universe-item { grid-template-columns: minmax(140px, 1.2fr) minmax(120px, 1fr) minmax(82px, .6fr); }
}
@media (max-width: 620px) {
  .market-switcher { display: flex; width: 100%; margin-top: -4px; }
  .market-switcher button { flex: 1; justify-content: center; padding-inline: 7px; }
  .index-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .breadth-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ranking-grid { grid-template-columns: 1fr; }
  .universe-toolbar { flex-wrap: wrap; }
  .search-box { flex-basis: 100%; }
  .universe-toolbar select { max-width: none; flex: 1; }
  .universe-item { grid-template-columns: minmax(120px, 1fr) minmax(105px, .8fr) minmax(72px, .55fr); padding-inline: 7px; }
}
</style>
