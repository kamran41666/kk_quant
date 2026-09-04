import { defineStore } from 'pinia'
import { ref } from 'vue'

export const usePaperStore = defineStore('paper', () => {
  const cash = ref<number | null>(null)
  const marketValue = ref<number | null>(null)
  const totalValue = ref<number | null>(null)
  const positions = ref<any[]>([])
  const signals = ref<{ code: string; weight: number }[]>([])

  function updateFromWS(msg: any) {
    if (msg.type === 'portfolio_update') {
      cash.value = msg.data.cash
      marketValue.value = msg.data.market_value
      totalValue.value = msg.data.total_value
      positions.value = msg.data.positions ?? []
    }
    if (msg.type === 'signal_snapshot') {
      signals.value = Object.entries(msg.data).map(([code, weight]) => ({ code, weight: weight as number }))
    }
  }

  function $reset() {
    cash.value = null
    marketValue.value = null
    totalValue.value = null
    positions.value = []
    signals.value = []
  }

  return { cash, marketValue, totalValue, positions, signals, updateFromWS, $reset }
})
