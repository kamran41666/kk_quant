import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'Dashboard', component: () => import('../views/Dashboard.vue') },
    { path: '/market', name: 'Market', component: () => import('../views/MarketCenter.vue') },
    { path: '/market/detail', name: 'MarketDetail', component: () => import('../views/MarketDetail.vue') },
    { path: '/strategies', name: 'Strategies', component: () => import('../views/StrategyManager.vue') },
    { path: '/backtest', name: 'Backtest', component: () => import('../views/BacktestRunner.vue') },
    { path: '/paper', name: 'Paper', component: () => import('../views/PaperTrading.vue') },
    // Keep old bookmarks working while the performance view lives in BacktestRunner.
    { path: '/analytics', name: 'Analytics', redirect: { name: 'Backtest' } },
    { path: '/live', name: 'LiveReadiness', component: () => import('../views/LiveReadiness.vue') },
  ]
})

export default router
