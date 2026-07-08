import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useDashboardStore = defineStore('dashboard', () => {
  const totalValue = ref(0)
  const dailyReturn = ref(0)
  const sharpeRatio = ref(0)
  const maxDrawdown = ref(0)
  const equityCurve = ref<{ date: string; value: number }[]>([])
  const recentTrades = ref<{ code: string; side: string; shares: number; price: number; date: string }[]>([])

  function updateFromWS(msg: any) {
    if (msg.type === 'portfolio_update') {
      const d = msg.data
      totalValue.value = d.total_value
      dailyReturn.value = d.daily_return
      sharpeRatio.value = d.sharpe_ratio ?? 0
      maxDrawdown.value = d.max_drawdown ?? 0
    }
    if (msg.type === 'order_fill') {
      recentTrades.value.unshift(msg.data)
      if (recentTrades.value.length > 20) recentTrades.value.pop()
    }
  }

  return { totalValue, dailyReturn, sharpeRatio, maxDrawdown, equityCurve, recentTrades, updateFromWS }
})
