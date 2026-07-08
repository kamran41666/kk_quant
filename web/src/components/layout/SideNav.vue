<template>
  <nav class="sidenav">
    <div class="logo">kk_quant</div>
    <router-link v-for="item in navItems" :key="item.path" :to="item.path"
                 class="nav-item" active-class="active">
      <span class="icon">{{ item.icon }}</span>
      <span>{{ item.label }}</span>
    </router-link>
  </nav>
</template>

<script setup lang="ts">
const navItems = [
  { path: '/', label: '仪表盘', icon: '📊' },
  { path: '/strategies', label: '策略管理', icon: '📋' },
  { path: '/backtest', label: '回测中心', icon: '⚙️' },
  { path: '/paper', label: '模拟交易', icon: '📈' },
  { path: '/analytics', label: '绩效分析', icon: '📉' },
]
</script>

<style scoped>
.sidenav {
  width: var(--sidebar-width);
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  padding: 20px 0;
  display: flex;
  flex-direction: column;
}
.logo {
  font-size: 20px; font-weight: 700; padding: 0 20px 24px;
  color: var(--accent);
}
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 20px; color: var(--text-secondary);
  transition: all 0.2s; border-left: 3px solid transparent;
}
.nav-item:hover { background: var(--bg-card); color: var(--text-primary); }
.nav-item.active {
  background: var(--bg-card); color: var(--accent);
  border-left-color: var(--accent);
}
.icon { font-size: 18px; }
</style>
