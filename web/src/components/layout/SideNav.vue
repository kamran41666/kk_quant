<template>
  <aside class="sidenav">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <div class="brand-block">
      <div class="brand-mark" aria-hidden="true">KQ</div>
      <div>
        <div class="brand-name">kk_quant</div>
        <div class="brand-caption">PERSONAL RESEARCH</div>
      </div>
    </div>

    <nav class="nav-list" aria-label="主导航">
      <router-link
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        class="nav-item"
        active-class="active"
        :aria-label="item.label"
      >
        <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="item.icon" /></svg>
        <span>{{ item.label }}</span>
      </router-link>
    </nav>

    <div class="nav-footer">
      <span>个人研究工作台</span>
    </div>
  </aside>
</template>

<script setup lang="ts">
const navItems = [
  { path: '/', label: '总览', icon: 'M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z' },
  { path: '/market', label: '市场行情', icon: 'M4 18 9 13l3 3 7-8m-5 0h5v5' },
  { path: '/data', label: '研究数据', icon: 'M5 6c0-1.1 3.1-2 7-2s7 .9 7 2-3.1 2-7 2-7-.9-7-2Zm0 0v6c0 1.1 3.1 2 7 2s7-.9 7-2V6m-14 6v6c0 1.1 3.1 2 7 2s7-.9 7-2v-6' },
  { path: '/strategies', label: '策略', icon: 'M6 4h12v16H6V4Zm3 4h6M9 12h6m-6 4h4' },
  { path: '/backtest', label: '回测', icon: 'M12 3a9 9 0 1 0 9 9M12 7v5l3 2m2-10v5h-5' },
  { path: '/paper', label: '模拟交易', icon: 'M4 19V9m5 10V5m6 14v-7m5 7V3' },
  { path: '/live', label: '账户接入', icon: 'M12 3 4 7v5c0 4.6 3.4 8.6 8 9 4.6-.4 8-4.4 8-9V7l-8-4Zm0 5v4m0 4h.01' },
]
</script>

<style scoped>
.sidenav {
  position: fixed;
  z-index: 30;
  inset: 0 auto 0 0;
  display: flex;
  width: var(--sidebar-width);
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--bg-primary);
  padding: 22px 14px 18px;
}
.skip-link {
  position: absolute;
  top: -60px;
  left: 12px;
  z-index: 100;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: white;
  padding: 8px 12px;
}
.skip-link:focus { top: 10px; }
.brand-block { display: flex; align-items: center; gap: 11px; padding: 0 8px 28px; }
.brand-mark {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid var(--border-strong);
  border-radius: 7px;
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 12px;
  font-weight: 720;
  letter-spacing: -0.05em;
}
.brand-name { font-size: 15px; font-weight: 680; letter-spacing: -0.02em; }
.brand-caption { margin-top: 2px; color: var(--text-tertiary); font-size: 8px; font-weight: 700; letter-spacing: 0.13em; }
.nav-list { display: grid; gap: 4px; }
.nav-item {
  display: flex;
  min-height: 42px;
  align-items: center;
  gap: 11px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text-secondary);
  padding: 9px 11px;
  font-size: 13px;
  font-weight: 560;
  transition: color 120ms ease, background 120ms ease, border-color 120ms ease;
}
.nav-item:hover { background: var(--bg-secondary); color: var(--text-primary); }
.nav-item.active { border-color: var(--border); background: var(--bg-elevated); color: var(--text-primary); }
.nav-item.active .nav-icon { color: var(--accent); }
.nav-icon {
  width: 17px;
  height: 17px;
  flex: 0 0 auto;
  color: currentColor;
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.7;
}
.nav-item:first-child .nav-icon { fill: currentColor; stroke: none; }
.nav-footer {
  display: grid;
  gap: 5px;
  margin-top: auto;
  border-top: 1px solid var(--border);
  color: var(--text-tertiary);
  padding: 16px 9px 0;
  font-size: 10px;
}
.status-row { display: flex; align-items: center; gap: 7px; color: var(--text-secondary); font-size: 11px; }
@media (max-width: 820px) {
  .sidenav {
    inset: auto 0 0;
    width: auto;
    height: 68px;
    border-top: 1px solid var(--border);
    border-right: 0;
    background: rgba(13, 17, 24, 0.98);
    padding: 6px 8px 5px;
    backdrop-filter: blur(12px);
  }
  .brand-block, .nav-footer, .skip-link { display: none; }
  .nav-list { display: grid; height: 100%; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 1px; }
  .nav-item {
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    padding: 4px 2px;
    font-size: clamp(7px, 2.25vw, 9px);
  }
  .nav-icon { width: 18px; height: 18px; }
}
@media (max-width: 360px) {
  .sidenav { padding-inline: 3px; }
  .nav-item { gap: 2px; padding-inline: 0; }
  .nav-icon { width: 16px; height: 16px; }
}
</style>
