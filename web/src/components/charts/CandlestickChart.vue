<template>
  <VChart class="candlestick-chart" :option="option" autoresize />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart as EChartsCandlestick } from 'echarts/charts'
import { AriaComponent, DataZoomComponent, GridComponent, TooltipComponent } from 'echarts/components'

use([CanvasRenderer, EChartsCandlestick, AriaComponent, DataZoomComponent, GridComponent, TooltipComponent])

export interface CandlePoint { date: string; open: number | null; close: number | null; low: number | null; high: number | null }
const props = withDefaults(defineProps<{ title: string; data: CandlePoint[]; zoom?: boolean }>(), { zoom: true })
const option = computed(() => ({
  animation: false,
  aria: { enabled: true, description: `${props.title}，包含开盘、收盘、最低和最高价。` },
  grid: { left: 10, right: 16, top: 18, bottom: props.zoom ? 48 : 24, containLabel: true },
  tooltip: {
    trigger: 'axis', confine: true, backgroundColor: '#111720', borderColor: '#2d3a4b', textStyle: { color: '#f2f5f8', fontSize: 12 },
    formatter: (params: Array<{ dataIndex: number }>) => { const point = props.data[params[0]?.dataIndex]; if (!point) return ''; const value = (item: number | null) => item == null ? '—' : item.toFixed(2); return `${point.date}<br/>开 ${value(point.open)}　高 ${value(point.high)}<br/>低 ${value(point.low)}　收 ${value(point.close)}` },
  },
  xAxis: { type: 'category', data: props.data.map(item => item.date), boundaryGap: true, axisLine: { lineStyle: { color: '#2d3a4b' } }, axisTick: { show: false }, axisLabel: { color: '#667386', fontSize: 10, hideOverlap: true } },
  yAxis: { type: 'value', scale: true, axisLabel: { color: '#667386', fontSize: 10, formatter: (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }, splitLine: { lineStyle: { color: '#202b39', type: 'dashed' } } },
  dataZoom: props.zoom ? [{ type: 'inside', start: 0, end: 100 }, { type: 'slider', height: 16, bottom: 7, borderColor: 'transparent', backgroundColor: '#111720', fillerColor: 'rgba(77,141,255,.16)', handleStyle: { color: '#4d8dff' }, textStyle: { color: '#667386' } }] : undefined,
  series: [{ type: 'candlestick', data: props.data.map(item => [item.open, item.close, item.low, item.high]), itemStyle: { color: '#ef5b64', color0: '#36b37e', borderColor: '#ef5b64', borderColor0: '#36b37e' } }],
}))
</script>

<style scoped>
.candlestick-chart { width: 100%; height: 100%; min-height: 300px; }
</style>
