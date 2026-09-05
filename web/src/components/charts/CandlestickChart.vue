<template>
  <VChart class="candlestick-chart" :option="option" autoresize />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart as EChartsCandlestick, LineChart, BarChart } from 'echarts/charts'
import { AriaComponent, DataZoomComponent, GridComponent, TooltipComponent } from 'echarts/components'

use([CanvasRenderer, EChartsCandlestick, LineChart, BarChart, AriaComponent, DataZoomComponent, GridComponent, TooltipComponent])

export interface CandlePoint {
  date: string
  open: number | null
  close: number | null
  low: number | null
  high: number | null
  volume?: number | null
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  ema12?: number | null
  ema26?: number | null
  rsi14?: number | null
  macd?: number | null
  macd_signal?: number | null
  macd_hist?: number | null
  boll_mid?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
}
const props = withDefaults(defineProps<{ title: string; data: CandlePoint[]; zoom?: boolean; indicatorKeys?: string[] }>(), { zoom: true, indicatorKeys: () => [] })
const lineDefinitions: Record<string, { name: string; color: string }> = {
  ma5: { name: 'MA5', color: '#f2b84b' }, ma20: { name: 'MA20', color: '#4d8dff' }, ma60: { name: 'MA60', color: '#bb86fc' },
  ema12: { name: 'EMA12', color: '#50c7c7' }, ema26: { name: 'EMA26', color: '#ef8f6f' },
  boll_mid: { name: 'BOLL中轨', color: '#8aa0b8' }, boll_upper: { name: 'BOLL上轨', color: '#69c99e' }, boll_lower: { name: 'BOLL下轨', color: '#edbd62' },
}
const indicatorNames: Record<string, string> = {
  macd: 'MACD', macd_signal: 'DEA', macd_hist: 'MACD柱', rsi14: 'RSI14',
  kdj_k: 'KDJ-K', kdj_d: 'KDJ-D', kdj_j: 'KDJ-J',
}
const indicatorColors: Record<string, string> = {
  macd: '#4d8dff', macd_signal: '#f2b84b', macd_hist: '#69c99e', rsi14: '#bb86fc',
  kdj_k: '#4d8dff', kdj_d: '#f2b84b', kdj_j: '#ef8f6f',
}

const option = computed(() => {
  const dates = props.data.map(item => item.date)
  const subCharts: Array<{ name: string; fields: string[] }> = []
  if (props.indicatorKeys.some(key => ['macd', 'macd_signal', 'macd_hist'].includes(key))) subCharts.push({ name: 'MACD', fields: ['macd', 'macd_signal', 'macd_hist'] })
  if (props.indicatorKeys.includes('rsi14')) subCharts.push({ name: 'RSI14', fields: ['rsi14'] })
  if (props.indicatorKeys.some(key => ['kdj_k', 'kdj_d', 'kdj_j'].includes(key))) subCharts.push({ name: 'KDJ', fields: ['kdj_k', 'kdj_d', 'kdj_j'] })
  const mainGrid: Record<string, unknown> = { left: 10, right: 16, top: 18, containLabel: true }
  if (subCharts.length) mainGrid.height = '40%'
  else mainGrid.bottom = props.zoom ? 48 : 24
  const grid: Array<Record<string, unknown>> = [mainGrid]
  const xAxis: Array<Record<string, unknown>> = [{ type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#2d3a4b' } }, axisTick: { show: false }, axisLabel: { color: '#667386', fontSize: 10, hideOverlap: true, show: subCharts.length === 0 } }]
  const yAxis: Array<Record<string, unknown>> = [{ type: 'value', scale: true, axisLabel: { color: '#667386', fontSize: 10, formatter: (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }, splitLine: { lineStyle: { color: '#202b39', type: 'dashed' } } }]
  const series: Array<Record<string, unknown>> = [
    { type: 'candlestick', data: props.data.map(item => [item.open, item.close, item.low, item.high]), itemStyle: { color: '#ef5b64', color0: '#36b37e', borderColor: '#ef5b64', borderColor0: '#36b37e' } },
    ...props.indicatorKeys.filter(key => lineDefinitions[key]).map(key => ({
      type: 'line', name: lineDefinitions[key].name, data: props.data.map(item => item[key as keyof CandlePoint] ?? null), showSymbol: false, connectNulls: false,
      smooth: false, lineStyle: { width: 1, color: lineDefinitions[key].color }, itemStyle: { color: lineDefinitions[key].color },
    })),
  ]
  const subHeight = subCharts.length ? Math.max(9, Math.floor(34 / subCharts.length)) : 0
  subCharts.forEach((sub, index) => {
    const axisIndex = index + 1
    grid.push({ left: 10, right: 16, top: `${62 + index * subHeight}%`, height: `${subHeight - 2}%`, containLabel: true })
    xAxis.push({ type: 'category', data: dates, gridIndex: axisIndex, boundaryGap: true, axisLine: { lineStyle: { color: '#2d3a4b' } }, axisTick: { show: false }, axisLabel: { color: '#667386', fontSize: 9, hideOverlap: true, show: index === subCharts.length - 1 } })
    yAxis.push({ type: 'value', gridIndex: axisIndex, scale: true, name: sub.name, nameTextStyle: { color: '#667386', fontSize: 9 }, axisLabel: { color: '#667386', fontSize: 9, formatter: (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }, splitLine: { lineStyle: { color: '#202b39', type: 'dashed' } } })
    sub.fields.forEach(field => {
      if (!props.indicatorKeys.includes(field)) return
      if (field === 'macd_hist') {
        series.push({ type: 'bar', name: indicatorNames[field], xAxisIndex: axisIndex, yAxisIndex: axisIndex, data: props.data.map(item => item[field as keyof CandlePoint] ?? null), barWidth: '45%', itemStyle: { color: indicatorColors[field] } })
      } else {
        series.push({ type: 'line', name: indicatorNames[field], xAxisIndex: axisIndex, yAxisIndex: axisIndex, data: props.data.map(item => item[field as keyof CandlePoint] ?? null), showSymbol: false, connectNulls: false, lineStyle: { width: 1, color: indicatorColors[field] }, itemStyle: { color: indicatorColors[field] } })
      }
    })
  })
  return {
    animation: false,
    aria: { enabled: true, description: `${props.title}，包含开盘、收盘、最低、最高价和已启用指标。` },
    grid,
    tooltip: {
      trigger: 'axis', confine: true, backgroundColor: '#111720', borderColor: '#2d3a4b', textStyle: { color: '#f2f5f8', fontSize: 12 },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const point = props.data[params.find(item => item.dataIndex != null)?.dataIndex ?? 0]
        if (!point) return ''
        const value = (item: unknown) => typeof item !== 'number' || !Number.isFinite(item) ? '—' : item.toFixed(2)
        const values = [`${point.date}<br/>开 ${value(point.open)}　高 ${value(point.high)}<br/>低 ${value(point.low)}　收 ${value(point.close)}`]
        props.indicatorKeys.forEach(key => {
          const label = lineDefinitions[key]?.name ?? indicatorNames[key]
          if (label) values.push(`${label} ${value(point[key as keyof CandlePoint])}`)
        })
        return values.join('<br/>')
      },
    },
    xAxis,
    yAxis,
    dataZoom: props.zoom ? [{ type: 'inside', xAxisIndex: xAxis.map((_, index) => index), start: 0, end: 100 }, { type: 'slider', xAxisIndex: xAxis.map((_, index) => index), height: 16, bottom: 7, borderColor: 'transparent', backgroundColor: '#111720', fillerColor: 'rgba(77,141,255,.16)', handleStyle: { color: '#4d8dff' }, textStyle: { color: '#667386' } }] : undefined,
    series,
  }
})
</script>

<style scoped>
.candlestick-chart { width: 100%; height: 100%; min-height: 300px; }
</style>
