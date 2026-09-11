<template>
  <div class="page workflow-page">
    <header class="workflow-hero">
      <div>
        <span class="workflow-kicker">RESEARCH → OBSERVE → EXECUTE</span>
        <h1>日频研究闭环</h1>
        <p class="page-subtitle">把研究证据、前瞻观察和人工执行放进同一条可追踪的工作流。</p>
      </div>
      <div class="hero-actions">
        <span class="mode-badge" :class="isDemo ? 'demo' : 'state'">{{ isDemo ? '合成数据 · 工程演示' : '项目真实状态' }}</span>
        <button class="btn-secondary" type="button" :disabled="loading" @click="refresh">{{ loading ? '刷新中…' : '刷新状态' }}</button>
      </div>
    </header>

    <div v-if="errorMessage" class="workflow-alert error" role="alert">{{ errorMessage }}</div>
    <div v-if="notice" class="workflow-alert" role="status">{{ notice }}</div>

    <section class="workflow-launch card" aria-labelledby="launch-title">
      <div class="launch-copy">
        <span class="section-label">当前工作台</span>
        <h2 id="launch-title">从研究结果走到日终复盘</h2>
        <p>按研究、观察、计划、确认、成交和复盘顺序，完成一条可恢复的日频工作流。</p>
      </div>
      <div class="launch-actions">
        <button class="btn-primary" type="button" :disabled="busy" @click="runExperience">{{ busy ? '推进中…' : '一键体验全流程' }}</button>
        <button class="btn-secondary" type="button" :disabled="busy" @click="createRun">{{ activeRun ? '新建演示运行' : '开始工程演示' }}</button>
      </div>
    </section>

    <section class="stats-grid" aria-label="项目状态统计">
      <article v-for="item in headlineStats" :key="item.label" class="stat-card">
        <span>{{ item.label }}</span><strong>{{ item.value }}</strong><small>{{ item.note }}</small>
      </article>
    </section>

    <section v-if="activeRun" class="card run-panel" aria-labelledby="run-title">
      <div class="section-heading">
        <div><span class="section-label">RUN {{ activeRun.id.slice(0, 8) }}</span><h2 id="run-title">{{ stepLabel(activeRun.current_step) }}</h2><p>{{ nextStepText }}</p></div>
        <span class="revision-pill">进度已保存</span>
      </div>
      <div class="workflow-steps" aria-label="日频闭环步骤">
        <button v-for="step in steps" :key="step.key" type="button" :class="['workflow-step', stepState(step.key)]" :disabled="busy || !canAdvance(step.key)" @click="advance(step.key)">
          <span class="step-index">{{ step.index }}</span><span><b>{{ step.label }}</b><small>{{ step.caption }}</small></span>
        </button>
      </div>
      <div class="run-toolbar">
        <label v-if="activeRun.current_step === 'fill'" class="fill-choice">模拟成交<select v-model="fillMode"><option value="full">全部成交</option><option value="partial">部分成交</option><option value="unfilled">未成交</option></select></label>
        <button class="btn-accent" type="button" :disabled="busy || !activeRun.next_step" @click="advance(activeRun.next_step || 'research')">{{ activeRun.next_step ? `推进：${stepLabel(activeRun.next_step)}` : '流程已完成' }}</button>
      </div>
    </section>

    <section v-else class="card empty-run">
      <div class="empty-icon">◎</div><div><h2>还没有当前演示运行</h2><p>先查看项目真实状态，再启动一条可恢复的合成工程演示。</p></div><button class="btn-primary" type="button" :disabled="busy" @click="createRun">开始演示</button>
    </section>

    <div class="workflow-columns">
      <div class="workflow-main">
        <section class="card chart-card" aria-labelledby="equity-title">
          <div class="section-heading"><div><span class="section-label">PERFORMANCE</span><h2 id="equity-title">策略净值与基准</h2></div><div class="chart-actions"><span class="qualification-pill">工程演示结果</span><div class="chart-toggle"><button type="button" :class="{ active: chartMode === 'research' }" @click="chartMode = 'research'">研究回测</button><button type="button" :disabled="!executionCurve.length" :class="{ active: chartMode === 'execution' }" @click="chartMode = 'execution'">执行演练</button></div></div></div>
          <VChart v-if="chartOption" class="workflow-chart" :option="chartOption" autoresize />
          <div v-else class="chart-empty">完成研究步骤后显示曲线。</div>
          <div class="metric-strip"><div><span>累计收益</span><strong>{{ formatPercent(metric('total_return')) }}</strong></div><div><span>最大回撤</span><strong>{{ formatPercent(metric('max_drawdown')) }}</strong></div><div><span>超额收益</span><strong>{{ formatPercent(metric('excess_return')) }}</strong></div><div><span>观察进度</span><strong>{{ observationProgress }}</strong></div></div>
          <div v-if="researchCandidates.length" class="research-evidence"><div class="evidence-title"><span>研究候选</span><small>当前演练因子：{{ factorLabel(activeRun?.research?.factor_name) }}</small></div><div class="candidate-table"><div v-for="candidate in researchCandidates" :key="String(candidate.name)" class="candidate-row"><strong>{{ factorLabel(candidate.name) }}</strong><span>{{ candidate.name === activeRun?.research?.factor_name ? '当前演练因子' : '候选' }}</span><span>基线 {{ formatPercent(candidateBaseline(candidate)) }}</span><span>压力 {{ formatPercent(candidateStress(candidate)) }}</span></div></div></div>
        </section>

        <section class="card decision-card" aria-labelledby="decision-title">
          <div class="section-heading"><div><span class="section-label">TODAY'S DECISION</span><h2 id="decision-title">当前决策解释</h2></div><span class="decision-status">{{ decisionAction }}</span></div>
          <p class="decision-summary">{{ decisionReason }}</p>
          <div class="decision-facts"><div><span>信号日</span><strong>{{ decisionValue('signal_date', '—') }}</strong></div><div><span>动作</span><strong>{{ decisionAction }}</strong></div><div><span>目标敞口</span><strong>{{ decisionValue('target_exposure', '—') }}</strong></div></div>
        </section>

        <section class="card plan-card" aria-labelledby="plan-title">
          <div class="section-heading"><div><span class="section-label">EXECUTION PLAN</span><h2 id="plan-title">人工执行计划</h2></div><span class="plan-note">生成计划 · 开盘 / 收盘</span></div>
          <div v-if="planItems.length" class="plan-groups"><div v-for="group in planGroups" :key="group.key" class="plan-group"><div class="group-title"><b>{{ group.label }}</b><span>{{ group.items.length }} 项</span></div><div v-for="item in group.items" :key="item.id || item.code" class="plan-item"><span class="side-mark" :class="item.side === 'sell' ? 'sell' : 'buy'">{{ item.side === 'sell' ? '卖' : '买' }}</span><span class="plan-code">{{ item.code || '—' }}</span><span class="plan-qty">{{ item.quantity || item.planned_quantity || '—' }} 股</span><span class="plan-price">¥{{ item.price || item.reference_price || '—' }}</span></div></div></div>
          <p v-else class="inline-empty">完成 plan 步骤后显示计划项；人工计划始终需要用户在券商侧执行。</p>
        </section>
        <section class="card account-card" aria-labelledby="account-title"><div class="section-heading"><div><span class="section-label">ACCOUNT STATE</span><h2 id="account-title">成交与持仓</h2></div><span class="plan-note">服务端运行状态</span></div><div v-if="fillItems.length" class="fill-list"><div v-for="fill in fillItems" :key="fill.intent_id || `${fill.code}-${fill.trade_date}`" class="fill-row"><span class="side-mark" :class="fill.side === 'sell' ? 'sell' : 'buy'">{{ fill.side === 'sell' ? '卖' : '买' }}</span><strong>{{ fill.code }}</strong><span>{{ fill.filled_quantity || 0 }} / {{ fill.requested_quantity || 0 }} 股</span><span :class="fill.status === 'unfilled' ? 'muted' : 'positive'">{{ fillStatus(fill.status) }}</span></div></div><p v-else class="inline-empty">完成成交步骤后显示模拟成交。</p><div v-if="positions.length" class="position-list"><div v-for="position in positions" :key="String(position.code)" class="position-row"><strong>{{ position.code }}</strong><span>{{ position.quantity }} 股</span><span>成本 ¥{{ position.avg_cost }}</span></div></div><p v-else class="inline-empty position-empty">当前没有持仓。</p></section>
      </div>

      <aside class="workflow-side">
        <section class="card observation-card" aria-labelledby="observation-title"><div class="section-heading"><div><span class="section-label">PROSPECTIVE PILOT</span><h2 id="observation-title">30 日观察演练</h2></div><span class="status-dot" :class="observationProgress !== '—' ? 'is-live' : 'is-warning'"></span></div><div class="observation-ring"><strong>{{ observationProgress }}</strong><span>交易日</span></div><p>{{ observationText }}</p><div class="mini-facts"><span>当前状态<strong>{{ currentPilot }}</strong></span><span>对账<strong>{{ observationValue('reconciled_days', '—') }}</strong></span></div></section>
        <section class="card review-card" aria-labelledby="review-title"><div class="section-heading"><div><span class="section-label">END OF DAY</span><h2 id="review-title">现金、持仓与复盘</h2></div></div><div class="review-values"><div><span>现金</span><strong>{{ reviewValue('cash', '—') }}</strong></div><div><span>持仓市值</span><strong>{{ reviewValue('market_value', '—') }}</strong></div><div><span>当日收益</span><strong :class="numberValue(reviewValue('daily_return')) >= 0 ? 'positive' : 'negative'">{{ reviewPercent('daily_return') }}</strong></div></div><p class="review-reason">{{ reviewSummary }}</p><button class="btn-secondary wide-button" type="button" :disabled="busy || activeRun?.next_step !== 'next_day'" @click="advance('next_day')">推进次日</button></section>
        <section class="card blockers-card" aria-labelledby="blockers-title"><div class="section-heading"><div><span class="section-label">PROJECT STATE</span><h2 id="blockers-title">当前阻断与入口</h2></div></div><div v-if="blockers.length" class="blocker-list"><div v-for="block in blockers" :key="String(block.title || block.reason)" class="blocker"><span class="blocker-dot"></span><div><strong>{{ block.title || '阻断' }}</strong><p>{{ block.reason || block.status }}</p><router-link v-if="block.target" :to="block.target">去处理 ↗</router-link></div></div></div><p v-else class="inline-empty">当前没有可读阻断。</p><div class="quick-links"><router-link to="/factors">因子研究</router-link><router-link to="/backtest">回测证据</router-link><router-link to="/manual">人工执行</router-link></div></section>
      </aside>
    </div>

    <section class="card live-section" aria-labelledby="live-title"><div class="section-heading"><div><span class="section-label">REAL PROJECT DATA</span><h2 id="live-title">真实项目状态</h2><p>研究候选、release、pilot 和人工账户来自服务端；工程演示不会改变这些真实对象。</p></div><span class="mode-badge state">真实项目记录</span></div><div class="live-grid"><div><span>候选因子</span><strong>{{ liveCount('candidates') }}</strong></div><div><span>策略版本</span><strong>{{ liveCount('releases') }}</strong></div><div><span>观察 pilot</span><strong>{{ liveCount('pilots') }}</strong></div><div><span>人工账户</span><strong>{{ liveCount('accounts') }}</strong></div></div><div class="live-actions"><router-link class="btn-secondary" to="/factors">查看因子研究</router-link><router-link class="btn-secondary" to="/backtest">查看回测</router-link><router-link class="btn-secondary" to="/manual">打开人工执行</router-link></div></section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { useApi } from '@/composables/useApi'
import type { WorkflowOverview, WorkflowRun, WorkflowStep } from '@/types/workflow'
import { newIdempotencyKey } from '@/utils/manual'

use([CanvasRenderer, LineChart, GridComponent, LegendComponent, TooltipComponent])
const { api } = useApi()
const router = useRouter()
const overview = ref<WorkflowOverview>({})
const activeRun = ref<WorkflowRun | null>(null)
const loading = ref(false)
const busy = ref(false)
const notice = ref('')
const errorMessage = ref('')
const fillMode = ref<'full' | 'partial' | 'unfilled'>('full')
const chartMode = ref<'research' | 'execution'>('research')
const steps: Array<{ key: WorkflowStep; label: string; caption: string; index: number }> = [
  { key: 'research', label: '研究', caption: '证据与策略', index: 1 }, { key: 'observe', label: '观察', caption: '前瞻输入', index: 2 }, { key: 'plan', label: '计划', caption: '开盘 / 收盘', index: 3 }, { key: 'confirm', label: '确认', caption: '人工确认', index: 4 }, { key: 'fill', label: '成交', caption: '模拟回填', index: 5 }, { key: 'review', label: '复盘', caption: '日终状态', index: 6 }, { key: 'next_day', label: '次日', caption: '继续循环', index: 7 },
]
const isDemo = computed(() => activeRun.value?.mode === 'engineering_demo' || !activeRun.value)
const blockers = computed(() => Array.isArray(overview.value.blockers) ? overview.value.blockers : [])
const headlineStats = computed(() => {
  const stats = overview.value.stats || {}
  return [
    { label: '研究候选', value: liveCount('candidates'), note: '来自真实数据库' }, { label: '策略版本', value: liveCount('releases'), note: 'release 状态可追踪' }, { label: '观察进度', value: observationProgress.value, note: '工程演示或真实 pilot' }, { label: '当前阻断', value: blockers.value.length, note: blockers.value.length ? '需要先处理' : '暂无可读阻断' },
  ].map((item) => ({ ...item, value: stats[item.label] ?? item.value }))
})
const chartOption = computed(() => {
  const rows: Array<Record<string, any>> = (chartMode.value === 'execution' ? executionCurve.value : (activeRun.value?.metrics?.equity_curve || [])) as Array<Record<string, any>>
  if (!rows.length) return null
  const labels = rows.map((row) => String(row.date || row.day || ''))
  const seriesKeys = [{ key: 'strategy', name: '策略', color: '#ef5b64' }, { key: 'benchmark', name: '沪深300', color: '#36b37e' }, { key: 'benchmark_2', name: '上证', color: '#4d8dff' }]
  return { animation: false, backgroundColor: 'transparent', tooltip: { trigger: 'axis' }, legend: { top: 0, textStyle: { color: '#9aa7b6' } }, grid: { top: 42, left: 42, right: 18, bottom: 28 }, xAxis: { type: 'category', data: labels, boundaryGap: false, axisLabel: { color: '#667386', fontSize: 10 } }, yAxis: { type: 'value', scale: true, axisLabel: { color: '#667386', fontSize: 10, formatter: (v: number) => v.toFixed(2) }, splitLine: { lineStyle: { color: '#202b39' } } }, series: seriesKeys.map((item) => ({ name: item.name, type: 'line', showSymbol: false, smooth: true, data: rows.map((row) => row[item.key] == null ? null : Number(row[item.key])), lineStyle: { color: item.color, width: 2 }, itemStyle: { color: item.color } })) }
})
const executionCurve = computed(() => {
  const rows = activeRun.value?.state?.nav
  return Array.isArray(rows) ? rows.map((row: any) => ({ date: row.date, strategy: row.equity ?? row.total_equity ?? row.nav, benchmark: null, benchmark_2: null })) : []
})
const planItems = computed(() => {
  const value = activeRun.value?.plan?.items || activeRun.value?.plan?.execution_items || []
  return Array.isArray(value) ? value as Array<Record<string, any>> : []
})
const planGroups = computed(() => [{ key: 'open', label: '开盘执行', items: planItems.value.filter((item) => String(item.session || item.execution_session || 'open') === 'open') }, { key: 'close', label: '收盘执行', items: planItems.value.filter((item) => String(item.session || item.execution_session) === 'close') }].filter((group) => group.items.length))
const observationProgress = computed(() => { const value = activeRun.value?.observation?.completed_days ?? activeRun.value?.observation?.valid_days ?? (Array.isArray(activeRun.value?.observation?.days) ? activeRun.value.observation.days.length : null); return value == null ? '—' : `${value} / 30` })
const currentPilot = computed(() => String(activeRun.value?.observation?.pilot_id || '演练状态'))
const observationText = computed(() => activeRun.value?.observation?.summary ? String(activeRun.value.observation.summary) : '观察输入由服务端按冻结日历推进；工程演示使用合成数据，不代表真实 30 日资格。')
const nextStepText = computed(() => activeRun.value?.next_step ? `下一步：${stepLabel(activeRun.value.next_step)}` : '当前运行已到达可读终点。')
const decisionActionLabels: Record<string, string> = { hold: '持有', rebalance: '调仓', reduce: '减仓', flat: '空仓', blocked: '阻断', reconcile: '待对账' }
const reasonLabels: Record<string, string> = { synthetic_engineering: '工程演示输入', data_health_blocked: '数据健康阻断' }
const decisionAction = computed(() => decisionActionLabels[String(activeRun.value?.decision?.action || '')] || String(activeRun.value?.decision?.action || '待研究'))
const decisionReason = computed(() => { const reasons = activeRun.value?.decision?.reasons; return Array.isArray(reasons) && reasons.length ? reasons.map((reason) => reasonLabels[String(reason)] || String(reason)).join(' · ') : '完成研究与观察步骤后显示当前日决策和阻断理由。' })
const fillItems = computed(() => Array.isArray(activeRun.value?.fill?.items) ? activeRun.value.fill.items as Array<Record<string, any>> : [])
const positions = computed(() => Array.isArray(activeRun.value?.state?.positions) ? activeRun.value.state.positions as Array<Record<string, any>> : [])
const researchCandidates = computed(() => Array.isArray(activeRun.value?.research?.candidates) ? activeRun.value.research.candidates as Array<Record<string, any>> : [])

function unwrapWorkflow<T>(payload: unknown, expectedMode: 'project_state' | 'engineering_demo'): T {
  if (!payload || typeof payload !== 'object') throw new Error('日频闭环返回格式无效。')
  const envelope = payload as Record<string, unknown>
  if (envelope.mode !== expectedMode || envelope.live_authorized !== false || envelope.broker_connected !== false) throw new Error('日频闭环安全边界响应不完整。')
  return envelope.data as T
}
function normalizeOverview(value: any): WorkflowOverview {
  const counts = value?.counts || {}
  const moduleLabels: Record<string, string> = { candidates: '候选因子', releases: '策略版本', pilots: '观察 pilot', accounts: '人工账户', plans: '执行计划', reviews: '日终复盘' }
  return { ...value, stats: counts, candidates: value?.latest?.candidates || [], releases: value?.latest?.releases || [], pilots: value?.latest?.pilots || [], accounts: value?.latest?.accounts || [], blockers: (value?.blocks || []).map((item: any) => ({ title: moduleLabels[item.module] || '项目阻断', reason: item.reason || item.status, status: item.status, target: item.module === 'candidates' ? '/factors' : item.module === 'releases' ? '/backtest' : '/manual' })), links: value?.entrypoints || [] }
}
function normalizeRun(value: any): WorkflowRun {
  const raw = value?.run || value || {}
  const state = raw.state || raw
  const current = state.next_action || (state.stage === 'completed' ? 'next_day' : state.stage)
  const research = state.research || {}
  const metrics = { ...(research.metrics || {}), baseline: research.baseline || {}, stress: research.stress || {}, equity_curve: (research.curves || []).map((row: any) => ({ ...row, benchmark: row.hs300, benchmark_2: row.shanghai })) }
  return { ...raw, current_step: current, next_step: state.next_action || null, state, research, metrics, observation: state.observation || {}, decision: state.decision || {}, plan: { items: Array.isArray(state.plans) ? state.plans.flatMap((plan: any) => (plan.items || []).map((item: any) => ({ ...item, session: plan.phase || item.phase }))) : [] }, fill: { items: state.fills || [] }, review: (state.reviews || []).at?.(-1) || {}, status: state.stage }
}
function stepLabel(step?: string | null) { return steps.find((item) => item.key === step)?.label || step || '—' }
function stepState(step: WorkflowStep) { if (!activeRun.value) return 'pending'; const order = steps.findIndex((item) => item.key === step); const current = steps.findIndex((item) => item.key === activeRun.value?.current_step); return order < current ? 'done' : order === current ? 'current' : 'pending' }
function canAdvance(step: WorkflowStep) { return activeRun.value?.next_step === step || activeRun.value?.current_step === step }
function numberValue(value: unknown) { const number = Number(value); return Number.isFinite(number) ? number : 0 }
function formatPercent(value: unknown) { const number = Number(value); return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : '—' }
function metric(key: string) { return activeRun.value?.metrics?.[key] ?? (activeRun.value?.metrics?.baseline as Record<string, unknown> | undefined)?.[key] ?? activeRun.value?.state?.[key] }
function decisionValue(key: string, fallback = '—') { return String(activeRun.value?.decision?.[key] ?? fallback) }
function observationValue(key: string, fallback = '—') { return String(activeRun.value?.observation?.[key] ?? fallback) }
function reviewValue(key: string, fallback = '—') { return String(activeRun.value?.review?.[key] ?? fallback) }
function reviewPercent(key: string) { const value = activeRun.value?.review?.[key]; return value == null ? '—' : formatPercent(value) }
const reviewSummary = computed(() => activeRun.value?.review?.status === 'matched' ? `已对账：现金 ${reviewValue('cash', '—')}，持仓市值 ${reviewValue('market_value', '—')}。` : reviewValue('summary', '日终复盘将在 review 步骤由服务端形成。'))
function liveCount(key: string) { const value = (overview.value.counts as Record<string, unknown> | undefined)?.[key] ?? overview.value.stats?.[key] ?? (overview.value[key] as unknown); return Array.isArray(value) ? value.length : Number(value ?? 0) || 0 }
function fillStatus(value: unknown) { return ({ filled: '全部成交', partially_filled: '部分成交', unfilled: '未成交' } as Record<string, string>)[String(value)] || String(value || '待处理') }
function factorLabel(value: unknown) { return ({ close_momentum: '收盘动量', range_pressure: '区间压力', volume_change: '成交量变化' } as Record<string, string>)[String(value)] || String(value || '—') }
function candidateScenario(candidate: Record<string, any>) { const scenarios = activeRun.value?.research?.scenarios as Record<string, any> | undefined; return scenarios?.[String(candidate.name)] || {} }
function candidateBaseline(candidate: Record<string, any>) { const scenario = candidateScenario(candidate); return candidate?.metrics?.baseline?.total_return ?? candidate?.baseline?.net_return ?? scenario?.metrics?.baseline?.total_return ?? scenario?.baseline?.net_return ?? (activeRun.value?.research?.baseline as Record<string, unknown> | undefined)?.net_return }
function candidateStress(candidate: Record<string, any>) { const scenario = candidateScenario(candidate); return candidate?.metrics?.stress?.total_return ?? candidate?.stress?.net_return ?? scenario?.metrics?.stress?.total_return ?? scenario?.stress?.net_return ?? (activeRun.value?.research?.stress as Record<string, unknown> | undefined)?.net_return }
function clearMessage() { notice.value = ''; errorMessage.value = '' }
function errorText(error: any) { return error?.response?.data?.detail?.message || error?.response?.data?.detail?.code || error?.response?.data?.detail || error?.message || '请求失败，请重试。' }
async function loadRun(id: string) { const response = await api.get(`/daily-workflow/runs/${id}`, { timeout: 120000 }); activeRun.value = normalizeRun(unwrapWorkflow<any>(response.data, 'engineering_demo')); localStorage.setItem('kk.quant.daily-workflow.run', id) }
async function refresh() { loading.value = true; clearMessage(); try { const response = await api.get('/daily-workflow/overview'); overview.value = normalizeOverview(unwrapWorkflow<any>(response.data, 'project_state') || {}); const id = activeRun.value?.id || localStorage.getItem('kk.quant.daily-workflow.run'); if (id) await loadRun(id) } catch (error: any) { errorMessage.value = errorText(error) } finally { loading.value = false } }
async function createRun() { busy.value = true; clearMessage(); try { const response = await api.post('/daily-workflow/runs', { seed: 7 }, { timeout: 120000 }); activeRun.value = normalizeRun(unwrapWorkflow<any>(response.data, 'engineering_demo')); if (activeRun.value?.id) localStorage.setItem('kk.quant.daily-workflow.run', activeRun.value.id); notice.value = '工程演示已创建，下一步由服务端状态推进。' } catch (error: any) { errorMessage.value = errorText(error) } finally { busy.value = false } }
async function advance(action: WorkflowStep) { if (!activeRun.value) return; busy.value = true; clearMessage(); try { const response = await api.post(`/daily-workflow/runs/${activeRun.value.id}/advance`, { action, fill_mode: action === 'fill' ? fillMode.value : undefined, expected_revision: activeRun.value.revision }, { timeout: 120000, headers: { 'Idempotency-Key': newIdempotencyKey(`workflow-${action}`) } }); activeRun.value = normalizeRun(unwrapWorkflow<any>(response.data, 'engineering_demo')); notice.value = `${stepLabel(action)}已由服务端完成。` } catch (error: any) { errorMessage.value = errorText(error) } finally { busy.value = false } }
async function runExperience() { if (!activeRun.value) await createRun(); if (!activeRun.value) return; busy.value = true; clearMessage(); try { const order: WorkflowStep[] = ['research', 'observe', 'plan', 'confirm', 'fill', 'review']; while (activeRun.value?.next_step && order.includes(activeRun.value.next_step)) { const run: WorkflowRun = activeRun.value; const action = run.next_step as WorkflowStep; const response: { data: unknown } = await api.post<unknown>(`/daily-workflow/runs/${run.id}/advance`, { action, fill_mode: action === 'fill' ? fillMode.value : undefined, expected_revision: run.revision }, { timeout: 120000, headers: { 'Idempotency-Key': newIdempotencyKey(`workflow-${action}`) } }); activeRun.value = normalizeRun(unwrapWorkflow<any>(response.data, 'engineering_demo')) } notice.value = activeRun.value?.current_step === 'next_day' || activeRun.value?.status === 'reviewed' ? '已完成一轮工程演示，复盘结果已保存。' : '流程已推进，当前进度已保存。' } catch (error: any) { errorMessage.value = errorText(error) } finally { busy.value = false } }
onMounted(() => { void refresh() })
</script>

<style scoped>
.workflow-page { max-width: 1500px; }
.workflow-hero, .section-heading, .hero-actions, .launch-actions, .run-toolbar, .live-actions { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.workflow-kicker, .section-label { color: var(--accent); font-size: 10px; font-weight: 750; letter-spacing: .13em; }
.workflow-hero { align-items: flex-start; margin-bottom: 24px; }
.workflow-hero h1 { margin-top: 7px; }
.hero-actions { align-items: flex-end; flex-direction: column; }
.mode-badge, .revision-pill, .plan-note { display: inline-flex; align-items: center; border: 1px solid var(--border-strong); border-radius: 999px; color: var(--text-secondary); font-size: 11px; padding: 5px 10px; white-space: nowrap; }
.mode-badge.demo { border-color: rgba(77,141,255,.45); background: var(--accent-muted); color: #8db5ff; }.mode-badge.state { border-color: rgba(228,168,58,.42); color: #edbd62; }
.workflow-launch { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 16px; background: linear-gradient(120deg, rgba(77,141,255,.13), rgba(22,30,41,.94) 48%); }
.launch-copy h2 { margin-top: 5px; font-size: 18px; }.launch-copy p { margin-top: 5px; color: var(--text-secondary); font-size: 12px; }
.workflow-alert { margin-bottom: 14px; border: 1px solid rgba(77,141,255,.35); border-radius: var(--radius-sm); background: var(--accent-muted); padding: 10px 13px; color: #a9c6ff; font-size: 13px; }.workflow-alert.error { border-color: rgba(239,91,100,.4); background: var(--up-muted); color: #ff9ba1; }
.stats-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }.stat-card { display: grid; gap: 4px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-elevated); padding: 15px 16px; }.stat-card span, .stat-card small, .metric-strip span, .review-values span, .decision-facts span, .live-grid span { color: var(--text-secondary); font-size: 11px; }.stat-card strong { font-size: 24px; letter-spacing: -.04em; }.stat-card small { color: var(--text-tertiary); }
.run-panel { margin-bottom: 18px; }.section-heading { align-items: flex-start; }.section-heading h2 { margin-top: 4px; font-size: 17px; }.section-heading p { margin-top: 4px; color: var(--text-secondary); font-size: 12px; }.revision-pill { font-family: monospace; }
.workflow-steps { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 5px; margin-top: 20px; }.workflow-step { display: flex; min-width: 0; align-items: center; gap: 7px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); color: var(--text-tertiary); padding: 10px 8px; text-align: left; }.workflow-step:disabled { cursor: default; opacity: 1; }.workflow-step.current { border-color: rgba(77,141,255,.6); background: var(--accent-muted); color: var(--text-primary); }.workflow-step.done { border-color: rgba(54,179,126,.35); color: var(--success); }.step-index { display: grid; width: 22px; height: 22px; flex: 0 0 auto; place-items: center; border-radius: 50%; background: var(--bg-muted); font-size: 10px; }.workflow-step b, .workflow-step small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.workflow-step b { font-size: 12px; }.workflow-step small { margin-top: 2px; color: inherit; font-size: 10px; opacity: .75; }.run-toolbar { justify-content: flex-end; margin-top: 16px; }.fill-choice { display: flex; align-items: center; gap: 7px; color: var(--text-secondary); font-size: 12px; }.fill-choice select { min-height: 34px; }
.empty-run { display: flex; align-items: center; gap: 16px; margin-bottom: 18px; }.empty-run p { margin-top: 3px; color: var(--text-secondary); font-size: 12px; }.empty-run button { margin-left: auto; }.empty-icon { display: grid; width: 42px; height: 42px; place-items: center; border-radius: 50%; background: var(--accent-muted); color: var(--accent); font-size: 26px; }
.workflow-columns { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(280px, .75fr); align-items: start; gap: 18px; }.workflow-main, .workflow-side { display: grid; gap: 18px; }.chart-caption, .plan-note { color: var(--text-tertiary); font-size: 11px; }.workflow-chart { width: 100%; height: 330px; margin-top: 10px; }.chart-empty { display: grid; min-height: 330px; place-items: center; color: var(--text-tertiary); font-size: 13px; }.metric-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 5px; }.metric-strip div, .review-values div, .decision-facts div, .live-grid div { display: grid; gap: 3px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); padding: 9px 10px; }.metric-strip strong, .review-values strong, .decision-facts strong, .live-grid strong { font-size: 14px; }
.qualification-pill { border: 1px solid rgba(228,168,58,.35); border-radius: 999px; color: #edbd62; padding: 5px 9px; font-size: 10px; font-family: monospace; }
.chart-actions { display: flex; align-items: center; gap: 8px; }.chart-toggle { display: inline-flex; border: 1px solid var(--border-strong); border-radius: 999px; overflow: hidden; }.chart-toggle button { background: transparent; color: var(--text-tertiary); padding: 5px 9px; font-size: 10px; }.chart-toggle button.active { background: var(--accent-muted); color: var(--accent-hover); }.chart-toggle button:disabled { cursor: not-allowed; opacity: .45; }
.fill-list, .position-list { display: grid; gap: 7px; margin-top: 14px; }.fill-row, .position-row { display: grid; grid-template-columns: 24px 1fr auto auto; align-items: center; gap: 8px; border-top: 1px solid var(--border); padding-top: 9px; font-size: 12px; }.position-list { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }.position-row { grid-template-columns: 1fr auto auto; }.position-empty { margin-top: 10px; }
.research-evidence { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 13px; }.evidence-title { display: flex; justify-content: space-between; gap: 10px; color: var(--text-primary); font-size: 12px; }.evidence-title small { color: var(--text-secondary); font-size: 10px; }.candidate-table { display: grid; gap: 5px; margin-top: 8px; }.candidate-row { display: grid; grid-template-columns: 1.25fr .8fr 1fr 1fr; gap: 8px; align-items: center; border-top: 1px solid var(--border); padding-top: 7px; color: var(--text-secondary); font-size: 11px; }.candidate-row strong { color: var(--text-primary); }
.decision-summary { margin: 16px 0; color: var(--text-primary); font-size: 14px; line-height: 1.7; }.decision-status { color: var(--success); font-size: 12px; }.decision-facts, .review-values, .live-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }.plan-groups { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }.plan-group { border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }.group-title { display: flex; justify-content: space-between; background: var(--bg-secondary); padding: 8px 10px; font-size: 12px; }.group-title span { color: var(--text-tertiary); }.plan-item { display: grid; grid-template-columns: 24px 1fr auto auto; align-items: center; gap: 7px; border-top: 1px solid var(--border); padding: 9px 10px; font-size: 12px; }.side-mark { display: grid; width: 20px; height: 20px; place-items: center; border-radius: 4px; font-size: 10px; }.side-mark.buy { background: var(--down-muted); color: var(--success); }.side-mark.sell { background: var(--up-muted); color: var(--danger); }.plan-code, .plan-qty, .plan-price { font-family: monospace; }.plan-qty, .plan-price { color: var(--text-secondary); }.inline-empty, .review-reason { color: var(--text-secondary); font-size: 12px; line-height: 1.7; }.observation-card, .review-card, .blockers-card { min-width: 0; }.observation-ring { display: grid; width: 116px; height: 116px; place-items: center; align-content: center; margin: 22px auto 14px; border: 8px solid rgba(77,141,255,.18); border-top-color: var(--accent); border-radius: 50%; }.observation-ring strong { font-size: 20px; }.observation-ring span { color: var(--text-tertiary); font-size: 10px; }.observation-card p { color: var(--text-secondary); font-size: 12px; line-height: 1.7; }.mini-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }.mini-facts span { display: grid; gap: 3px; color: var(--text-secondary); font-size: 11px; }.mini-facts strong { overflow: hidden; color: var(--text-primary); text-overflow: ellipsis; }.review-values { grid-template-columns: repeat(3, 1fr); margin: 16px 0; }.review-reason { min-height: 56px; }.wide-button { width: 100%; margin-top: 14px; }.blocker-list { display: grid; gap: 10px; margin-top: 14px; }.blocker { display: flex; gap: 8px; }.blocker-dot { width: 7px; height: 7px; flex: 0 0 auto; margin-top: 6px; border-radius: 50%; background: var(--warning); }.blocker strong { font-size: 12px; }.blocker p { margin: 2px 0; color: var(--text-secondary); font-size: 11px; }.blocker a { font-size: 11px; }.quick-links { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px; padding-top: 13px; border-top: 1px solid var(--border); }.quick-links a { border: 1px solid var(--border-strong); border-radius: 999px; padding: 4px 8px; font-size: 11px; }.live-section { margin-top: 18px; }.live-section .section-heading p { max-width: 620px; }.live-grid { grid-template-columns: repeat(4, 1fr); margin-top: 16px; }.live-actions { justify-content: flex-start; margin-top: 15px; }
@media (max-width: 980px) { .workflow-columns { grid-template-columns: 1fr; }.workflow-side { grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; }.workflow-side .card { min-height: 100%; }.blockers-card { grid-column: 1 / -1; } }
.blocker p { overflow-wrap: anywhere; }
@media (max-width: 720px) { .workflow-hero, .workflow-launch, .section-heading { align-items: flex-start; flex-direction: column; }.hero-actions { width: 100%; align-items: flex-start; flex-direction: row; justify-content: space-between; }.stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.workflow-steps { grid-template-columns: repeat(4, minmax(0, 1fr)); }.workflow-step { align-items: flex-start; flex-direction: column; }.workflow-step small { display: none; }.workflow-side { grid-template-columns: 1fr; }.plan-groups, .metric-strip, .live-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.run-toolbar { align-items: stretch; flex-direction: column; }.run-toolbar .btn-accent { width: 100%; }.launch-actions { width: 100%; align-items: stretch; flex-direction: column; }.launch-actions button { width: 100%; }.empty-run { align-items: flex-start; flex-wrap: wrap; }.empty-run button { width: 100%; margin-left: 0; }.decision-facts { grid-template-columns: 1fr; } }
@media (max-width: 420px) { .workflow-page .card { padding: 14px; }.workflow-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); }.plan-item { grid-template-columns: 24px 1fr; }.plan-qty, .plan-price { grid-column: 2; }.live-actions { align-items: stretch; flex-direction: column; }.live-actions a { text-align: center; }.metric-strip { gap: 6px; }.stat-card strong { font-size: 20px; } }
</style>
