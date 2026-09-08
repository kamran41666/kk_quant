<template>
  <section ref="container" class="card research-comparison" aria-labelledby="research-comparison-title">
    <div class="comparison-heading">
      <div><span class="eyebrow">历史研究 · 市场对照</span><h2 id="research-comparison-title">策略净值与市场基准</h2></div>
      <div v-if="hasData && !loading" class="export-actions">
        <button type="button" :disabled="!chartReady" @click="prepareDownload">生成 PNG</button>
        <a v-if="downloadUrl" :href="downloadUrl" :download="`research-${runId}.png`">下载 PNG</a>
      </div>
    </div>
    <div v-if="loading" class="chart-state" role="status" aria-live="polite">正在读取真实回测与基准行情…</div>
    <div v-else-if="error" class="chart-state error-state" role="alert"><p>{{ error }}</p><button type="button" @click="loadComparison">重试加载</button></div>
    <div v-else-if="!hasData" class="chart-state">该实验暂未提供可绘制的真实净值数据。</div>
    <template v-else-if="comparison">
      <p class="validation-note" :class="{ unresolved: !comparison.validated }">
        {{ comparison.validated ? '已完成所列审计检查；历史回测不代表策略当前有效或未来收益。' : '研究假设 / 待解决问题：当前结果尚未通过完整验证，不能据此认定策略已盈利或当前有效。' }}
      </p>
      <VChart ref="chart" class="comparison-chart" :option="option" autoresize @finished="chartReady = true" />
      <div class="interval-grid">
        <article class="interval-card gain"><span>最大上涨 · 先低后高</span><strong>{{ formatChange(comparison.max_gain?.change) }}</strong><p>{{ intervalDates(comparison.max_gain) }}</p></article>
        <article class="interval-card drawdown"><span>最大回撤 · 先峰后谷</span><strong>{{ formatChange(comparison.max_drawdown?.change) }}</strong><p>{{ intervalDates(comparison.max_drawdown) }}</p></article>
      </div>
      <p class="chart-note">{{ comparison.note }}</p>
      <p class="chart-note">拖动下方时间滑块或在图内缩放查看区间；缺失行情保留断线。区间标注始终对应完整回测。</p>
      <details v-if="comparison.limitations.length" class="limitations"><summary>研究假设与待核查事项（{{ comparison.limitations.length }}）</summary><ul><li v-for="(item, index) in comparison.limitations" :key="index">{{ readableLimitation(item) }}</li></ul></details>
      <p v-if="exportError" class="export-error" role="alert">{{ exportError }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { AriaComponent, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkPointComponent, TooltipComponent } from 'echarts/components'
import { useApi } from '@/composables/useApi'
import { apiErrorMessage } from '@/utils/market'

use([CanvasRenderer, LineChart, AriaComponent, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkPointComponent, TooltipComponent])

interface Interval { start_date: string; end_date: string; change: number }
interface Comparison {
  labels: string[]
  series: Array<{ name: string; values: Array<number | null>; color: string }>
  max_gain: Interval | null
  max_drawdown: Interval | null
  note: string
  validated: boolean
  limitations: string[]
}
const props = defineProps<{ runId: string; runLabel?: string }>()
const { api } = useApi()
const comparison = ref<Comparison | null>(null)
const loading = ref(false)
const error = ref('')
const exportError = ref('')
const chart = ref<InstanceType<typeof VChart> | null>(null)
const chartReady = ref(false)
const container = ref<HTMLElement | null>(null)
const compact = ref(false)
const downloadUrl = ref('')
let requestVersion = 0
let controller: AbortController | undefined
let observer: ResizeObserver | undefined
const hasData = computed(() => !!comparison.value?.labels.length && comparison.value.series.some(series => series.values.some(value => value !== null)))

function formatChange(value: number | undefined): string {
  return value === undefined ? '无有效区间' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}
function intervalDates(interval: Interval | null): string {
  return interval ? `${interval.start_date} → ${interval.end_date}` : '没有满足时间顺序的区间'
}

function readableLimitation(value: string): string {
  const affected = /^dataset has unresolved coverage\/action issues for (\d+) securities/.exec(value)
  if (affected) return `${affected[1]} 只证券仍有待核对的行情或公司行动问题，详见数据质量报告。`
  return ({
    research_only_not_paper_authorization: '仅用于研究，未授权自动观察或真实交易。',
    initial_historical_pool_with_dynamic_daily_eligibility: '采用历史起点股票样本，并按每天的交易资格筛选；不包含后续新股。',
    opening_liquidity_uses_previous_session_volume: '开盘可成交数量受上一交易日成交量限制。',
    transfer_fee_flat_conservative_assumption: '过户费使用统一研究假设，未逐年还原全部真实费率。',
    dividend_tax_scenario_not_investor_specific_tax_collection: '股息税为情景假设，未模拟个人逐批追缴。',
    fractional_bonus_shares_are_floored: '红股不足一股的部分按向下取整处理。',
    bonus_fraction_floor_assumption: '本次持仓涉及红股取整，可能与实际登记分配存在细小差异。',
    derived_main_board_limits_not_exchange_order_book_evidence: '涨跌停按主板规则推导，未使用真实盘口验证排队成交。',
    unexplained_preclose_adjustment: '部分除权参考价变化尚未完全解释，可能涉及配股或差异化分配。',
    bonus_holder_allocation_unverified: '尚未确认部分转增股份是否归属普通股东，未将其计入权益。',
    unverified_bonus_shares_excluded_from_equity: '未确认的红股权益未计入净值。',
    delisting_zero_recovery_stress: '退市持仓采用零回收保守假设，真实清算回收值尚未核实。',
    active_daily_bar_missing: '存续期存在行情缺口。',
    held_price_missing: '部分持仓缺少有效估值价格。',
    corporate_action_record_date_unresolved: '部分公司行动的登记权益尚未确认。',
    dividend_pay_date_unresolved: '部分派息日期缺少充分证据。',
    bonus_listing_date_unresolved: '部分红股上市日期缺少充分证据。',
    unconfirmed_price_limit_regime: '部分交易日的涨跌停制度尚未确认。',
  } as Record<string, string>)[value] || `其他诊断：${value}`
}

function checkPayload(data: Comparison): Comparison {
  if (!data || !Array.isArray(data.labels) || !Array.isArray(data.series)
    || data.labels.some(date => typeof date !== 'string')
    || data.series.some(series => typeof series.name !== 'string' || !Array.isArray(series.values) || series.values.length !== data.labels.length)) {
    throw new Error('净值数据结构不完整，请重新生成研究结果')
  }
  const checkInterval = (interval: Interval | null) => {
    if (interval == null) return null
    const start = data.labels.indexOf(interval.start_date)
    const end = data.labels.indexOf(interval.end_date)
    if (start < 0 || end <= start || !Number.isFinite(interval.change)) throw new Error('研究区间标注与净值日期不一致')
    return interval
  }
  return {
    ...data,
    series: data.series.map(series => ({ ...series, values: series.values.map(value => typeof value === 'number' && Number.isFinite(value) ? value : null) })),
    max_gain: checkInterval(data.max_gain), max_drawdown: checkInterval(data.max_drawdown),
    validated: data.validated === true,
    note: typeof data.note === 'string' ? data.note : '基准为价格指数，不含分红；起点按真实收盘价归一。',
    limitations: Array.isArray(data.limitations) ? data.limitations.map(String) : [],
  }
}

async function loadComparison() {
  const version = ++requestVersion
  controller?.abort()
  controller = new AbortController()
  comparison.value = null
  downloadUrl.value = ''
  exportError.value = ''
  error.value = ''
  chartReady.value = false
  loading.value = true
  try {
    const { data } = await api.get<Comparison>(`/backtest/runs/${encodeURIComponent(props.runId)}/comparison`, { signal: controller.signal })
    if (version === requestVersion) comparison.value = checkPayload(data)
  } catch (reason) {
    if (version === requestVersion) error.value = apiErrorMessage(reason, '净值对照加载失败，请重试')
  } finally {
    if (version === requestVersion) loading.value = false
  }
}
watch(() => props.runId, loadComparison, { immediate: true })
onMounted(() => {
  if (container.value) {
    compact.value = container.value.clientWidth < 640
    observer = new ResizeObserver(entries => { compact.value = (entries[0]?.contentRect.width ?? 800) < 640 })
    observer.observe(container.value)
  }
})
onUnmounted(() => { ++requestVersion; controller?.abort(); observer?.disconnect() })

async function prepareDownload() {
  exportError.value = ''
  try {
    const exported = comparison.value
    const runId = props.runId
    const label = props.runLabel || '策略净值与市场基准'
    const url = chart.value?.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#0d1118' })
    if (!url || !exported) throw new Error('图表尚未准备完成')
    const picture = new Image()
    await new Promise<void>((resolve, reject) => { picture.onload = () => resolve(); picture.onerror = reject; picture.src = url })
    if (props.runId !== runId) return
    const canvas = document.createElement('canvas')
    const fontSize = Math.max(12, Math.min(26, Math.floor(picture.width / 48)))
    const header = fontSize * 4
    canvas.width = picture.width
    canvas.height = picture.height + header + fontSize * 8
    const context = canvas.getContext('2d')
    if (!context) throw new Error('图片画布不可用')
    context.fillStyle = '#0d1118'
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.font = `${fontSize}px sans-serif`
    context.fillStyle = '#f2f5f8'
    context.fillText(label, 24, fontSize * 1.7, canvas.width - 48)
    context.fillStyle = '#9aa7b6'
    context.fillText(`${exported.labels[0]} — ${exported.labels[exported.labels.length - 1]}`, 24, fontSize * 3.1)
    context.drawImage(picture, 0, header)
    let line = picture.height + header + fontSize * 1.6
    for (const [title, interval, color] of [
      ['最大上涨', exported.max_gain, '#89dca6'], ['最大回撤', exported.max_drawdown, '#c1a5ec'],
    ] as const) {
      context.fillStyle = color
      context.fillText(`${title} ${formatChange(interval?.change)}  ${intervalDates(interval)}`, 24, line, canvas.width - 48)
      line += fontSize * 1.6
    }
    context.fillStyle = '#9aa7b6'
    context.fillText('上证 / 沪深300为价格指数，不含分红；区间标注基于完整回测。', 24, line, canvas.width - 48)
    line += fontSize * 1.6
    context.fillText(exported.validated ? '研究结果不代表未来收益。' : '存在尚未解决的市场事件或模型假设；不作为策略有效性认证。', 24, line, canvas.width - 48)
    downloadUrl.value = canvas.toDataURL('image/png')
  } catch {
    exportError.value = 'PNG 生成失败，请等待图表加载完成后重试。'
  }
}

const option = computed(() => {
  const data = comparison.value
  if (!data) return {}
  const strategy = data.series[0]
  const intervals = [
    { interval: data.max_gain, title: '最大上涨', fill: 'rgba(134,215,158,.16)', color: '#89dca6', position: 'top' },
    { interval: data.max_drawdown, title: '最大回撤', fill: 'rgba(183,153,232,.18)', color: '#c1a5ec', position: 'bottom' },
  ]
  const areas = intervals.filter(item => item.interval).map(item => [
    { name: item.title, xAxis: item.interval!.start_date, itemStyle: { color: item.fill } },
    { xAxis: item.interval!.end_date },
  ])
  const points = intervals.flatMap(item => item.interval ? [item.interval.start_date, item.interval.end_date].flatMap((date, index) => {
    const value = strategy?.values[data.labels.indexOf(date)]
    return value == null ? [] : [{
      name: `${item.title}${index === 0 ? '起点' : '终点'}`,
      coord: [date, value], value,
      itemStyle: { color: item.color, borderColor: '#0d1118', borderWidth: 1 },
      label: { show: !compact.value, position: item.position, color: item.color, fontSize: 10, distance: 10,
        formatter: `${date}${index === 1 ? `\n${item.title} ${formatChange(item.interval!.change)}` : ''}` },
    }]
  }) : [])
  return {
    animation: false,
    textStyle: { fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif' },
    aria: { enabled: true, description: '策略净值与上证指数、沪深300历史对照。浅绿标注最大上涨，浅紫标注最大回撤。完整区间日期及百分比在图表下方列出。' },
    grid: { left: compact.value ? 6 : 25, right: compact.value ? 12 : 65, top: 78, bottom: 65, containLabel: true },
    legend: { type: 'scroll', top: 12, left: 6, right: 6, textStyle: { color: '#9aa7b6', fontSize: 11 }, itemWidth: 16, itemHeight: 3, pageTextStyle: { color: '#9aa7b6' } },
    tooltip: { trigger: 'axis', confine: true, renderMode: 'richText', backgroundColor: '#111720', borderColor: '#2d3a4b', textStyle: { color: '#f2f5f8', fontSize: 12 }, valueFormatter: (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : '缺失' },
    xAxis: { type: 'category', boundaryGap: false, data: data.labels, axisLine: { lineStyle: { color: '#2d3a4b' } }, axisTick: { show: false }, axisLabel: { color: '#667386', fontSize: 10, hideOverlap: true, formatter: (value: string) => value.slice(0, 7) } },
    yAxis: { type: 'value', scale: true, boundaryGap: ['18%', '22%'], axisLabel: { color: '#667386', fontSize: 10, formatter: (value: number) => value.toFixed(2) }, splitLine: { lineStyle: { color: '#202b39', type: 'dashed' } } },
    dataZoom: [
      { type: 'inside', start: 0, end: 100, filterMode: 'none' },
      { type: 'slider', start: 0, end: 100, filterMode: 'none', height: 20, bottom: 12, borderColor: 'transparent', backgroundColor: '#111720', fillerColor: 'rgba(77,141,255,.16)', handleStyle: { color: '#4d8dff' }, textStyle: { color: '#9aa7b6' } },
    ],
    series: data.series.map((series, index) => ({
      name: series.name, type: 'line', data: series.values, showSymbol: false, connectNulls: false, smooth: false,
      lineStyle: { width: index === 0 ? 2.2 : 1.6, color: series.color || ['#ee5b65', '#4d8dff', '#4cc3a1'][index % 3] },
      itemStyle: { color: series.color || ['#ee5b65', '#4d8dff', '#4cc3a1'][index % 3] },
      emphasis: { focus: 'series' },
      markArea: index === 0 ? { silent: true, label: { show: false }, data: areas } : undefined,
      markPoint: index === 0 ? { symbol: 'circle', symbolSize: 8, data: points } : undefined,
    })),
  }
})
</script>

<style scoped>
.research-comparison { min-width: 0; margin-top: 24px; }
.comparison-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.eyebrow { color: var(--accent); font-size: 10px; letter-spacing: .13em; }
h2 { margin: 6px 0 0; font-size: 18px; }
.export-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 12px; }
button { border: 1px solid var(--border-strong); border-radius: 6px; background: var(--bg-secondary); padding: 7px 12px; color: var(--text-secondary); }
button:disabled { opacity: .5; cursor: wait; }
.chart-state { display: grid; justify-items: center; align-content: center; gap: 12px; min-height: 230px; color: var(--text-secondary); font-size: 13px; text-align: center; }
.error-state, .export-error { color: #eea98a; }
.comparison-chart { width: 100%; height: 440px; min-width: 0; }
.validation-note { margin: 16px 0 0; padding: 11px 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px; font-size: 12px; color: var(--text-secondary); line-height: 1.8; }
.validation-note.unresolved { color: #dcbb80; border-color: #514229; background: rgba(159,117,42,.08); }
.interval-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 8px 0 15px; }
.interval-card { border: 1px solid var(--border); border-radius: 8px; padding: 15px 16px; min-width: 0; }
.interval-card span { font-size: 11px; }.interval-card strong { display: block; font-size: 22px; margin: 8px 0; font-variant-numeric: tabular-nums; }.interval-card p { font-size: 12px; color: var(--text-secondary); margin: 0; overflow-wrap: anywhere; }
.gain { background: rgba(134,215,158,.05); color: #89dca6; }.drawdown { background: rgba(183,153,232,.06); color: #c1a5ec; }
.chart-note { color: var(--text-tertiary); font-size: 11px; line-height: 1.8; margin: 6px 0; overflow-wrap: anywhere; }
.limitations { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }.limitations summary { font-size: 12px; color: #dcbb80; cursor: pointer; }.limitations ul { margin: 10px 0 0; padding-left: 18px; color: var(--text-secondary); font-size: 12px; line-height: 1.9; overflow-wrap: anywhere; }
@media(max-width:640px) { .comparison-heading { align-items: flex-start; flex-direction: column; }.comparison-chart { height: 370px; }.interval-grid { grid-template-columns: 1fr; }.interval-card { padding: 12px; }h2 { font-size: 16px; } }
</style>
