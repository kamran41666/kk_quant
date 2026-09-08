<template>
  <div class="page">
    <div class="page-header">
      <div><p class="eyebrow">策略工作台</p><h1>策略管理</h1></div>
      <button class="btn-primary" type="button" @click="showForm = !showForm">{{ showForm ? '取消' : '+ 新建策略' }}</button>
    </div>

    <section class="card protocol-card" aria-labelledby="protocol-title">
      <div class="protocol-header">
        <div><div class="section-kicker">统一执行协议</div><h2 id="protocol-title">策略协议 {{ protocol.version || '1.0' }}</h2></div>
        <span class="protocol-badge">{{ protocol.id || 'kk-quant-strategy-v1' }}</span>
      </div>
      <p class="protocol-summary">同一套策略可以进入回测与模拟观察；你只需要实现“初始化”和“生成目标权重”两个入口。</p>
      <div class="protocol-flow" aria-label="策略执行流程">
        <span v-for="(step, index) in protocol.lifecycle" :key="step" class="flow-step"><b>{{ index + 1 }}</b>{{ hookLabel(step) }}<i v-if="index < protocol.lifecycle.length - 1">→</i></span>
      </div>
      <div class="contract-grid">
        <div><span>必须实现</span><strong>initialize · generate_signals</strong></div>
        <div><span>输出格式</span><strong>代码 → 目标权重</strong></div>
        <div><span>适用市场</span><strong>{{ protocol.supported_markets.map(marketLabel).join(' · ') || 'A 股 · 国内基金' }}</strong></div>
        <div><span>执行约束</span><strong>交易日、费用、风控由引擎统一处理</strong></div>
      </div>
    </section>

    <section v-if="showForm" class="card form-card" aria-labelledby="create-title">
      <div class="form-title-row">
        <div><h2 id="create-title">按协议新建策略</h2><p class="hint">从自动发现的策略模板创建实例，参数会在服务端再次校验。</p></div>
        <span class="protocol-badge">协议 {{ protocol.version || '1.0' }}</span>
      </div>
      <div class="form-grid">
        <label>策略模板
          <select v-model="form.strategy_class" @change="applyTemplate">
            <option value="">-- 选择已发现模板 --</option>
            <option v-for="item in catalog" :key="item.id" :value="item.implementation">{{ item.name }} · v{{ item.version }}</option>
          </select>
        </label>
        <label>策略名称<input v-model.trim="form.name" placeholder="均线动量策略" /></label>
        <label>适用市场<select v-model="form.market"><option v-for="market in selectedTemplate?.markets || []" :key="market" :value="market">{{ marketLabel(market) }}</option></select></label>
        <label>策略说明<textarea v-model.trim="form.description" rows="2" placeholder="说明信号、持仓和调仓逻辑"></textarea></label>
        <div v-if="selectedTemplate" class="wide-field parameter-panel">
          <div class="parameter-heading"><strong>策略参数</strong><span>建议调仓：{{ frequencyLabel(selectedTemplate.execution.rebalance_frequency) }}</span></div>
          <div class="parameter-grid">
            <label v-for="parameter in selectedTemplate.parameters" :key="parameter.key">
              {{ parameter.label }}
              <select v-if="parameter.choices.length" v-model="form.params[parameter.key]">
                <option v-for="choice in parameter.choices" :key="String(choice)" :value="choice">{{ choice }}</option>
              </select>
              <input v-else-if="parameter.type === 'boolean'" v-model="form.params[parameter.key]" type="checkbox" />
              <input v-else-if="parameter.type === 'integer' || parameter.type === 'number'" v-model.number="form.params[parameter.key]" type="number" :min="parameter.minimum ?? undefined" :max="parameter.maximum ?? undefined" :step="parameter.type === 'integer' ? 1 : 'any'" />
              <input v-else v-model="form.params[parameter.key]" type="text" />
              <span class="field-hint">{{ parameter.description || rangeHint(parameter) }}</span>
            </label>
          </div>
          <p class="data-requirement">数据需求：{{ selectedTemplate.data.map(item => `${item.dataset} · 至少 ${requiredBars(selectedTemplate, item)} 条 · ${item.frequency}/${item.adjustment} · ${item.fields.join('/')}`).join('；') || '无额外声明' }}</p>
        </div>
      </div>
      <button class="btn-primary" type="button" :disabled="saving" @click="createStrategy">{{ saving ? '保存中…' : '保存策略' }}</button>
      <p v-if="error" class="error">{{ error }}</p>
    </section>

    <section class="section strategy-list" aria-labelledby="list-title">
      <div class="section-title-row"><div><h2 id="list-title">已登记策略</h2><span class="list-count">{{ strategies.length }} 个</span></div><span class="hint">新建策略按协议 {{ protocol.version || '1.0' }} 校验</span></div>
      <div v-if="strategies.length" class="strategy-grid">
        <article v-for="s in strategies" :key="s.id" class="card strategy-card">
          <div class="strategy-card-head"><div><h3>{{ s.name }}</h3><span class="market-tag">{{ marketLabel(s.market) }}</span></div><span class="protocol-mark" :class="{ legacy: !isProtocolCompatible(s) }">{{ isProtocolCompatible(s) ? `协议 ${s.protocol_version || protocol.version || '1.0'}` : '旧记录' }}</span></div>
          <p class="strategy-description">{{ s.description || '未填写策略说明' }}</p>
          <div class="strategy-entry"><span>执行入口</span><code>{{ s.strategy_class }}</code></div>
          <div class="strategy-meta"><span>参数 {{ Object.keys(s.params || {}).length }} 项</span><span>更新 {{ s.updated_at?.slice(0, 10) || '-' }}</span></div>
          <p v-if="!isProtocolCompatible(s) && s.compatibility_error" class="compatibility-error">{{ s.compatibility_error }}</p>
          <details class="params-details"><summary>查看参数</summary><pre>{{ JSON.stringify(s.params, null, 2) }}</pre></details>
          <div class="strategy-actions"><button class="btn-sm danger" type="button" @click="deleteStrategy(s.id)">删除</button></div>
        </article>
      </div>
      <p v-else class="hint empty-state">暂无策略 — 点击“新建策略”开始</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useApi } from '@/composables/useApi'
import type { Strategy, StrategyCatalog, StrategyParameterSpec, StrategySpec } from '@/types/api'

type StrategyProtocol = { id: string; version: string; lifecycle: string[]; supported_markets: string[] }
const { api } = useApi()
const strategies = ref<Strategy[]>([])
const showForm = ref(false)
const saving = ref(false)
const error = ref('')
const protocol = ref<StrategyProtocol>({ id: '', version: '', lifecycle: [], supported_markets: [] })
const catalog = ref<StrategySpec[]>([])
const emptyForm = () => ({ name: '', strategy_class: '', description: '', market: 'a-share' as 'a-share' | 'cn-fund' | 'us-equity', params: {} as Record<string, unknown> })
const form = ref(emptyForm())
const selectedTemplate = computed(() => catalog.value.find(item => item.implementation === form.value.strategy_class))

async function loadStrategies() { try { const { data } = await api.get<Strategy[]>('/strategies'); strategies.value = data } catch (e: any) { error.value = '策略加载失败: ' + e.message } }
async function loadProtocol() {
  try { const { data } = await api.get<StrategyProtocol>('/strategies/protocol'); protocol.value = data }
  catch (e) { protocol.value = { id: 'kk-quant-strategy-v1', version: '1.0', lifecycle: ['initialize', 'before_trading', 'generate_signals', 'on_rebalance', 'on_order_filled', 'teardown'], supported_markets: ['a-share', 'cn-fund'] } }
}
async function loadCatalog() {
  try {
    const { data } = await api.get<StrategyCatalog>('/strategies/catalog')
    catalog.value = data.strategies
  } catch (e: any) {
    error.value = '策略模板加载失败: ' + e.message
  }
}
function applyTemplate() {
  const template = selectedTemplate.value
  if (!template) return
  form.value.name = template.name
  form.value.description = template.description
  form.value.market = template.markets[0] || 'a-share'
  form.value.params = Object.fromEntries(template.parameters.map(item => [item.key, item.default]))
}
async function createStrategy() {
  if (!form.value.name || !form.value.strategy_class) { error.value = '请选择策略模板并填写策略名称'; return }
  try { saving.value = true; error.value = ''; await api.post('/strategies', { name: form.value.name, strategy_class: form.value.strategy_class, description: form.value.description, market: form.value.market, params: form.value.params }); showForm.value = false; form.value = emptyForm(); await loadStrategies() }
  catch (e: any) { error.value = e.response?.data?.detail || e.message }
  finally { saving.value = false }
}
async function deleteStrategy(id: string) { try { await api.delete(`/strategies/${id}`); await loadStrategies() } catch (e: any) { error.value = '删除失败: ' + e.message } }
function marketLabel(market?: string) { return ({ 'a-share': 'A 股', 'cn-fund': '国内基金', 'us-equity': '美股' } as Record<string, string>)[market || 'a-share'] || market || '未指定' }
function hookLabel(hook: string) { return ({ initialize: '初始化', before_trading: '盘前', generate_signals: '生成信号', on_rebalance: '调仓', on_order_filled: '成交', teardown: '结束' } as Record<string, string>)[hook] || hook }
function frequencyLabel(value: string) { return ({ daily: '每日', weekly: '每周', monthly: '每月' } as Record<string, string>)[value] || value }
function rangeHint(parameter: StrategyParameterSpec) {
  if (parameter.minimum == null && parameter.maximum == null) return parameter.required ? '必填' : '可选'
  return `范围 ${parameter.minimum ?? '不限'} ～ ${parameter.maximum ?? '不限'}`
}
function requiredBars(template: StrategySpec | undefined, requirement: StrategySpec['data'][number]) {
  const parameterValue = requirement.lookback_parameter ? Number(form.value.params[requirement.lookback_parameter]) : requirement.lookback
  const dynamicLookback = Number.isFinite(parameterValue) ? parameterValue + (requirement.lookback_offset || 0) : requirement.lookback
  return Math.max(template?.execution.warmup_bars || 0, requirement.lookback, dynamicLookback)
}
function isProtocolCompatible(strategy: Strategy) { return strategy.protocol_compatible === true }
onMounted(() => { loadStrategies(); loadProtocol(); loadCatalog() })
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 20px; }
.page-header h1 { margin: 0; }.eyebrow, .section-kicker { margin: 0 0 5px; color: var(--accent); font-size: 12px; font-weight: 600; }
.btn-primary { background: var(--accent); color: #fff; padding: 10px 20px; border-radius: 6px; font-size: 14px; }.btn-primary:hover { background: var(--accent-hover); }.btn-primary:disabled { opacity: .55; cursor: not-allowed; }
.protocol-card { margin-bottom: 20px; }.protocol-header, .form-title-row, .section-title-row, .strategy-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }.protocol-header h2, .form-title-row h2, .section-title-row h2 { margin: 0; }
.protocol-badge, .protocol-mark, .market-tag, .list-count { color: var(--accent); font-size: 12px; white-space: nowrap; }.protocol-mark.legacy { color: var(--text-tertiary); }.protocol-summary { margin: 10px 0 16px; color: var(--text-secondary); }
.protocol-flow { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 18px; }.flow-step { display: inline-flex; align-items: center; gap: 6px; color: var(--text-primary); font-size: 12px; }.flow-step b { display: grid; width: 20px; height: 20px; place-items: center; border-radius: 50%; background: rgba(59,130,246,.16); color: var(--accent); font-size: 11px; }.flow-step i { margin-left: 3px; color: var(--text-tertiary); font-style: normal; }
.contract-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }.contract-grid div { display: grid; gap: 5px; padding: 11px 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-secondary); }.contract-grid span, .strategy-entry span, .strategy-meta { color: var(--text-secondary); font-size: 11px; }.contract-grid strong { font-size: 12px; font-weight: 600; }
.form-card { margin-bottom: 24px; }.form-title-row { margin-bottom: 16px; }.form-title-row .hint { margin: 5px 0 0; text-align: left; }.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }.form-grid label { display: flex; flex-direction: column; gap: 5px; color: var(--text-secondary); font-size: 13px; }.form-grid input, .form-grid textarea, .form-grid select { font-size: 14px; }.wide-field { grid-column: 1 / -1; }.field-hint { color: var(--text-tertiary); font-size: 11px; }.parameter-panel { padding: 14px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-secondary); }.parameter-heading { display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 12px; }.parameter-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }.parameter-grid input[type='checkbox'] { width: 18px; min-width: 18px; height: 18px; }.data-requirement { margin: 12px 0 0; color: var(--text-tertiary); font-size: 11px; }
.error, .compatibility-error { color: var(--red); font-size: 13px; margin-top: 12px; }.compatibility-error { margin-bottom: 0; }.section { margin-top: 24px; }.section-title-row { align-items: center; margin-bottom: 12px; }.section-title-row > div { display: flex; align-items: baseline; gap: 10px; }.section-title-row .hint { margin: 0; }.strategy-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }.strategy-card { padding: 18px; }.strategy-card h3 { margin: 0 0 5px; }.market-tag { color: var(--text-secondary); }.strategy-description { min-height: 34px; margin: 14px 0; color: var(--text-secondary); font-size: 13px; line-height: 1.5; }.strategy-entry { display: grid; gap: 5px; padding: 10px 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }.strategy-entry code { overflow: hidden; color: var(--text-primary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }.strategy-meta { display: flex; justify-content: space-between; margin-top: 10px; }.params-details { margin-top: 12px; color: var(--text-secondary); font-size: 12px; }.params-details pre { max-height: 130px; overflow: auto; margin: 8px 0 0; border-radius: 5px; background: var(--bg-secondary); padding: 10px; color: var(--text-primary); font-size: 11px; }.strategy-actions { display: flex; justify-content: flex-end; margin-top: 12px; }.btn-sm { border: 1px solid var(--border); border-radius: 4px; background: var(--bg-secondary); color: var(--text-secondary); padding: 4px 12px; font-size: 12px; }.btn-sm.danger:hover { border-color: var(--red); color: var(--red); }.hint { color: var(--text-secondary); margin-top: 12px; }.empty-state { padding: 24px; text-align: center; }
@media (max-width: 780px) { .contract-grid, .strategy-grid { grid-template-columns: 1fr 1fr; }.parameter-grid { grid-template-columns: 1fr 1fr; } } @media (max-width: 560px) { .page-header, .protocol-header, .form-title-row, .section-title-row { align-items: stretch; flex-direction: column; } .form-grid, .contract-grid, .strategy-grid, .parameter-grid { grid-template-columns: 1fr; } .wide-field { grid-column: auto; } .section-title-row .hint { text-align: left; } }
</style>
