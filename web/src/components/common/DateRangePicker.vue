<template>
  <div class="date-range">
    <label>
      起始日期
      <input type="date" :value="start" @input="$emit('update:start', ($event.target as HTMLInputElement).value)" />
    </label>
    <label>
      结束日期
      <input type="date" :value="end" @input="$emit('update:end', ($event.target as HTMLInputElement).value)" />
    </label>
  </div>
</template>

<script setup lang="ts">
defineProps<{ start: string; end: string }>()
defineEmits<{ 'update:start': [value: string]; 'update:end': [value: string] }>()
</script>

<style scoped>
.date-range { display: flex; gap: 16px; }
.date-range label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); }
@media (max-width: 480px) {
  .date-range { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr); }
  .date-range input { width: 100%; min-width: 0; }
}
</style>
