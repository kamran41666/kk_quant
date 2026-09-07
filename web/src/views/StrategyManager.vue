<template>
  <div class="page">
    <div class="page-header">
      <h1>策略管理</h1>
      <button class="btn-primary" @click="showForm = !showForm">
        {{ showForm ? '取消' : '+ 新建策略' }}
      </button>
    </div>

    <!-- Create Form -->
    <div class="card form-card" v-if="showForm">
      <h3>新建策略</h3>
      <div class="form-grid">
        <label>名称 <input v-model="form.name" placeholder="均线动量策略" /></label>
        <label>策略类 <input v-model="form.strategy_class" placeholder="my_strategies.Momentum" /></label>
        <label>适用市场
          <select v-model="form.market">
            <option value="a-share">A 股</option>
            <option value="cn-fund">国内基金</option>
            <option value="us-equity">美股（策略回测暂未开放）</option>
          </select>
        </label>
        <label>描述 <textarea v-model="form.description" rows="2" placeholder="策略描述..."></textarea></label>
        <label>参数 (JSON) <textarea v-model="form.paramsStr" rows="3" placeholder='{"top_n": 50, "lookback": 60}'></textarea></label>
      </div>
      <button class="btn-primary" @click="createStrategy">保存</button>
      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <!-- List -->
    <table class="data-table" v-if="strategies.length > 0">
      <thead>
        <tr><th>名称</th><th>市场</th><th>策略类</th><th>参数</th><th>更新时间</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="s in strategies" :key="s.id">
          <td>{{ s.name }}</td>
          <td>{{ marketLabel(s.market) }}</td>
          <td><code>{{ s.strategy_class }}</code></td>
          <td>{{ JSON.stringify(s.params) }}</td>
          <td>{{ s.updated_at?.slice(0, 10) }}</td>
          <td>
            <button class="btn-sm" @click="deleteStrategy(s.id)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="hint">暂无策略 — 点击"新建策略"开始</p>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import type { Strategy } from '@/types/api'

const { api } = useApi()
const strategies = ref<Strategy[]>([])
const showForm = ref(false)
const error = ref('')

const form = ref({
  name: '',
  strategy_class: '',
  description: '',
  market: 'a-share' as 'a-share' | 'cn-fund' | 'us-equity',
  paramsStr: '{}',
})

async function loadStrategies() {
  try {
    const { data } = await api.get<Strategy[]>('/strategies')
    strategies.value = data
  } catch (e: any) {
    error.value = '加载失败: ' + e.message
  }
}

async function createStrategy() {
  try {
    error.value = ''
    const params = JSON.parse(form.value.paramsStr)
    await api.post('/strategies', {
      name: form.value.name,
      strategy_class: form.value.strategy_class,
      description: form.value.description,
      market: form.value.market,
      params,
    })
    showForm.value = false
    form.value = { name: '', strategy_class: '', description: '', market: 'a-share', paramsStr: '{}' }
    await loadStrategies()
  } catch (e: any) {
    error.value = e.response?.data?.detail || e.message
  }
}

async function deleteStrategy(id: string) {
  try {
    await api.delete(`/strategies/${id}`)
    await loadStrategies()
  } catch (e: any) {
    error.value = '删除失败: ' + e.message
  }
}

onMounted(loadStrategies)

function marketLabel(market?: string) {
  return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || 'A 股'
}
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.btn-primary { background: var(--accent); color: #fff; padding: 10px 20px; border-radius: 6px; font-size: 14px; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-sm { background: var(--bg-secondary); color: var(--text-secondary); padding: 4px 12px; border-radius: 4px; font-size: 12px; border: 1px solid var(--border); }
.btn-sm:hover { color: var(--red); border-color: var(--red); }
.form-card { margin-bottom: 24px; }
.form-card h3 { margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); }
.form-grid input, .form-grid textarea { font-size: 14px; }
.error { color: var(--red); font-size: 13px; margin-top: 12px; }
.data-table { width: 100%; border-collapse: collapse; }
.data-table th, .data-table td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); font-size: 14px; }
.data-table th { color: var(--text-secondary); font-weight: 600; }
.hint { color: var(--text-secondary); margin-top: 12px; }
</style>
