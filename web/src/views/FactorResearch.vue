<template>
  <div class="page factor-page">
    <div class="page-header">
      <div>
        <p class="page-kicker">RESTRICTED FACTOR LAB</p>
        <h1>因子研究</h1>
        <p class="page-subtitle">审查受限表达式、持久实验与训练记忆。所有计算都绑定冻结数据和明确门禁。</p>
      </div>
      <button class="btn-secondary" type="button" :disabled="loading" @click="loadAll">
        {{ loading ? '刷新中…' : '刷新研究状态' }}
      </button>
    </div>

    <section class="boundary-card" aria-label="研究边界">
      <div class="boundary-mark">!</div>
      <div>
        <strong>训练通过只允许进入下一研究阶段，不代表策略晋级。</strong>
        <p>当前页面不会创建交易策略、开启模拟观察或发送订单。验证通过后仍需正式组合回测、成本、换手、容量和新留出数据检验。</p>
      </div>
    </section>

    <section class="memory-grid" aria-label="训练记忆计数">
      <article class="memory-card good">
        <span>训练通过</span><strong>{{ memory?.counts.training_passed ?? 0 }}</strong><small>只可进入 validation</small>
      </article>
      <article class="memory-card bad">
        <span>训练拒绝</span><strong>{{ memory?.counts.training_rejected ?? 0 }}</strong><small>保留失败门禁</small>
      </article>
      <article class="memory-card warn">
        <span>执行失败</span><strong>{{ memory?.counts.failed ?? 0 }}</strong><small>可显式重试</small>
      </article>
      <article class="memory-card neutral">
        <span>旧版已评估</span><strong>{{ memory?.counts.legacy_evaluated ?? 0 }}</strong><small>不自动晋级</small>
      </article>
    </section>

    <p v-if="error" class="page-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="page-notice" role="status">{{ notice }}</p>

    <div class="workbench-grid">
      <section class="card action-card" aria-labelledby="generate-title">
        <div class="section-header compact">
          <div><h2 id="generate-title">生成候选</h2><p>确定性的 template-v1 会跳过记忆中已有的表达式。</p></div>
          <span class="mode-pill">无模型 · 无密钥</span>
        </div>
        <div class="inline-action">
          <label>本轮上限
            <select v-model.number="generationLimit"><option :value="1">1</option><option :value="2">2</option><option :value="4">4</option></select>
          </label>
          <button class="btn-accent" type="button" :disabled="generating" @click="generateTemplates">
            {{ generating ? '生成中…' : '运行 template-v1' }}
          </button>
        </div>
        <small class="action-footnote">模板只登记受限 AST；重复表达式由哈希和 research memory 排除。</small>
      </section>

      <section class="card action-card" aria-labelledby="queue-title">
        <div class="section-header compact">
          <div><h2 id="queue-title">创建实验</h2><p>实验进入持久队列，需由本地 factor worker 执行。</p></div>
          <span class="mode-pill queue">QUEUE</span>
        </div>
        <form class="experiment-form" @submit.prevent="queueExperiment">
          <label class="wide">候选
            <select v-model="experimentForm.candidate_id" required :disabled="!candidates.length">
              <option value="" disabled>{{ candidates.length ? '选择一个候选' : '请先生成或登记候选' }}</option>
              <option v-for="candidate in candidates" :key="candidate.id" :value="candidate.id">{{ candidate.name }} · {{ factorStatusLabel(candidate.status) }}</option>
            </select>
          </label>
          <label class="wide">冻结数据集 ID<input v-model.trim="experimentForm.dataset_id" required placeholder="ashare-inception-2014-v1/normalized-v2" /></label>
          <label>阶段<select v-model="experimentForm.stage"><option value="training">训练</option><option value="validation">验证</option><option value="holdout">留出</option></select></label>
          <label>持有期<input v-model.number="experimentForm.forward_horizon" type="number" min="1" max="60" required /></label>
          <label>开始日期<input v-model="experimentForm.start_date" type="date" required /></label>
          <label>结束日期<input v-model="experimentForm.end_date" type="date" required /></label>
          <button class="btn-primary wide" type="submit" :disabled="queueing || !candidates.length">{{ queueing ? '提交中…' : '加入实验队列' }}</button>
        </form>
        <small class="action-footnote">validation 需要同候选已有 training_passed；holdout 需要 validation_passed。</small>
      </section>
    </div>

    <details class="card manual-register">
      <summary>手工登记受限表达式</summary>
      <form class="candidate-form" @submit.prevent="registerCandidate">
        <label>名称<input v-model.trim="candidateForm.name" pattern="[a-z][a-z0-9_]{2,79}" required /></label>
        <label>方向<select v-model.number="candidateForm.direction"><option :value="1">+1 正向</option><option :value="-1">-1 反向</option></select></label>
        <label>角色<select v-model="candidateForm.role"><option value="rank">rank</option><option value="filter">filter</option><option value="risk">risk</option></select></label>
        <label>来源<input v-model.trim="candidateForm.source" required /></label>
        <label class="wide">金融假设<textarea v-model.trim="candidateForm.hypothesis" rows="2" required /></label>
        <label class="wide">表达式 JSON<textarea v-model="candidateExpression" rows="9" spellcheck="false" required /></label>
        <button class="btn-secondary" type="submit" :disabled="registering">{{ registering ? '登记中…' : '校验并登记' }}</button>
      </form>
    </details>

    <section class="section" aria-labelledby="candidate-title">
      <div class="section-header">
        <div><h2 id="candidate-title">候选目录</h2><p>{{ candidates.length }} 个去重表达式</p></div>
      </div>
      <div v-if="!loading && !candidates.length" class="state-panel card"><strong>尚无候选</strong><p>运行 template-v1，或在上方登记一条受限表达式。</p></div>
      <div v-else class="candidate-grid">
        <article v-for="candidate in candidates" :key="candidate.id" class="card candidate-card">
          <div class="candidate-head">
            <div><span class="candidate-source">{{ candidate.spec.source }}</span><h3>{{ candidate.name }}</h3></div>
            <span class="state-pill" :class="factorTone(candidate.status)">{{ factorStatusLabel(candidate.status) }}</span>
          </div>
          <p>{{ candidate.spec.hypothesis }}</p>
          <div class="candidate-facts">
            <span>方向 <strong>{{ candidate.spec.direction > 0 ? '+1' : '-1' }}</strong></span>
            <span>角色 <strong>{{ candidate.spec.role }}</strong></span>
            <span>预热 <strong>{{ candidate.spec.lookback ?? '—' }}</strong></span>
          </div>
          <div class="field-list"><span v-for="field in candidate.spec.required_fields || []" :key="field">{{ field }}</span><span v-if="!(candidate.spec.required_fields || []).length">字段未返回</span></div>
          <small v-if="candidate.rejection_reason" class="reject-reason">未通过：{{ candidate.rejection_reason }}</small>
          <details class="expression-detail"><summary>表达式与指纹</summary><code>{{ shortFactorHash(candidate.expression_hash) }}</code><pre>{{ JSON.stringify(candidate.spec.expression, null, 2) }}</pre></details>
        </article>
      </div>
    </section>

    <section class="section" aria-labelledby="experiment-title">
      <div class="section-header">
        <div><h2 id="experiment-title">实验队列</h2><p>{{ activeExperimentCount ? `${activeExperimentCount} 个等待或运行中` : '当前没有运行中实验' }}</p></div>
      </div>
      <div v-if="!loading && !experiments.length" class="state-panel card"><strong>尚无实验</strong><p>选择候选和冻结数据集后创建训练实验。</p></div>
      <div v-else class="experiment-list">
        <article v-for="experiment in experiments" :key="experiment.id" class="card experiment-card">
          <div class="experiment-head">
            <div>
              <span class="experiment-stage">{{ factorStageLabel(experiment.stage) }} · 第 {{ experiment.attempt }} 次尝试</span>
              <h3>{{ candidateName(experiment.candidate_id) }}</h3>
              <code>{{ experiment.id }}</code>
            </div>
            <span class="state-pill" :class="factorTone(experiment.result?.decision || experiment.status)">{{ factorStatusLabel(experiment.result?.decision || experiment.status) }}</span>
          </div>
          <div class="experiment-meta">
            <span><small>区间</small>{{ experiment.start_date }} → {{ experiment.end_date }}</span>
            <span><small>标签</small>T+1 开盘 · {{ experiment.forward_horizon }} 日</span>
            <span><small>数据集</small><code>{{ experiment.dataset_id }}</code></span>
            <span><small>内容哈希</small><code>{{ shortFactorHash(experiment.data_content_hash) }}</code></span>
          </div>
          <div v-if="experiment.result" class="result-metrics">
            <div><span>覆盖率</span><strong>{{ factorMetric(experiment.result.coverage, 'ratio') }}</strong></div>
            <div><span>方向 IC</span><strong>{{ factorMetric(experiment.result.directional_ic) }}</strong></div>
            <div><span>最大相关</span><strong>{{ factorMetric(experiment.result.maximum_absolute_correlation) }}</strong></div>
            <div><span>Q5−Q1</span><strong>{{ factorMetric(experiment.result.quantile_summary?.directional_top_bottom_spread_mean, 'ratio') }}</strong></div>
          </div>
          <div v-if="experiment.result?.gate_results?.length" class="gate-grid">
            <div v-for="gate in normalizeFactorGates(experiment.result.gate_results)" :key="gate.name" class="gate-item" :class="gate.passed ? 'passed' : 'rejected'">
              <span>{{ factorGateLabel(gate.name) }}</span><strong>{{ factorGateActual(gate) }}</strong><small>{{ gate.passed ? '通过' : '未通过' }} · {{ gate.rule }}</small>
            </div>
          </div>
          <div v-if="experiment.error_code || experiment.error_message" class="experiment-error"><strong>{{ experiment.error_code || '执行失败' }}</strong><span>{{ experiment.error_message }}</span></div>
          <div class="experiment-actions">
            <span v-if="experiment.lease_owner" class="lease">worker: {{ experiment.lease_owner }}</span>
            <button v-if="experiment.status === 'failed'" class="btn-secondary" type="button" :disabled="retryingId === experiment.id" @click="retryExperiment(experiment.id)">{{ retryingId === experiment.id ? '重试中…' : '重新排队' }}</button>
          </div>
        </article>
      </div>
    </section>

    <section class="section" aria-labelledby="memory-title">
      <div class="section-header"><div><h2 id="memory-title">最近训练记忆</h2><p>只含训练证据，不向生成器泄露最终留出详情。</p></div></div>
      <div v-if="memory?.items.length" class="memory-list card">
        <div v-for="item in memory.items.slice(0, 8)" :key="item.experiment_id" class="memory-row">
          <div><strong>{{ item.name || item.candidate_id }}</strong><code>{{ shortFactorHash(item.expression_hash) }}</code></div>
          <span>{{ item.period[0] }} → {{ item.period[1] }}</span>
          <span class="state-pill" :class="factorTone(item.decision)">{{ factorStatusLabel(item.decision) }}</span>
          <small>{{ item.failed_gates.length ? `失败门：${item.failed_gates.map(factorGateLabel).join('、')}` : '未记录失败门' }}</small>
        </div>
      </div>
      <div v-else class="state-panel card compact"><strong>尚无训练记忆</strong><p>worker 完成或记录失败的训练实验后，这里会出现紧凑证据。</p></div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useApi } from '@/composables/useApi'
import type { FactorCandidate, FactorExperiment, FactorMemory, FactorStage } from '@/types/factors'
import {
  factorGateActual,
  factorGateLabel,
  factorMetric,
  factorStageLabel,
  factorStatusLabel,
  factorTone,
  normalizeFactorGates,
  shortFactorHash,
} from '@/utils/factors'
import { apiErrorMessage } from '@/utils/market'
import { validResearchDateRange } from '@/utils/research'

const { api } = useApi()
const candidates = ref<FactorCandidate[]>([])
const experiments = ref<FactorExperiment[]>([])
const memory = ref<FactorMemory | null>(null)
const loading = ref(true)
const generating = ref(false)
const queueing = ref(false)
const registering = ref(false)
const retryingId = ref('')
const error = ref('')
const notice = ref('')
const generationLimit = ref(4)
let disposed = false
let loadSequence = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined

const experimentForm = reactive({
  candidate_id: '',
  dataset_id: 'ashare-inception-2014-v1/normalized-v2',
  start_date: '2015-01-05',
  end_date: '2015-12-31',
  forward_horizon: 5,
  stage: 'training' as FactorStage,
})

const candidateForm = reactive({
  name: 'manual_lagged_return_5',
  hypothesis: '上一完整交易周的价格变化可能包含可检验的截面信息。',
  direction: 1 as 1 | -1,
  role: 'rank',
  source: 'human',
})
const candidateExpression = ref(JSON.stringify({
  op: 'winsorize_zscore',
  args: [{
    op: 'sub',
    args: [{
      op: 'div',
      args: [
        { op: 'delay', args: [{ field: 'close' }], params: { periods: 1 } },
        { op: 'delay', args: [{ field: 'close' }], params: { periods: 6 } },
      ],
    }, { constant: 1 }],
  }],
  params: { lower: 0.01, upper: 0.99 },
}, null, 2))

const activeExperimentCount = computed(() => experiments.value.filter(item => ['queued', 'running'].includes(item.status)).length)

function candidateName(id: string): string {
  return candidates.value.find(candidate => candidate.id === id)?.name || id
}

async function loadAll() {
  const sequence = ++loadSequence
  loading.value = true
  error.value = ''
  if (pollTimer) clearTimeout(pollTimer)
  const results = await Promise.allSettled([
    api.get<{ data: FactorCandidate[] }>('/factor-research/candidates', { params: { limit: 200 } }),
    api.get<{ data: FactorExperiment[] }>('/factor-research/experiments', { params: { limit: 200 } }),
    api.get<FactorMemory>('/factor-research/memory', { params: { limit: 100 } }),
  ])
  if (disposed || sequence !== loadSequence) return
  const [candidateResult, experimentResult, memoryResult] = results
  if (candidateResult.status === 'fulfilled') candidates.value = candidateResult.value.data.data
  if (experimentResult.status === 'fulfilled') experiments.value = experimentResult.value.data.data
  if (memoryResult.status === 'fulfilled') memory.value = memoryResult.value.data
  const rejected = results.find(result => result.status === 'rejected')
  if (rejected?.status === 'rejected') error.value = apiErrorMessage(rejected.reason, '因子研究状态未完整载入，请重试。')
  if (!experimentForm.candidate_id && candidates.value.length) experimentForm.candidate_id = candidates.value[0].id
  loading.value = false
  if (activeExperimentCount.value) pollTimer = setTimeout(loadAll, 5000)
}

async function generateTemplates() {
  generating.value = true
  error.value = ''
  notice.value = ''
  try {
    const { data } = await api.post<{ generated: number; requested: number }>('/factor-research/candidates/generate', { generator: 'template-v1', limit: generationLimit.value })
    notice.value = data.generated ? `已登记 ${data.generated} 个 template-v1 候选。` : '模板候选已全部存在；本轮没有新增表达式。'
    await loadAll()
  } catch (reason) {
    error.value = apiErrorMessage(reason, '候选生成失败，请重试。')
  } finally {
    generating.value = false
  }
}

async function registerCandidate() {
  registering.value = true
  error.value = ''
  notice.value = ''
  try {
    let expression: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(candidateExpression.value)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid')
      expression = parsed as Record<string, unknown>
    } catch {
      error.value = '表达式必须是有效的 JSON 对象。'
      return
    }
    const { data } = await api.post<{ created: boolean; candidate: FactorCandidate }>('/factor-research/candidates', { ...candidateForm, expression, protocol_version: '1.0' })
    notice.value = data.created ? `候选 ${data.candidate.name} 已登记。` : '相同表达式已存在，已复用原候选。'
    experimentForm.candidate_id = data.candidate.id
    await loadAll()
  } catch (reason) {
    error.value = apiErrorMessage(reason, '候选登记失败，请检查表达式约束。')
  } finally {
    registering.value = false
  }
}

async function queueExperiment() {
  error.value = ''
  notice.value = ''
  if (!experimentForm.candidate_id || !experimentForm.dataset_id) {
    error.value = '请选择候选并填写冻结数据集 ID。'
    return
  }
  if (!validResearchDateRange(experimentForm.start_date, experimentForm.end_date)) {
    error.value = '实验日期无效、顺序错误或跨度超过十年。'
    return
  }
  queueing.value = true
  try {
    const { data } = await api.post<{ created: boolean; experiment: FactorExperiment }>('/factor-research/experiments', { ...experimentForm, evaluation_policy: {} })
    notice.value = data.created ? `实验 ${data.experiment.id} 已加入队列。` : `相同实验已存在：${data.experiment.id}`
    await loadAll()
  } catch (reason) {
    error.value = apiErrorMessage(reason, '实验创建失败，请检查数据集、阶段前置条件与日期。')
  } finally {
    queueing.value = false
  }
}

async function retryExperiment(id: string) {
  retryingId.value = id
  error.value = ''
  notice.value = ''
  try {
    await api.post(`/factor-research/experiments/${id}/retry`)
    notice.value = `实验 ${id} 已重新排队。`
    await loadAll()
  } catch (reason) {
    error.value = apiErrorMessage(reason, '实验重试失败。')
  } finally {
    retryingId.value = ''
  }
}

onMounted(() => { void loadAll() })
onBeforeUnmount(() => { disposed = true; loadSequence += 1; if (pollTimer) clearTimeout(pollTimer) })
</script>

<style scoped>
.factor-page { overflow: hidden; }
.page-kicker { margin-bottom: 6px; color: #61d7df; font-size: 10px; font-weight: 760; letter-spacing: .16em; }
.boundary-card { display: flex; gap: 13px; align-items: flex-start; border: 1px solid rgba(228,168,58,.32); border-radius: var(--radius-md); background: rgba(228,168,58,.07); padding: 15px 17px; }
.boundary-mark { display: grid; width: 27px; height: 27px; flex: 0 0 auto; place-items: center; border: 1px solid rgba(228,168,58,.55); border-radius: 50%; color: #edbd62; font-weight: 750; }
.boundary-card strong { color: #f1c774; font-size: 13px; }.boundary-card p { margin-top: 4px; color: var(--text-secondary); font-size: 11px; line-height: 1.6; }
.memory-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); margin-top: 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-elevated); }
.memory-card { min-width: 0; padding: 16px 18px; border-right: 1px solid var(--border); }.memory-card:last-child { border-right: 0; }.memory-card span,.memory-card small { display: block; color: var(--text-tertiary); font-size: 10px; }.memory-card strong { display: block; margin: 4px 0; font-size: 24px; }.memory-card.good strong { color: #69cca4; }.memory-card.bad strong { color: #f78a90; }.memory-card.warn strong { color: #edbd62; }
.page-error,.page-notice { margin-top: 12px; border-radius: 6px; padding: 9px 12px; font-size: 12px; }.page-error { background: var(--up-muted); color: #f2a1a5; }.page-notice { background: var(--down-muted); color: #79d4ad; }
.workbench-grid { display: grid; grid-template-columns: .8fr 1.2fr; gap: 12px; margin-top: 22px; }.action-card { min-width: 0; }.compact { align-items: flex-start; }.compact h2 { font-size: 16px; }.compact p { margin-top: 3px; color: var(--text-secondary); font-size: 11px; }.mode-pill { flex: 0 0 auto; border: 1px solid rgba(97,215,223,.25); border-radius: 999px; background: rgba(97,215,223,.07); color: #9de8eb; padding: 3px 8px; font-size: 9px; }.mode-pill.queue { border-color: rgba(77,141,255,.3); background: var(--accent-muted); color: #8ab4ff; }
.inline-action { display: flex; align-items: flex-end; gap: 10px; margin-top: 18px; }.inline-action label,.experiment-form label,.candidate-form label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 11px; }.inline-action select { min-width: 100px; }.action-footnote { display: block; margin-top: 11px; color: var(--text-tertiary); font-size: 10px; line-height: 1.55; }
.experiment-form { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; margin-top: 15px; }.wide { grid-column: 1/-1; }.experiment-form input,.experiment-form select,.candidate-form input,.candidate-form select,.candidate-form textarea { width: 100%; min-width: 0; }.experiment-form .btn-primary { margin-top: 3px; }
.manual-register { margin-top: 12px; }.manual-register summary { color: var(--text-secondary); font-size: 12px; cursor: pointer; }.manual-register[open] summary { margin-bottom: 16px; color: var(--text-primary); }.candidate-form { display: grid; grid-template-columns: 1.3fr .55fr .55fr .8fr; gap: 10px; }.candidate-form textarea { resize: vertical; font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; }.candidate-form .btn-secondary { justify-self: start; }
.section-header>div>p { margin-top: 3px; color: var(--text-tertiary); font-size: 11px; }.candidate-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }.candidate-card { min-width: 0; }.candidate-head,.experiment-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.candidate-source,.experiment-stage { color: #61d7df; font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }.candidate-card h3,.experiment-card h3 { margin-top: 4px; font-size: 14px; }.candidate-card>p { min-height: 42px; margin-top: 11px; color: var(--text-secondary); font-size: 11px; line-height: 1.65; }
.state-pill { display: inline-flex; flex: 0 0 auto; align-items: center; border: 1px solid var(--border); border-radius: 999px; background: var(--bg-secondary); color: var(--text-secondary); padding: 3px 8px; font-size: 10px; white-space: nowrap; }.state-pill.good { border-color: rgba(54,179,126,.3); background: var(--down-muted); color: #69cca4; }.state-pill.bad { border-color: rgba(239,91,100,.3); background: var(--up-muted); color: #f78a90; }.state-pill.warn { border-color: rgba(228,168,58,.3); background: rgba(228,168,58,.09); color: #edbd62; }
.candidate-facts { display: grid; grid-template-columns: repeat(3,1fr); margin-top: 13px; border-block: 1px solid var(--border); }.candidate-facts span { padding: 8px 4px; color: var(--text-tertiary); font-size: 10px; }.candidate-facts strong { display: block; margin-top: 2px; color: var(--text-primary); font-size: 11px; }.field-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 11px; }.field-list span { border-radius: 4px; background: var(--bg-secondary); color: var(--text-secondary); padding: 3px 6px; font-size: 9px; }.reject-reason { display: block; margin-top: 10px; color: #f2a1a5; }.expression-detail { margin-top: 12px; color: var(--text-tertiary); font-size: 10px; }.expression-detail summary { cursor: pointer; }.expression-detail code { display: block; margin-top: 8px; }.expression-detail pre { max-height: 260px; overflow: auto; margin-top: 8px; border-radius: 5px; background: var(--bg-primary); color: var(--text-secondary); padding: 10px; font-size: 9px; }
.experiment-list { display: grid; gap: 12px; }.experiment-card { min-width: 0; }.experiment-head code { display: block; margin-top: 4px; color: var(--text-tertiary); font-size: 9px; }.experiment-meta { display: grid; grid-template-columns: 1fr 1fr 1.4fr 1fr; gap: 10px; margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }.experiment-meta span { min-width: 0; color: var(--text-secondary); font-size: 10px; }.experiment-meta small { display: block; margin-bottom: 3px; color: var(--text-tertiary); }.experiment-meta code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-metrics { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); margin-top: 13px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-primary); }.result-metrics div { padding: 10px 12px; border-right: 1px solid var(--border); }.result-metrics div:last-child { border-right: 0; }.result-metrics span { display: block; color: var(--text-tertiary); font-size: 9px; }.result-metrics strong { display: block; margin-top: 3px; font-size: 14px; }
.gate-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 7px; margin-top: 12px; }.gate-item { min-width: 0; border-left: 2px solid var(--border-strong); border-radius: 4px; background: var(--bg-secondary); padding: 8px 10px; }.gate-item.passed { border-left-color: var(--success); }.gate-item.rejected { border-left-color: var(--danger); }.gate-item span,.gate-item small { display: block; overflow: hidden; color: var(--text-tertiary); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }.gate-item strong { display: block; margin: 3px 0; font-size: 12px; }.gate-item.passed small { color: #69cca4; }.gate-item.rejected small { color: #f78a90; }
.experiment-error { display: grid; gap: 3px; margin-top: 12px; border-radius: 5px; background: var(--up-muted); color: #f2a1a5; padding: 9px 11px; font-size: 10px; }.experiment-error span { overflow-wrap: anywhere; }.experiment-actions { display: flex; min-height: 34px; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 10px; }.lease { margin-right: auto; color: var(--text-tertiary); font-size: 9px; }
.memory-list { padding-block: 4px; }.memory-row { display: grid; grid-template-columns: minmax(180px,1.2fr) 1fr auto minmax(170px,1fr); gap: 12px; align-items: center; border-bottom: 1px solid var(--border); padding: 11px 0; }.memory-row:last-child { border-bottom: 0; }.memory-row>div { min-width: 0; }.memory-row strong,.memory-row code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.memory-row strong { font-size: 11px; }.memory-row code,.memory-row span,.memory-row small { color: var(--text-tertiary); font-size: 9px; }.state-panel.compact { min-height: 120px; }
@media (max-width: 1080px) { .workbench-grid { grid-template-columns: 1fr; }.candidate-form { grid-template-columns: 1fr 1fr; }.candidate-grid { grid-template-columns: 1fr; }.experiment-meta { grid-template-columns: 1fr 1fr; }.gate-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }.memory-row { grid-template-columns: 1fr auto; }.memory-row>span:not(.state-pill),.memory-row>small { grid-column: 1/-1; } }
@media (max-width: 720px) { .memory-grid { grid-template-columns: 1fr 1fr; }.memory-card:nth-child(2) { border-right: 0; }.memory-card:nth-child(-n+2) { border-bottom: 1px solid var(--border); }.candidate-form,.experiment-form { grid-template-columns: minmax(0,1fr); }.wide { grid-column: auto; }.result-metrics { grid-template-columns: 1fr 1fr; }.result-metrics div:nth-child(2) { border-right: 0; }.result-metrics div:nth-child(-n+2) { border-bottom: 1px solid var(--border); }.gate-grid { grid-template-columns: 1fr; } }
@media (max-width: 390px) { .memory-card { padding: 13px 11px; }.memory-card strong { font-size: 21px; }.inline-action { align-items: stretch; flex-direction: column; }.experiment-meta { grid-template-columns: 1fr; } }
</style>
