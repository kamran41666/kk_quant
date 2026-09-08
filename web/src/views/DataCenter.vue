<template>
  <div class="page data-center">
    <header class="page-header">
      <div>
        <p class="page-kicker">LOCAL RESEARCH EVIDENCE</p>
        <h1>研究数据中心</h1>
        <p class="page-subtitle">盘点本地数据，验证回测区间，并看清每项市场能力的证据边界。</p>
      </div>
      <span class="local-chip"><span aria-hidden="true"></span> 本地只读检查</span>
    </header>

    <section class="hero" aria-labelledby="inventory-summary-heading">
      <div class="hero-copy">
        <span class="hero-label">DATA INVENTORY</span>
        <h2 id="inventory-summary-heading">先验证数据，再开始研究</h2>
        <p>库存日期只表示文件中的最早与最晚记录，不代表区间内每个交易日都有可用行情。</p>
      </div>
      <div class="metric-grid" aria-live="polite">
        <article class="metric"><span>本地证券</span><strong class="numeric">{{ summaryValue('security_count') }}</strong><small>Parquet 库存</small></article>
        <article class="metric"><span>行情行数</span><strong class="numeric">{{ summaryValue('row_count') }}</strong><small>所有文件合计</small></article>
        <article class="metric wide"><span>日期边界</span><strong class="numeric date-boundary"><b>{{ inventory?.summary.start_date || '—' }}</b><i aria-hidden="true">→</i><b>{{ inventory?.summary.end_date || '—' }}</b></strong><small>边界 ≠ 全区间覆盖</small></article>
        <article class="metric" :class="{ alert: (inventory?.summary.error_count ?? 0) > 0 }"><span>读取异常</span><strong class="numeric">{{ summaryValue('error_count') }}</strong><small>库存扫描结果</small></article>
      </div>
    </section>

    <div class="tabs" role="tablist" aria-label="研究数据视图">
      <button v-for="tab in tabs" :key="tab.id" type="button" role="tab" :aria-selected="activeTab === tab.id" :class="{ active: activeTab === tab.id }" @click="selectTab(tab.id)">
        {{ tab.label }}<span>{{ tab.caption }}</span>
      </button>
    </div>

    <section v-if="activeTab === 'inventory'" class="tab-panel" role="tabpanel">
      <section class="card research-snapshots">
        <div class="section-header"><div><h2>冻结研究数据集</h2><p>与下方日线缓存分开归档；数量来自固定版本清单。</p></div><router-link to="/backtest">查看研究结果 ↗</router-link></div>
        <p v-if="researchLoading" class="muted">正在读取研究清单…</p>
        <p v-else-if="researchError" class="error">{{ researchError }}</p>
        <p v-else-if="!researchDatasets.length" class="muted">暂无冻结研究数据集。</p>
        <div v-else class="research-dataset-grid">
          <article v-for="dataset in researchDatasets" :key="dataset.dataset_id" class="research-dataset">
            <div class="research-dataset-title"><strong>{{ dataset.dataset_id }}</strong><span class="state-pill" :class="dataset.latest ? 'good' : 'neutral'">{{ dataset.latest ? '最新归档' : '历史版本' }}</span></div>
            <p><b>{{ dataset.universe_count }}</b> 只证券 · <b>{{ formatInteger(dataset.row_count) }}</b> 行 · {{ dataset.action_count }} 条行动</p>
            <p class="numeric">{{ dataset.start_date }} → {{ dataset.end_date }}</p>
            <p>缺日 {{ dataset.missing_count }} · 非法行情 {{ dataset.invalid_rows }} · 待解释除权参考 {{ dataset.unresolved_actions }}</p>
            <code :title="dataset.content_hash">{{ shortHash(dataset.content_hash) }}</code>
          </article>
        </div>
      </section>
      <div class="panel-heading">
        <div><h2>A 股日频库存</h2><p>逐证券展示本地 Parquet 文件的物理边界与读取状态。</p></div>
        <button class="btn-secondary" type="button" :disabled="inventoryLoading" @click="loadInventory">{{ inventoryLoading ? '扫描中…' : '重新扫描' }}</button>
      </div>

      <div v-if="inventoryError" class="state-panel card"><strong>库存读取失败</strong><p>{{ inventoryError }}</p><button class="btn-secondary" type="button" @click="loadInventory">重试</button></div>
      <div v-else-if="inventoryLoading" class="state-panel card" aria-live="polite"><strong>正在扫描本地行情</strong><p>只读取本地行情，不修改研究数据。</p></div>
      <div v-else-if="!inventory || inventory.items.length === 0" class="state-panel card"><strong>暂无本地日频数据</strong><p>当前数据目录没有可展示的 A 股 Parquet 库存。</p></div>
      <template v-else>
        <div class="inventory-toolbar card">
          <label class="filter-input"><span class="sr-only">筛选证券代码</span><input v-model="inventoryFilter" type="search" placeholder="筛选证券代码，例如 600519" /></label>
          <span class="toolbar-meta">{{ filteredInventory.length }} 只匹配 · 已选 {{ selectedCodes.size }} 只</span>
          <button class="btn-secondary" type="button" :disabled="selectedCodes.size === 0" @click="useSelectedForCoverage">加入覆盖预检</button>
        </div>
        <div class="inventory-table card">
          <div class="data-table-wrap">
            <table class="data-table">
              <thead><tr><th class="check-cell"><span class="sr-only">选择</span></th><th>证券代码</th><th>文件</th><th>行情行</th><th>最早记录</th><th>最晚记录</th><th>读取状态</th></tr></thead>
              <tbody>
                <tr v-for="item in pagedInventory" :key="item.code">
                  <td class="check-cell"><input type="checkbox" :checked="selectedCodes.has(item.code)" :aria-label="`选择 ${item.code}`" @change="toggleSelected(item.code)" /></td>
                  <td><code>{{ item.code }}</code></td><td class="numeric">{{ formatInteger(item.file_count) }}</td><td class="numeric">{{ formatInteger(item.row_count) }}</td>
                  <td class="numeric">{{ item.start_date || '—' }}</td><td class="numeric">{{ item.end_date || '—' }}</td>
                  <td><span class="state-pill" :class="item.read_errors.length ? 'bad' : 'good'">{{ item.read_errors.length ? `${item.read_errors.length} 项异常` : '可读取' }}</span><details v-if="item.read_errors.length" class="row-details"><summary>查看</summary><p v-for="message in item.read_errors" :key="message">{{ message }}</p></details></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="pagination">
            <span>第 {{ inventoryPage }} / {{ inventoryPageCount }} 页</span>
            <div><button class="btn-secondary" type="button" :disabled="inventoryPage <= 1" @click="inventoryPage--">上一页</button><button class="btn-secondary" type="button" :disabled="inventoryPage >= inventoryPageCount" @click="inventoryPage++">下一页</button></div>
          </div>
        </div>
        <p class="boundary-note"><strong>边界说明</strong> 某证券存在首尾日期，不等于这段日期内无缺日、重复行或非法行情值。请用“覆盖预检”确认具体研究区间。</p>
      </template>
    </section>

    <section v-else-if="activeTab === 'coverage'" class="tab-panel" role="tabpanel">
      <div class="panel-heading"><div><h2>回测覆盖预检</h2><p>按 A 股交易日历核对本地原始 OHLCV，只做数据准入检查。</p></div><a v-if="coverageReport && reportDownloadUrl" class="btn-secondary" :href="reportDownloadUrl" :download="`daily-coverage-${coverageReport.start_date}-${coverageReport.end_date}.json`">下载 JSON 报告</a></div>
      <form class="coverage-form card" @submit.prevent="runCoverage">
        <label class="codes-field"><span>证券代码</span><textarea v-model="codesInput" rows="3" placeholder="000001.SZ, 600519.SH" spellcheck="false"></textarea><small>使用六位代码与 .SH / .SZ / .BJ，可用逗号、空格或换行分隔。</small></label>
        <div class="date-fields">
          <label><span>开始日期</span><input v-model="startDate" type="date" /></label><label><span>结束日期</span><input v-model="endDate" type="date" /></label>
        </div>
        <div class="form-actions"><button class="btn-primary" type="submit" :disabled="coverageLoading">{{ coverageLoading ? '正在核对…' : '运行预检' }}</button><button v-if="inventory?.items.length" class="text-button" type="button" @click="useAllInventoryForCoverage">选取全部本地证券</button></div>
        <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
      </form>

      <div v-if="coverageError" class="state-panel card"><strong>覆盖预检失败</strong><p>{{ coverageError }}</p><button class="btn-secondary" type="button" @click="runCoverage">重试</button></div>
      <div v-else-if="coverageLoading" class="state-panel card" aria-live="polite"><strong>正在核对交易日与 OHLCV</strong><p>证券较多或区间较长时可能需要一些时间。</p></div>
      <div v-else-if="!coverageReport" class="state-panel card"><strong>尚未生成预检报告</strong><p>输入证券与研究日期后运行预检；结果不会替你启动回测。</p></div>
      <div v-else class="report-stack" :class="{ stale: reportIsStale }">
        <div v-if="reportIsStale" class="stale-banner" role="status"><strong>报告已过期</strong><span>表单或选取证券已变更，请重新运行预检。下方仍展示上一次请求的结果。</span></div>
        <div class="report-overview card">
          <div><span class="report-status" :class="coverageReport.complete ? 'pass' : 'warn'">{{ coverageReport.complete ? '区间检查通过' : '发现覆盖问题' }}</span><h3>{{ coverageReport.start_date }} → {{ coverageReport.end_date }}</h3><p>{{ coverageReport.requested_codes.length }} 只证券 · {{ coverageReport.expected_trading_days }} 个预期交易日 · {{ coverageReport.source }}</p></div>
          <div class="calendar-state" :class="coverageReport.calendar_complete && coverageReport.calendar_verified ? 'verified' : 'warning'">
            <strong>{{ coverageReport.calendar_complete && coverageReport.calendar_verified ? '交易日历已验证' : '交易日历证据不完整' }}</strong>
            <span>{{ coverageReport.calendar_source || '日历来源未知' }} · {{ coverageReport.calendar_version || '版本未知' }}</span>
          </div>
        </div>
        <p class="scope-warning"><strong>预检边界</strong> 通过仅说明本地原始 OHLCV 与当前交易日历相符；公司行动、复权事件、历史成分、基本面和策略所需的其他数据仍须单独验证。</p>
        <article v-for="item in coverageReport.items" :key="item.code" class="coverage-item card">
          <div class="coverage-head"><div><code>{{ item.code }}</code><span class="state-pill" :class="item.status === 'complete' ? 'good' : 'bad'">{{ coverageStatus(item.status) }}</span></div><strong class="numeric">{{ coveragePercent(item) }}%</strong></div>
          <div class="progress-track" role="progressbar" :aria-label="`${item.code} 覆盖率`" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="Number(coveragePercent(item))"><span :style="{ width: `${coveragePercent(item)}%` }"></span></div>
          <div class="coverage-stats"><span>有记录交易日 <b class="numeric">{{ item.observed_trading_days }} / {{ item.expected_trading_days }}</b></span><span>缺日 <b class="numeric" :class="{ issue: item.missing_count }">{{ item.missing_count }}</b></span><span>重复行 <b class="numeric" :class="{ issue: item.duplicate_rows }">{{ item.duplicate_rows }}</b></span><span>非法行情行 <b class="numeric" :class="{ issue: item.invalid_field_rows }">{{ item.invalid_field_rows }}</b></span><span>非交易日记录 <b class="numeric" :class="{ issue: item.unexpected_count }">{{ item.unexpected_count }}</b></span></div>
          <p v-if="item.read_error" class="read-error"><strong>读取错误：</strong>{{ item.read_error }}</p>
          <details v-if="item.missing_dates.length" class="missing-days"><summary>展开缺失日期（已返回 {{ item.missing_dates.length }} / {{ item.missing_count }}）</summary><div><code v-for="day in item.missing_dates" :key="day">{{ day }}</code></div><p v-if="item.missing_dates_truncated">报告仅返回前 200 个缺失日期，JSON 中同样标记为已截断。</p></details>
        </article>
      </div>
    </section>

    <section v-else class="tab-panel" role="tabpanel">
      <div class="panel-heading"><div><h2>数据源与市场边界</h2><p>卡片表示代码中已接入的能力；只有 A 股健康接口提供本次进程的观察状态。</p></div><button class="btn-secondary" type="button" :disabled="sourcesLoading" @click="loadSources">{{ sourcesLoading ? '刷新中…' : '刷新观察' }}</button></div>
      <div class="source-grid">
        <article v-for="source in sources" :key="source.title" class="source-card card">
          <div class="source-top"><span class="source-icon">{{ source.mark }}</span><span v-if="source.health" class="state-pill" :class="healthClass(source.health)">{{ healthLabel(source.health) }}</span><span v-else class="state-pill neutral">已接入能力</span></div>
          <h3>{{ source.title }}</h3><p>{{ source.description }}</p><code>{{ source.adapters }}</code>
        </article>
      </div>
      <p v-if="healthError" class="inline-error">A 股数据源观察状态暂不可用：{{ healthError }}</p>

      <section class="capability card" aria-labelledby="capability-heading">
        <div class="section-header"><div><h2 id="capability-heading">跨市场研究能力</h2><p>当前可执行边界，不代表所有来源此刻在线。</p></div></div>
        <div class="data-table-wrap"><table class="data-table"><thead><tr><th>市场</th><th>本地研究 / 回测</th><th>纸面链路</th><th>主要边界</th></tr></thead><tbody><tr><td>A 股</td><td><span class="cap yes">日频事件回测</span></td><td>策略观察与模拟交易</td><td>基本面、历史成分需文件导入</td></tr><tr><td>国内基金</td><td><span class="cap yes">归档 NAV 回测</span></td><td>基金 NAV 纸面切片</td><td>依赖不可变 NAV 归档与有效日期</td></tr><tr><td>美股</td><td><span class="cap limited">仅行情</span></td><td>手动纸面</td><td>公司行动、调整价、日历与费用未闭环</td></tr><tr><td>黄金</td><td><span class="cap limited">仅行情</span></td><td>未接入</td><td>没有策略回测与执行证据链</td></tr></tbody></table></div>
      </section>

      <section class="fund-archive card" aria-labelledby="fund-heading">
        <div class="section-header"><div><h2 id="fund-heading">基金 NAV 本地归档</h2><p>东财历史 NAV 的不可变内容寻址快照，最多显示最近 50 条。</p></div></div>
        <div v-if="fundError" class="state-panel compact"><strong>基金归档暂不可用</strong><p>{{ fundError }}</p><button class="btn-secondary" type="button" @click="loadFunds">重试</button></div>
        <div v-else-if="fundLoading" class="state-panel compact"><strong>正在读取基金归档</strong></div>
        <div v-else-if="fundDatasets.length === 0" class="state-panel compact"><strong>暂无已归档基金数据</strong><p>这里只展示已持久化的 NAV 数据集。</p></div>
        <div v-else class="data-table-wrap"><table class="data-table"><thead><tr><th>基金代码</th><th>区间</th><th>行数</th><th>来源</th><th>归档时间</th><th>内容标识</th></tr></thead><tbody><tr v-for="fund in fundDatasets" :key="fund.dataset_id"><td><code>{{ fund.code }}</code></td><td class="numeric">{{ fund.start_date }} → {{ fund.end_date }}</td><td class="numeric">{{ formatInteger(fund.row_count) }}</td><td><code>{{ fund.source }}</code></td><td>{{ formatDateTime(fund.created_at) }}</td><td><code class="hash">{{ shortHash(fund.content_hash) }}</code></td></tr></tbody></table></div>
      </section>
      <p class="unfinished-note">尚未完成：授权数据源与生产 SLA、统一跨市场日历、基本面自动采集、历史成分自动归档，以及美股/黄金的可审计策略回测链路。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '@/composables/useApi'
import { apiErrorMessage } from '@/utils/market'
import { validResearchDate, validResearchDateRange } from '@/utils/research'
import type { DailyCoverageItem, DailyCoverageReport, DailyInventoryResponse, FundNavDataset, FundNavDatasetResponse, MarketHealthResponse, ProviderHealth } from '@/types/research'

type TabId = 'inventory' | 'coverage' | 'sources'
const tabs: Array<{ id: TabId; label: string; caption: string }> = [
  { id: 'inventory', label: '库存', caption: '本地资产' },
  { id: 'coverage', label: '覆盖预检', caption: '回测门禁' },
  { id: 'sources', label: '数据源', caption: '能力边界' },
]
const route = useRoute()
const router = useRouter()
const { api } = useApi()
const validTabs: TabId[] = ['inventory', 'coverage', 'sources']
const activeTab = ref<TabId>(validTabs.includes(String(route.query.tab) as TabId) ? String(route.query.tab) as TabId : 'inventory')
const inventory = ref<DailyInventoryResponse | null>(null)
const inventoryLoading = ref(true)
const inventoryError = ref('')
const inventoryFilter = ref('')
const inventoryPage = ref(1)
const selectedCodes = ref(new Set<string>())
interface ResearchDatasetSummary { dataset_id: string; content_hash: string; universe_count: number; row_count: number; action_count: number; start_date: string; end_date: string; missing_count: number; invalid_rows: number; unresolved_actions: number; latest: boolean }
const researchDatasets = ref<ResearchDatasetSummary[]>([])
const researchLoading = ref(true)
const researchError = ref('')
const initialDates = validResearchDateRange(route.query.start_date, route.query.end_date)
const codesInput = ref(validQueryCodes(route.query.codes))
const startDate = ref(initialDates?.start || defaultStartDate())
const endDate = ref(initialDates?.end || todayDate())
const formError = ref('')
const coverageReport = ref<DailyCoverageReport | null>(null)
const reportDownloadUrl = ref('')
const coverageReportSignature = ref('')
const coverageLoading = ref(false)
const coverageError = ref('')
const fundDatasets = ref<FundNavDataset[]>([])
const fundLoading = ref(true)
const fundError = ref('')
const health = ref<MarketHealthResponse | null>(null)
const healthError = ref('')
const healthLoading = ref(true)
let disposed = false
let inventoryRequest = 0
let coverageRequest = 0
let fundRequest = 0
let healthRequest = 0
let coverageController: AbortController | null = null

function scalarQuery(value: unknown): string { return Array.isArray(value) ? String(value[0] || '') : String(value || '') }
function validQueryCodes(value: unknown): string { const raw = scalarQuery(value); if (!raw) return ''; const codes = raw.split(',').map(code => code.trim().toUpperCase()).filter(Boolean); return codes.length && codes.every(code => /^\d{6}\.(SH|SZ|BJ)$/.test(code)) ? [...new Set(codes)].join(', ') : '' }
function todayDate(): string { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date()) }
function defaultStartDate(): string { const value = new Date(); value.setFullYear(value.getFullYear() - 1); return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(value) }
function parsedCodes(): string[] { return [...new Set(codesInput.value.toUpperCase().split(/[\s,，;；]+/).map(code => code.trim()).filter(Boolean))] }
function currentSignature(): string { return JSON.stringify({ codes: parsedCodes().sort(), start: startDate.value, end: endDate.value }) }

const filteredInventory = computed(() => { const needle = inventoryFilter.value.trim().toUpperCase(); return (inventory.value?.items ?? []).filter(item => !needle || item.code.includes(needle)) })
const inventoryPageCount = computed(() => Math.max(1, Math.ceil(filteredInventory.value.length / 10)))
const pagedInventory = computed(() => filteredInventory.value.slice((inventoryPage.value - 1) * 10, inventoryPage.value * 10))
const reportIsStale = computed(() => Boolean(coverageReport.value && coverageReportSignature.value !== currentSignature()))
const sourcesLoading = computed(() => healthLoading.value || fundLoading.value)
const healthProviders = computed<ProviderHealth[]>(() => { const providers = health.value?.providers; return Array.isArray(providers) ? providers : providers ? Object.values(providers) : [] })
const sources = computed(() => [
  { mark: 'TX', title: '腾讯 A 股', description: '公开快照与近期日线行情，适合研究工作台的轻量读取。', adapters: 'tencent:qt / tencent:kline', health: providerHealth(['tencent:qt']) },
  { mark: 'AK', title: 'AKShare A 股', description: '东财与新浪现货行情回退，状态来自本进程的真实观察。', adapters: 'akshare:eastmoney / akshare:sina', health: providerHealth(['akshare:eastmoney', 'akshare:sina']) },
  { mark: 'FN', title: '国内基金 NAV', description: '东财基金历史 NAV，可固化为不可变本地数据集。', adapters: 'eastmoney:fund_nav', health: '' },
  { mark: 'US', title: '美股行情', description: 'Yahoo Chart 的行情与历史价格研究入口。', adapters: 'yahoo:chart', health: '' },
  { mark: 'AU', title: '黄金行情', description: '新浪黄金与 Yahoo Chart 的现货、期货和 ETF 行情入口。', adapters: 'sina:gold + yahoo:chart', health: '' },
])

function providerHealth(names: string[]): string { if (!health.value) return healthError.value ? 'unavailable' : 'loading'; const matches = healthProviders.value.filter(item => names.includes(String(item.name || ''))); if (!matches.length) return 'unknown'; const states = matches.map(item => String(item.status || 'unknown')); if (states.every(state => state === 'ok')) return 'ok'; if (states.some(state => state === 'ok')) return 'degraded'; if (states.every(state => state === 'unknown')) return 'unknown'; return 'unavailable' }
function healthClass(value: string): string { return value === 'ok' ? 'good' : value === 'degraded' ? 'warn' : value === 'loading' ? 'neutral' : 'bad' }
function healthLabel(value: string): string { return ({ ok: '本进程已观测可用', degraded: '本进程部分可用', unknown: '尚未观测', unavailable: '本进程不可用', loading: '读取观察状态' } as Record<string, string>)[value] || `状态：${value}` }
function summaryValue(key: keyof DailyInventoryResponse['summary']): string { if (!inventory.value) return '—'; const value = inventory.value.summary[key]; return typeof value === 'number' ? formatInteger(value) : value || '—' }
function formatInteger(value: number): string { return Number.isFinite(value) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value) : '—' }
function coveragePercent(item: DailyCoverageItem): string { if (!item.expected_trading_days) return '0.0'; return Math.min(100, Math.max(0, item.observed_trading_days / item.expected_trading_days * 100)).toFixed(1) }
function coverageStatus(status: string): string { return ({ complete: '完整', partial: '部分覆盖', empty: '无数据', no_trading_days: '无交易日' } as Record<string, string>)[status] || status }
function formatDateTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) }
function shortHash(value: string): string { return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—' }

function selectTab(tab: TabId) { activeTab.value = tab; const query = tab === 'coverage' ? { ...route.query, tab } : { tab }; void router.replace({ query }) }
function toggleSelected(code: string) { const next = new Set(selectedCodes.value); next.has(code) ? next.delete(code) : next.add(code); selectedCodes.value = next }
function useSelectedForCoverage() { codesInput.value = [...selectedCodes.value].sort().join(', '); selectTab('coverage'); window.scrollTo({ top: 0, behavior: 'smooth' }) }
function useAllInventoryForCoverage() { codesInput.value = (inventory.value?.items ?? []).map(item => item.code).sort().join(', ') }

async function loadInventory() {
  const request = ++inventoryRequest; inventoryLoading.value = true; inventoryError.value = ''
  try { const { data } = await api.get<DailyInventoryResponse>('/market/inventory/daily'); if (disposed || request !== inventoryRequest) return; inventory.value = data }
  catch (error: unknown) { if (disposed || request !== inventoryRequest) return; inventory.value = null; inventoryError.value = apiErrorMessage(error, '无法读取本地日频库存。') }
  finally { if (!disposed && request === inventoryRequest) inventoryLoading.value = false }
}

async function loadResearchDatasets() {
  try {
    const { data } = await api.get<{ data: ResearchDatasetSummary[]; errors: Array<{ error: string }> }>('/market/research-datasets')
    if (disposed) return
    researchDatasets.value = data.data.sort((a, b) => Number(b.latest) - Number(a.latest))
    if (data.errors.length) researchError.value = `有 ${data.errors.length} 个研究清单读取异常`
  } catch (error) {
    if (!disposed) researchError.value = apiErrorMessage(error, '研究清单暂不可用')
  } finally {
    if (!disposed) researchLoading.value = false
  }
}

function validateCoverage(): boolean {
  formError.value = ''; const codes = parsedCodes()
  const invalid = codes.filter(code => !/^\d{6}\.(SH|SZ|BJ)$/.test(code))
  if (!codes.length) formError.value = '请至少输入一个证券代码。'
  else if (invalid.length) formError.value = `证券代码格式不正确：${invalid.slice(0, 3).join('、')}`
  else if (!validResearchDate(startDate.value) || !validResearchDate(endDate.value)) formError.value = '请选择有效的开始与结束日期。'
  else if (startDate.value > endDate.value) formError.value = '开始日期不能晚于结束日期。'
  else if ((Date.parse(`${endDate.value}T00:00:00Z`) - Date.parse(`${startDate.value}T00:00:00Z`)) / 86400000 > 3660) formError.value = '单次预检区间不能超过十年。'
  else if (codes.length > 500) formError.value = '单次预检最多支持 500 只证券。'
  return !formError.value
}

async function runCoverage() {
  if (!validateCoverage()) return
  coverageController?.abort(); const controller = new AbortController(); coverageController = controller
  const request = ++coverageRequest; const signature = currentSignature(); coverageLoading.value = true; coverageError.value = ''
  try {
    const { data } = await api.get<DailyCoverageReport>('/market/coverage/daily', { params: { codes: parsedCodes().join(','), start_date: startDate.value, end_date: endDate.value }, signal: controller.signal })
    if (disposed || request !== coverageRequest) return
    coverageReport.value = data; coverageReportSignature.value = signature
  } catch (error: unknown) {
    if (disposed || request !== coverageRequest || controller.signal.aborted) return
    coverageError.value = apiErrorMessage(error, '无法生成覆盖预检报告。')
  } finally { if (!disposed && request === coverageRequest) coverageLoading.value = false }
}

async function loadFunds() {
  const request = ++fundRequest; fundLoading.value = true; fundError.value = ''
  try { const { data } = await api.get<FundNavDatasetResponse>('/market/markets/cn-fund/datasets', { params: { limit: 50 } }); if (disposed || request !== fundRequest) return; fundDatasets.value = data.data }
  catch (error: unknown) { if (disposed || request !== fundRequest) return; fundDatasets.value = []; fundError.value = apiErrorMessage(error, '无法读取基金 NAV 归档。') }
  finally { if (!disposed && request === fundRequest) fundLoading.value = false }
}

async function loadHealth() {
  const request = ++healthRequest; healthLoading.value = true; healthError.value = ''
  try { const { data } = await api.get<MarketHealthResponse>('/market/health'); if (disposed || request !== healthRequest) return; health.value = data }
  catch (error: unknown) { if (disposed || request !== healthRequest) return; health.value = null; healthError.value = apiErrorMessage(error, '无法读取 A 股数据源状态。') }
  finally { if (!disposed && request === healthRequest) healthLoading.value = false }
}
async function loadSources() { await Promise.allSettled([loadHealth(), loadFunds()]) }
watch(coverageReport, report => {
  if (reportDownloadUrl.value) URL.revokeObjectURL(reportDownloadUrl.value)
  reportDownloadUrl.value = report ? URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json;charset=utf-8' })) : ''
})

watch(inventoryFilter, () => { inventoryPage.value = 1 })
watch([codesInput, startDate, endDate], () => { formError.value = ''; coverageError.value = ''; if (coverageLoading.value) { coverageController?.abort(); coverageRequest += 1; coverageLoading.value = false } })
watch(() => route.query.tab, value => { const tab = String(value || 'inventory') as TabId; if (validTabs.includes(tab)) activeTab.value = tab })
watch(() => [route.query.start_date, route.query.end_date, route.query.codes] as const, ([startQuery, endQuery, codesQuery]) => { const range = validResearchDateRange(startQuery, endQuery); const codes = validQueryCodes(codesQuery); if (range) { startDate.value = range.start; endDate.value = range.end } if (codes) codesInput.value = codes })
onMounted(() => { void loadInventory(); void loadSources(); void loadResearchDatasets() })
onBeforeUnmount(() => { disposed = true; inventoryRequest += 1; coverageRequest += 1; fundRequest += 1; healthRequest += 1; coverageController?.abort(); if (reportDownloadUrl.value) URL.revokeObjectURL(reportDownloadUrl.value) })
</script>

<style scoped>
.data-center { overflow: hidden; }
.research-snapshots { margin-bottom: 24px; }
.research-snapshots .section-header { flex-wrap: wrap; }
.research-snapshots a { font-size: 12px; }
.research-dataset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 12px; }
.research-dataset { min-width: 0; border: 1px solid var(--border); border-radius: 8px; padding: 15px; background: var(--bg-primary); }
.research-dataset-title { display: flex; align-items: flex-start; gap: 10px; justify-content: space-between; margin-bottom: 10px; }
.research-dataset-title strong { overflow-wrap: anywhere; font-size: 12px; }
.research-dataset p { margin-top: 5px; font-size: 11px; color: var(--text-secondary); }
.research-dataset code { display: block; margin-top: 10px; font-size: 10px; color: var(--text-tertiary); }
.page-kicker { margin-bottom: 7px; color: #61d7df; font-size: 10px; font-weight: 760; letter-spacing: .16em; }
.local-chip { display: inline-flex; align-items: center; gap: 8px; border: 1px solid rgba(97,215,223,.22); border-radius: 999px; background: rgba(97,215,223,.07); color: #9de8eb; padding: 6px 11px; font-size: 11px; }
.local-chip span { width: 6px; height: 6px; border-radius: 50%; background: #61d7df; box-shadow: 0 0 0 4px rgba(97,215,223,.09); }
.hero { position: relative; display: grid; grid-template-columns: minmax(0,.8fr) minmax(0,1.55fr); gap: 28px; overflow: hidden; border: 1px solid #243447; border-radius: var(--radius-lg); background: linear-gradient(135deg, #111a25 0%, #101722 57%, #0e1b25 100%); padding: clamp(22px,3vw,36px); box-shadow: var(--shadow-raised); }
.hero::after { position: absolute; width: 340px; height: 340px; border-radius: 50%; background: rgba(51,160,196,.08); content: ''; right: -170px; top: -240px; filter: blur(4px); }
.hero-copy { align-self: center; max-width: 430px; }
.hero-label { color: #61d7df; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; letter-spacing: .14em; }
.hero h2 { margin-top: 14px; font-size: clamp(21px,2.1vw,29px); letter-spacing: -.025em; }
.hero-copy p { margin-top: 10px; color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.metric-grid { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); border: 1px solid rgba(255,255,255,.065); border-radius: var(--radius-md); background: rgba(7,12,18,.3); }
.metric { min-width: 0; padding: 19px 17px; border-right: 1px solid rgba(255,255,255,.065); }
.metric:last-child { border-right: 0; }.metric span,.metric small { display: block; color: var(--text-tertiary); font-size: 10px; }.metric strong { display: block; overflow: hidden; margin: 8px 0 6px; color: var(--text-primary); font-size: clamp(20px,2vw,28px); text-overflow: ellipsis; white-space: nowrap; }.metric .date-boundary { display: grid; gap: 1px; overflow: visible; font-size: clamp(11px,1.1vw,15px); white-space: normal; }.date-boundary b { font: inherit; }.date-boundary i { color: var(--text-tertiary); font-size: 9px; font-style: normal; line-height: 1; }.metric.alert strong { color: var(--warning); }
.tabs { display: grid; grid-template-columns: repeat(3,1fr); margin-top: 26px; border-bottom: 1px solid var(--border); }
.tabs button { position: relative; display: flex; align-items: baseline; justify-content: center; gap: 8px; background: transparent; color: var(--text-secondary); padding: 13px; font-size: 13px; }.tabs button::after { position: absolute; height: 2px; content: ''; inset: auto 0 -1px; background: transparent; }.tabs button.active { color: var(--text-primary); }.tabs button.active::after { background: linear-gradient(90deg, transparent, #61d7df 24%, var(--accent) 76%, transparent); }.tabs button span { color: var(--text-tertiary); font-size: 10px; }
.tab-panel { margin-top: 24px; }.panel-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 13px; }.panel-heading h2 { font-size: 17px; }.panel-heading p,.section-header p { margin-top: 4px; color: var(--text-secondary); font-size: 12px; }.state-panel.card { margin-top: 0; }.state-panel.compact { min-height: 130px; }
.inventory-toolbar { display: flex; align-items: center; gap: 12px; padding: 13px 14px; }.filter-input { min-width: 180px; flex: 1; }.filter-input input { width: 100%; }.toolbar-meta { color: var(--text-tertiary); font-size: 11px; }.inventory-table { margin-top: 10px; padding: 4px 8px 8px; }.check-cell { width: 36px; }.check-cell input { width: 16px; height: 16px; min-height: 0; accent-color: var(--accent); }.state-pill { display: inline-flex; align-items: center; border: 1px solid var(--border); border-radius: 999px; padding: 3px 8px; font-size: 10px; white-space: nowrap; }.state-pill.good { border-color: rgba(54,179,126,.28); background: var(--down-muted); color: #69cca4; }.state-pill.bad { border-color: rgba(239,91,100,.28); background: var(--up-muted); color: #f78a90; }.state-pill.warn { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.09); color: #edbd62; }.state-pill.neutral { color: var(--text-secondary); background: var(--bg-secondary); }.row-details { display: inline-block; margin-left: 7px; color: var(--warning); font-size: 11px; }.row-details p { max-width: 420px; margin-top: 5px; white-space: normal; }.pagination { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid var(--border); color: var(--text-tertiary); padding: 12px 5px 4px; font-size: 11px; }.pagination div { display: flex; gap: 7px; }.pagination .btn-secondary { min-height: 31px; padding: 4px 10px; }.boundary-note,.scope-warning,.unfinished-note { margin-top: 12px; border-left: 2px solid var(--warning); background: rgba(228,168,58,.055); color: var(--text-secondary); padding: 10px 13px; font-size: 11px; }.boundary-note strong,.scope-warning strong { margin-right: 7px; color: #edbd62; }
.coverage-form { display: grid; grid-template-columns: minmax(300px,1fr) minmax(280px,.7fr) auto; align-items: end; gap: 16px; }.coverage-form label>span { display: block; margin-bottom: 6px; color: var(--text-secondary); font-size: 11px; font-weight: 620; }.codes-field textarea { width: 100%; resize: vertical; font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }.codes-field small { display: block; margin-top: 5px; color: var(--text-tertiary); font-size: 10px; }.date-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }.date-fields input { width: 100%; min-width: 0; }.form-actions { display: grid; gap: 5px; }.text-button { background: transparent; color: var(--accent-hover); padding: 4px; font-size: 10px; }.form-error { grid-column: 1/-1; color: var(--danger); font-size: 12px; }.report-stack { display: grid; gap: 11px; margin-top: 13px; }.report-stack.stale .coverage-item,.report-stack.stale .report-overview,.report-stack.stale .scope-warning { opacity: .58; }.stale-banner { position: relative; z-index: 1; display: flex; flex-wrap: wrap; gap: 6px 12px; border: 1px solid rgba(228,168,58,.35); border-radius: var(--radius-md); background: #2a2115; color: var(--text-secondary); padding: 11px 14px; font-size: 11px; }.stale-banner strong { color: #f1c774; }.report-overview { display: flex; justify-content: space-between; gap: 20px; }.report-status { display: inline-block; margin-bottom: 8px; font-size: 11px; font-weight: 700; }.report-status.pass { color: var(--success); }.report-status.warn { color: var(--warning); }.report-overview h3 { font-size: 16px; }.report-overview p { margin-top: 5px; color: var(--text-secondary); font-size: 11px; }.calendar-state { display: grid; align-content: center; min-width: 220px; border-left: 1px solid var(--border); padding-left: 20px; }.calendar-state strong { font-size: 12px; }.calendar-state span { margin-top: 4px; color: var(--text-tertiary); font-size: 10px; }.calendar-state.verified strong { color: var(--success); }.calendar-state.warning strong { color: var(--warning); }.coverage-item { padding: 17px 19px; }.coverage-head,.coverage-head>div { display: flex; align-items: center; justify-content: space-between; gap: 10px; }.coverage-head code { color: var(--text-primary); font-size: 13px; }.coverage-head>strong { font-size: 18px; }.progress-track { height: 5px; overflow: hidden; margin-top: 13px; border-radius: 99px; background: var(--bg-muted); }.progress-track span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg,#37c5c9,var(--accent)); }.coverage-stats { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 8px; margin-top: 13px; }.coverage-stats span { color: var(--text-tertiary); font-size: 10px; }.coverage-stats b { display: block; margin-top: 3px; color: var(--text-secondary); font-size: 12px; }.coverage-stats b.issue { color: var(--warning); }.read-error { margin-top: 11px; border-radius: 5px; background: var(--up-muted); color: #f2a1a5; padding: 8px 10px; font-size: 11px; word-break: break-word; }.missing-days { margin-top: 12px; color: var(--accent-hover); font-size: 11px; }.missing-days>div { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }.missing-days code { border: 1px solid var(--border); border-radius: 4px; background: var(--bg-secondary); color: var(--text-secondary); padding: 3px 6px; font-size: 10px; }.missing-days p { margin-top: 8px; color: var(--warning); }
.source-grid { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 11px; }.source-card { min-width: 0; }.source-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }.source-icon { display: grid; width: 34px; height: 34px; place-items: center; border: 1px solid rgba(97,215,223,.22); border-radius: 7px; background: rgba(97,215,223,.07); color: #85e0e5; font-family: "SFMono-Regular", Consolas, monospace; font-size: 10px; font-weight: 720; }.source-card h3 { margin-top: 17px; font-size: 14px; }.source-card p { min-height: 58px; margin-top: 7px; color: var(--text-secondary); font-size: 11px; line-height: 1.65; }.source-card>code { display: block; overflow: hidden; margin-top: 12px; color: var(--text-tertiary); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }.inline-error { margin-top: 9px; color: var(--danger); font-size: 11px; }.capability,.fund-archive { margin-top: 13px; }.cap { display: inline-flex; border-radius: 999px; padding: 3px 8px; font-size: 10px; }.cap.yes { background: var(--down-muted); color: #69cca4; }.cap.limited { background: rgba(228,168,58,.09); color: #edbd62; }.hash { color: var(--text-tertiary); }.unfinished-note { border-color: var(--border-strong); background: var(--bg-secondary); color: var(--text-tertiary); }
@media (max-width: 1400px) { .hero { grid-template-columns: 1fr; } }
@media (max-width: 1120px) { .coverage-form { grid-template-columns: 1fr 1fr; }.form-actions { grid-column: 1/-1; display: flex; align-items: center; }.source-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 720px) { .page-header { margin-bottom: 18px; }.local-chip { align-self: flex-start; }.hero { padding: 20px 16px; }.metric-grid { grid-template-columns: 1fr 1fr; }.metric { border-bottom: 1px solid rgba(255,255,255,.065); }.metric:nth-child(2) { border-right: 0; }.metric:nth-child(3),.metric:nth-child(4) { border-bottom: 0; }.tabs button { flex-direction: column; gap: 1px; }.panel-heading { align-items: stretch; flex-direction: column; }.inventory-toolbar { align-items: stretch; flex-direction: column; }.toolbar-meta { order: 2; }.coverage-form { grid-template-columns: minmax(0,1fr); }.date-fields { grid-template-columns: 1fr 1fr; }.form-actions { grid-column: auto; }.report-overview { flex-direction: column; }.calendar-state { min-width: 0; border-top: 1px solid var(--border); border-left: 0; padding-top: 13px; padding-left: 0; }.coverage-stats { grid-template-columns: repeat(2,minmax(0,1fr)); }.source-grid { grid-template-columns: minmax(0,1fr); }.source-card p { min-height: 0; }.data-table th,.data-table td { padding-inline: 9px; }.pagination { gap: 8px; }.pagination>span { white-space: nowrap; } }
@media (max-width: 390px) { .metric { padding: 15px 11px; }.metric strong { font-size: 20px; }.metric .date-boundary { font-size: 10px; }.tabs button { padding-inline: 5px; }.date-fields { grid-template-columns: minmax(0,1fr); }.coverage-item { padding: 15px 13px; }.coverage-stats { grid-template-columns: 1fr 1fr; }.pagination .btn-secondary { padding-inline: 7px; } }
</style>
