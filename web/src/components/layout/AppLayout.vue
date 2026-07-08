<template>
  <div class="app-layout">
    <SideNav />
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import SideNav from './SideNav.vue'
</script>

<style scoped>
.app-layout { display: flex; min-height: 100vh; }
.main-content { flex: 1; padding: 24px; overflow-y: auto; }
</style>
