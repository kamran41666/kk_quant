<template>
  <div class="app-shell">
    <SideNav />
    <section class="workspace">
      <header class="topbar">
        <div class="mobile-brand" aria-label="kk quant">KQ</div>
        <div class="market-context">
          <span class="eyebrow">个人量化研究工作台</span>
          <span class="market-session">Asia/Shanghai</span>
        </div>
        <div class="environment-badge" title="当前不会向真实券商发送委托">研究 / 模拟环境</div>
      </header>
      <main class="main-content" id="main-content">
        <router-view />
      </main>
    </section>
  </div>
</template>

<script setup lang="ts">
import SideNav from './SideNav.vue'
</script>

<style scoped>
.app-shell { display: flex; min-height: 100vh; }
.workspace { min-width: 0; flex: 1; padding-left: var(--sidebar-width); }
.topbar {
  position: sticky;
  z-index: 20;
  top: 0;
  display: flex;
  height: var(--topbar-height);
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  background: rgba(13, 17, 24, 0.94);
  padding: 0 28px;
  backdrop-filter: blur(12px);
}
.market-context { display: flex; align-items: center; gap: 16px; }
.eyebrow { color: var(--text-secondary); font-size: 12px; font-weight: 620; letter-spacing: 0.04em; }
.market-session { display: inline-flex; align-items: center; gap: 7px; color: var(--text-tertiary); font-size: 11px; }
.environment-badge {
  border: 1px solid rgba(228, 168, 58, 0.28);
  border-radius: 999px;
  background: rgba(228, 168, 58, 0.08);
  color: #edbd62;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 650;
}
.mobile-brand { display: none; }
.main-content { min-height: calc(100vh - var(--topbar-height)); padding: 30px clamp(18px, 2.6vw, 40px) 48px; }
@media (max-width: 820px) {
  .workspace { padding-left: 0; padding-bottom: 68px; }
  .topbar { padding: 0 16px; }
  .mobile-brand {
    display: grid;
    width: 30px;
    height: 30px;
    place-items: center;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    color: var(--text-primary);
    font-family: "SFMono-Regular", Consolas, monospace;
    font-size: 11px;
    font-weight: 700;
  }
  .eyebrow, .market-session { display: none; }
  .main-content { padding: 22px 14px 32px; }
}
</style>
