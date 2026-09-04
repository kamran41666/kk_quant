<template>
  <VChart class="line-chart" :option="option" autoresize />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { chartValue } from '@/utils/market'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'

use([CanvasRenderer, LineChart, AriaComponent, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent])

export interface LineChartSeries {
  name: string
  values: Array<number | null>
  color?: string
  area?: boolean
}

const props = withDefaults(defineProps<{
  labels: string[]
  series: LineChartSeries[]
  title: string
  percent?: boolean
  zoom?: boolean
}>(), {
  percent: false,
  zoom: false,
})

const option = computed(() => ({
  animation: false,
  aria: {
    enabled: true,
    description: `${props.title}。图表包含 ${props.series.map(item => item.name).join('、')}。`,
  },
  color: props.series.map(item => item.color ?? '#4d8dff'),
  grid: { left: 10, right: 16, top: props.series.length > 1 ? 42 : 22, bottom: props.zoom ? 48 : 24, containLabel: true },
  legend: props.series.length > 1 ? {
    top: 6,
    right: 8,
    textStyle: { color: '#9aa7b6', fontSize: 11 },
    itemWidth: 14,
    itemHeight: 3,
  } : undefined,
  tooltip: {
    trigger: 'axis',
    confine: true,
    backgroundColor: '#111720',
    borderColor: '#2d3a4b',
    textStyle: { color: '#f2f5f8', fontSize: 12 },
    valueFormatter: (value: unknown) => chartValue(value, props.percent),
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: props.labels,
    axisLine: { lineStyle: { color: '#2d3a4b' } },
    axisTick: { show: false },
    axisLabel: { color: '#667386', fontSize: 10, hideOverlap: true },
  },
  yAxis: {
    type: 'value',
    scale: true,
    axisLabel: {
      color: '#667386',
      fontSize: 10,
      formatter: (value: number) => props.percent ? `${value.toFixed(1)}%` : value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }),
    },
    splitLine: { lineStyle: { color: '#202b39', type: 'dashed' } },
  },
  dataZoom: props.zoom ? [
    { type: 'inside', start: 0, end: 100 },
    { type: 'slider', height: 16, bottom: 7, borderColor: 'transparent', backgroundColor: '#111720', fillerColor: 'rgba(77,141,255,.16)', handleStyle: { color: '#4d8dff' }, textStyle: { color: '#667386' } },
  ] : undefined,
  series: props.series.map(item => ({
    name: item.name,
    type: 'line',
    data: item.values,
    showSymbol: false,
    connectNulls: false,
    smooth: false,
    lineStyle: { width: 1.7 },
    areaStyle: item.area ? { opacity: 0.1 } : undefined,
    emphasis: { focus: 'series' },
  })),
}))
</script>

<style scoped>
.line-chart { width: 100%; height: 100%; min-height: 260px; }
</style>
