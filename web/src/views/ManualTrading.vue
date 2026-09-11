<template>
  <div class="page manual-page">
    <div class="page-header">
      <div>
        <h1>人工执行</h1>
        <p class="page-note">A 股日频人工执行闭环：系统生成和展示计划，用户在券商侧操作后回填成交事实。</p>
      </div>
      <div class="manual-header-actions"><router-link class="btn-secondary" to="/daily-workflow">进入日频研究闭环 ↗</router-link><button class="btn-secondary" type="button" :disabled="loading" @click="refreshAll">{{ loading ? '刷新中...' : '刷新账户' }}</button></div>
    </div>

    <section class="manual-guardrail" aria-labelledby="manual-guardrail-title">
      <div class="guardrail-mark" aria-hidden="true">⛨</div>
      <div>
        <h2 id="manual-guardrail-title">人工执行 · 用户回填 · 实盘委托已阻断</h2>
        <p>本页不连接券商、不保存券商密钥、不提交订单。所有成交和资金事件都必须由用户在券商侧完成后手工记录。</p>
      </div>
      <span class="guardrail-pill">paper_only / no broker API</span>
    </section>

    <p v-if="errorMessage" class="page-status error" role="alert">{{ errorMessage }}</p>
    <p v-if="notice" class="page-status" role="status">{{ notice }}</p>

    <section class="card account-section" aria-labelledby="manual-account-title">
      <div class="section-header compact">
        <div><h2 id="manual-account-title">人工账户</h2><p>只记录账户快照和用户报告的事实，不代表券商连接状态。</p></div>
        <span class="manual-badge">人工台账</span>
      </div>
      <div class="account-toolbar">
        <label class="account-select">当前账户
          <select v-model="selectedAccountId" :disabled="!accounts.length" @change="loadAccountData">
            <option value="" disabled>请选择账户</option>
            <option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }}{{ account.broker_label ? ` · ${account.broker_label}` : '' }}</option>
          </select>
        </label>
        <span v-if="selectedAccount" class="account-status" :class="`is-${selectedAccount.status}`">{{ accountStatus(selectedAccount.status) }}</span>
      </div>
      <form class="form-grid account-create" @submit.prevent="createAccount">
        <label>账户名称<input v-model="accountForm.name" required maxlength="120" placeholder="例如：我的 A 股账户" /></label>
        <label>券商备注<input v-model="accountForm.broker_label" maxlength="120" placeholder="只记录名称，不填密钥" /></label>
        <button class="btn-secondary" type="submit" :disabled="saving">创建人工账户</button>
      </form>
      <div v-if="selectedAccount" class="account-metrics">
        <div><span>已确认现金</span><strong>¥{{ decimalText(selectedAccount.confirmed_cash) }}</strong></div>
        <div><span>台账序列</span><strong class="mono">{{ selectedAccount.ledger_checkpoint_hash ? selectedAccount.ledger_checkpoint_hash.slice(0, 12) : '—' }}</strong></div>
        <div><span>券商状态</span><strong>未连接</strong></div>
      </div>
      <p v-else class="empty-note">还没有人工账户。创建账户后，先记录开户或当前可确认现金。</p>
    </section>

    <template v-if="selectedAccount">
      <section class="two-column">
        <article class="card" aria-labelledby="cash-event-title">
          <div class="section-header compact"><div><h2 id="cash-event-title">记录资金事实</h2><p>用于开户余额、入金、出金或费用调整。</p></div></div>
          <form class="stack-form" @submit.prevent="recordCash">
            <label>事件类型<select v-model="cashForm.event_type"><option value="opening_balance">开户/当前余额</option><option value="deposit">入金</option><option value="withdrawal">出金</option><option value="interest">利息</option><option value="fee_adjustment">费用调整</option></select></label>
            <label>金额<input v-model="cashForm.amount" type="number" step="0.01" required placeholder="出金请填负数" /></label>
            <label>发生时间<input v-model="cashForm.occurred_at" type="datetime-local" required /></label>
            <label>备注<textarea v-model="cashForm.note" rows="2" maxlength="500" placeholder="可选：对应券商流水或截图编号" /></label>
            <button class="btn-accent" type="submit" :disabled="saving">保存用户报告的资金事实</button>
          </form>
        </article>

        <article class="card" aria-labelledby="fill-event-title">
          <div class="section-header compact"><div><h2 id="fill-event-title">回填计划内成交</h2><p>只允许选择已查看并逐项确认的执行计划；计划外成交必须先进入对账。</p></div></div>
          <form class="stack-form" @submit.prevent="recordFill">
            <label>已确认计划项<select v-model="fillForm.selection" required><option value="" disabled>请先在下方计划中确认执行项</option><option v-for="entry in confirmedPlanItems" :key="entry.key" :value="entry.key">{{ entry.plan.execution_date }} · {{ entry.item.code }} · {{ entry.item.side === 'buy' ? '买入' : '卖出' }} · 计划 {{ decimalText(entry.item.planned_quantity) }} 股</option></select></label>
            <div class="form-grid"><label>成交数量<input v-model="fillForm.quantity" type="number" min="1" step="1" required /></label><label>成交价格<input v-model="fillForm.price" type="number" min="0.00000001" step="0.00000001" required /></label></div>
            <div class="form-grid"><label>佣金<input v-model="fillForm.commission" type="number" min="0" step="0.01" /></label><label>印花税<input v-model="fillForm.stamp_duty" type="number" min="0" step="0.01" /></label></div>
            <label>成交时间<input v-model="fillForm.traded_at" type="datetime-local" required /></label>
            <button class="btn-accent" type="submit" :disabled="saving || selectedAccount.status !== 'active' || !selectedFillEntry">保存计划绑定的用户成交</button>
            <small v-if="selectedAccount.status !== 'active'" class="hint warning">账户必须有非负现金且处于 active 状态；当前状态：{{ accountStatus(selectedAccount.status) }}。</small>
            <small class="hint">买入会校验 A 股交易日和 T+1 约束；日历不可用时系统会阻断，不会按休市处理。</small>
          </form>
        </article>
      </section>

      <section class="card state-section" aria-labelledby="state-title">
        <div class="section-header compact"><div><h2 id="state-title">人工影子台账</h2><p>由已记录的资金和成交事实重建；“对账异常”时禁止继续执行。</p></div><span class="manual-badge">只读汇总</span></div>
        <div class="state-summary"><div><span>确认现金</span><strong>¥{{ decimalText(state?.cash) }}</strong></div><div><span>持仓代码</span><strong>{{ Object.keys(state?.positions || {}).length }}</strong></div><div><span>台账校验</span><strong :class="{ 'text-warning': selectedAccount.status === 'reconcile' }">{{ selectedAccount.status === 'reconcile' ? '需对账' : '已记录' }}</strong></div></div>
        <div v-if="Object.keys(state?.positions || {}).length" class="data-table-wrap"><table class="data-table"><thead><tr><th>代码</th><th>总数量</th><th>可卖数量</th></tr></thead><tbody><tr v-for="(position, code) in state?.positions" :key="code"><td class="mono">{{ code }}</td><td>{{ decimalText(position.quantity) }}</td><td>{{ decimalText(position.available_quantity) }}</td></tr></tbody></table></div>
        <p v-else class="empty-note">暂无持仓事实。</p>
      </section>

      <section class="card reconcile-section" aria-labelledby="reconcile-title">
        <div class="section-header compact"><div><h2 id="reconcile-title">收盘对账</h2><p>把券商账户当日看到的现金、总资产和持仓快照回填；差异会进入 reconcile 状态。</p></div><span class="manual-badge">失败关闭</span></div>
        <form class="form-grid" @submit.prevent="reconcile">
          <label>券商现金<input v-model="reconcileForm.cash" type="number" min="0" step="0.01" required /></label>
          <label>券商总资产<input v-model="reconcileForm.total_asset" type="number" min="0" step="0.01" required /></label>
          <label class="wide">持仓 JSON（可选）<textarea v-model="reconcileForm.positions_json" rows="2" placeholder='[{"code":"000001.SZ","total_quantity":100,"available_quantity":100,"avg_cost":10,"market_value":1000}]' /></label>
          <button class="btn-secondary" type="submit" :disabled="saving">保存对账快照</button>
        </form>
      </section>

      <section class="card review-section" aria-labelledby="review-title">
        <div class="section-header compact"><div><h2 id="review-title">日终估值与复盘</h2><p>用户回填收盘资产后生成现金流中和收益、执行偏差和数据健康状态。</p></div><span class="manual-badge">研究修订独立排队</span></div>
        <div class="two-column review-forms">
          <form class="stack-form" @submit.prevent="recordValuation">
            <strong class="form-title">记录用户报告的估值</strong>
            <label>估值日期<input v-model="valuationForm.valuation_date" type="date" required /></label>
            <div class="form-grid"><label>现金<input v-model="valuationForm.cash" type="number" min="0" step="0.01" required /></label><label>持仓市值<input v-model="valuationForm.market_value" type="number" min="0" step="0.01" required /></label></div>
            <label>总资产<input v-model="valuationForm.total_asset" type="number" min="0" step="0.01" required /></label>
            <label>价格/截图时间<input v-model="valuationForm.price_as_of" maxlength="40" placeholder="可选：券商收盘截图时间" /></label>
            <button class="btn-secondary" type="submit" :disabled="saving">保存估值快照</button>
          </form>
          <form class="stack-form" @submit.prevent="createReview">
            <strong class="form-title">生成日终复盘</strong>
            <label>复盘日期<input v-model="reviewForm.review_date" type="date" required /></label>
            <label>备注<textarea v-model="reviewForm.notes" rows="4" maxlength="1000" placeholder="记录未成交、滑点、数据异常或人工原因" /></label>
            <button class="btn-secondary" type="submit" :disabled="saving || !valuations.length">生成复盘报告</button>
            <small v-if="!valuations.length" class="hint warning">请先保存对应日期的估值快照。</small>
          </form>
        </div>
        <div v-if="reviews.length" class="review-list"><article v-for="review in reviews.slice(0, 10)" :key="review.id" class="review-row"><div><strong>{{ review.review_date }} · {{ review.status === 'ready' ? '可用' : '已阻断' }}</strong><span>对账：{{ review.reconciliation_status }} · 计划 {{ review.planned_item_count }} 项 · 回填 {{ review.reported_fill_count }} 笔</span></div><span class="mono">偏差 {{ decimalText(review.execution_deviation) }}</span></article></div><p v-else class="empty-note">暂无日终复盘。</p>
        <div v-if="valuations.length" class="valuation-list"><div v-for="valuation in valuations.slice(0, 5)" :key="valuation.id"><span>{{ valuation.valuation_date }} · 总资产 ¥{{ decimalText(valuation.total_asset) }}</span><strong :class="Number(valuation.daily_return) >= 0 ? 'positive' : 'negative'">收益 {{ decimalText(valuation.daily_return) }}</strong></div></div>
      </section>

      <section class="card plan-section" aria-labelledby="plan-title">
        <div class="section-header compact"><div><h2 id="plan-title">人工执行计划</h2><p>计划是可读执行清单，不是订单；必须由人工逐项完成并回填。</p></div><span class="manual-badge">无提交按钮</span></div>
        <div v-if="plans.length" class="plan-list"><article v-for="plan in plans" :key="plan.id" class="plan-row"><div class="plan-head"><div><strong>{{ plan.execution_date }} · {{ plan.execution_session === 'open' ? '开盘' : '收盘' }} · {{ plan.plan_type }}</strong><span>版本 {{ plan.id.slice(-8) }} · {{ planStatus(plan.status) }}</span></div><button v-if="plan.status === 'ready'" class="link-button" type="button" @click="viewPlan(plan.id)">标记已查看</button></div><p v-if="plan.blocked_reason" class="hint warning">阻断原因：{{ plan.blocked_reason }}</p><div v-if="plan.items.length" class="data-table-wrap"><table class="data-table compact-table"><thead><tr><th>阶段</th><th>代码</th><th>方向</th><th>数量</th><th>参考价</th><th>状态</th><th>人工确认</th></tr></thead><tbody><tr v-for="item in plan.items" :key="item.id"><td>{{ item.side === 'sell' ? '先卖' : '后买' }}</td><td class="mono">{{ item.code }}</td><td>{{ item.side === 'buy' ? '买入' : '卖出' }}</td><td>{{ decimalText(item.planned_quantity) }}</td><td>¥{{ decimalText(item.reference_price) }}</td><td>{{ planStatus(item.status) }}</td><td><span v-if="item.confirmed">已确认</span><button v-else-if="['viewed', 'partially_filled'].includes(plan.status) && ['planned', 'submitted', 'partially_filled'].includes(item.status)" class="link-button" type="button" @click="confirmItem(plan.id, item.id)">确认此项</button><span v-else>—</span></td></tr></tbody></table></div><p v-else class="empty-note">该计划没有可执行条目。</p></article></div>
        <p v-else class="empty-note">暂无已持久化的人工执行计划。计划需要先经过策略发布、授权、决策和数据就绪检查。</p>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useApi } from '@/composables/useApi'
import type { ManualAccount, ManualPlan, ManualPlanItem, ManualReview, ManualState, ManualValuation } from '@/types/manual'
import { decimalText, manualError, newIdempotencyKey, unwrapManual } from '@/utils/manual'

const { api } = useApi()
const route = useRoute()
const loading = ref(false)
const saving = ref(false)
const notice = ref('')
const errorMessage = ref('')
const accounts = ref<ManualAccount[]>([])
const selectedAccountId = ref('')
const state = ref<ManualState | null>(null)
const plans = ref<ManualPlan[]>([])
const valuations = ref<ManualValuation[]>([])
const reviews = ref<ManualReview[]>([])
const selectedAccount = computed(() => accounts.value.find(account => account.id === selectedAccountId.value) ?? null)
const confirmedPlanItems = computed(() => plans.value.flatMap(plan => plan.items.filter(item => item.confirmed && ['planned', 'submitted', 'partially_filled'].includes(item.status)).map(item => ({ key: `${plan.id}|${item.id}`, plan, item }))))
const selectedFillEntry = computed<{ plan: ManualPlan, item: ManualPlanItem } | null>(() => {
  const entry = confirmedPlanItems.value.find(value => value.key === fillForm.selection)
  return entry ? { plan: entry.plan, item: entry.item } : null
})
function localDate(value = new Date()) { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}` }
function localDateTime(value = new Date()) { return `${localDate(value)}T${String(value.getHours()).padStart(2, '0')}:${String(value.getMinutes()).padStart(2, '0')}` }
const accountForm = reactive({ name: '', broker_label: '' })
const cashForm = reactive({ event_type: 'opening_balance', amount: '', occurred_at: localDateTime(), note: '' })
const fillForm = reactive({ selection: '', quantity: '100', price: '', commission: '0', stamp_duty: '0', traded_at: localDateTime() })
const reconcileForm = reactive({ cash: '', total_asset: '', positions_json: '' })
const valuationForm = reactive({ valuation_date: localDate(), cash: '', market_value: '0', total_asset: '', price_as_of: '' })
const reviewForm = reactive({ review_date: localDate(), notes: '' })

function clearMessage() { notice.value = ''; errorMessage.value = '' }
function accountStatus(value: string) { return ({ draft: '待开户余额', active: '可记录', reconcile: '对账异常', suspended: '已暂停', closed: '已关闭' } as Record<string, string>)[value] ?? value }
function planStatus(value: string) { return ({ draft: '草稿', ready: '待查看', viewed: '已查看', partially_filled: '部分完成', completed: '已完成', blocked: '已阻断', expired: '已过期', cancelled: '已取消' } as Record<string, string>)[value] ?? value }

async function refreshAll() {
  loading.value = true
  clearMessage()
  try {
    const response = await api.get('/manual-trading/accounts')
    accounts.value = unwrapManual<ManualAccount[]>(response.data)
    const routeAccount = typeof route.query.account_id === 'string' ? route.query.account_id : ''
    if (!selectedAccountId.value || !accounts.value.some(account => account.id === selectedAccountId.value)) selectedAccountId.value = accounts.value.some(account => account.id === routeAccount) ? routeAccount : accounts.value[0]?.id ?? ''
    if (selectedAccountId.value) await loadAccountData()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { loading.value = false }
}

async function loadAccountData() {
  if (!selectedAccountId.value) return
  try {
    const [stateResponse, plansResponse, valuationsResponse, reviewsResponse] = await Promise.all([
      api.get(`/manual-trading/accounts/${selectedAccountId.value}/state`),
      api.get(`/manual-trading/accounts/${selectedAccountId.value}/plans`),
      api.get(`/manual-trading/accounts/${selectedAccountId.value}/valuations`),
      api.get(`/manual-trading/accounts/${selectedAccountId.value}/reviews`),
    ])
    state.value = unwrapManual<ManualState>(stateResponse.data)
    plans.value = unwrapManual<ManualPlan[]>(plansResponse.data)
    valuations.value = unwrapManual<ManualValuation[]>(valuationsResponse.data)
    reviews.value = unwrapManual<ManualReview[]>(reviewsResponse.data)
  } catch (error: any) { errorMessage.value = manualError(error) }
}

async function createAccount() {
  saving.value = true; clearMessage()
  try {
    const response = await api.post('/manual-trading/accounts', accountForm, { headers: { 'Idempotency-Key': newIdempotencyKey('manual-account') } })
    const created = unwrapManual<ManualAccount>(response.data)
    accountForm.name = ''; accountForm.broker_label = ''; selectedAccountId.value = created.id
    notice.value = '人工账户已创建；请先记录可确认现金。'
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function recordCash() {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try {
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/cash-events`, { ...cashForm, amount: cashForm.amount, occurred_at: new Date(cashForm.occurred_at).toISOString() }, { headers: { 'Idempotency-Key': newIdempotencyKey('manual-cash') } })
    notice.value = '资金事实已保存到人工台账。'
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function recordFill() {
  if (!selectedAccountId.value || !selectedFillEntry.value) return
  saving.value = true; clearMessage()
  try {
    const { plan, item } = selectedFillEntry.value
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/plans/${plan.id}/items/${item.id}/fill`, { client_event_id: newIdempotencyKey('manual-fill'), quantity: fillForm.quantity, price: fillForm.price, commission: fillForm.commission, stamp_duty: fillForm.stamp_duty, traded_at: new Date(fillForm.traded_at).toISOString() })
    notice.value = '用户报告的成交已保存；系统未向券商发送任何委托。'
    fillForm.selection = ''
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function confirmItem(planId: string, itemId: string) {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try {
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/plans/${planId}/items/${itemId}/confirm`, { actor: 'local-user' })
    notice.value = '计划项已确认；请仅在券商侧完成后回填实际成交。'
    await loadAccountData()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function reconcile() {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try {
    let positions: unknown[] = []
    if (reconcileForm.positions_json.trim()) {
      const parsed = JSON.parse(reconcileForm.positions_json)
      if (!Array.isArray(parsed)) throw new Error('持仓 JSON 必须是数组。')
      positions = parsed
    }
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/reconcile`, { idempotency_key: newIdempotencyKey('manual-reconcile'), as_of: new Date().toISOString(), cash: reconcileForm.cash, total_asset: reconcileForm.total_asset, positions })
    notice.value = '对账快照已保存；如有差异，账户会自动进入对账阻断状态。'
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function recordValuation() {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try {
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/valuations`, { ...valuationForm, idempotency_key: newIdempotencyKey('manual-valuation') })
    notice.value = '日终估值已保存；收益率已按外部现金流中和计算。'
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function createReview() {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try {
    await api.post(`/manual-trading/accounts/${selectedAccountId.value}/reviews`, reviewForm)
    notice.value = '日终复盘已生成；因子衰减只会进入研究队列，不改变人工授权。'
    await refreshAll()
  } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

async function viewPlan(planId: string) {
  if (!selectedAccountId.value) return
  saving.value = true; clearMessage()
  try { await api.post(`/manual-trading/accounts/${selectedAccountId.value}/plans/${planId}/view`); notice.value = '计划已标记为已查看。'; await loadAccountData() } catch (error: any) { errorMessage.value = manualError(error) } finally { saving.value = false }
}

onMounted(() => { void refreshAll() })
</script>

<style scoped>
.page-note { max-width: 700px; margin-top: 6px; color: var(--text-secondary); font-size: 13px; }
.manual-header-actions { display: flex; align-items: center; gap: 8px; }
.manual-guardrail { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; border: 1px solid rgba(228, 168, 58, .42); border-radius: var(--radius-md); background: rgba(228, 168, 58, .08); padding: 16px 18px; }
.guardrail-mark { color: var(--warning); font-size: 24px; }
.manual-guardrail h2 { font-size: 15px; }
.manual-guardrail p { margin-top: 3px; color: var(--text-secondary); font-size: 12px; }
.guardrail-pill, .manual-badge, .account-status { display: inline-flex; align-items: center; border: 1px solid var(--border-strong); border-radius: 999px; color: var(--text-secondary); padding: 4px 9px; font-size: 11px; white-space: nowrap; }
.guardrail-pill { margin-left: auto; border-color: rgba(228, 168, 58, .42); color: #edbd62; }
.card { margin-bottom: 18px; }
.compact p { margin-top: 3px; color: var(--text-secondary); font-size: 12px; }
.account-toolbar { display: flex; align-items: end; gap: 12px; margin-bottom: 14px; }
.account-select { display: grid; flex: 1; max-width: 560px; gap: 5px; color: var(--text-secondary); font-size: 12px; }
.account-status.is-active { border-color: rgba(54, 179, 126, .45); color: var(--success); }
.account-status.is-reconcile { border-color: rgba(228, 168, 58, .45); color: var(--warning); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.form-grid label, .stack-form label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; }
.form-grid .wide { grid-column: 1 / -1; }
.account-create { align-items: end; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; }
.account-metrics, .state-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 16px; }
.account-metrics div, .state-summary div { display: grid; gap: 4px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); padding: 11px 12px; }
.account-metrics span, .state-summary span { color: var(--text-secondary); font-size: 11px; }
.account-metrics strong, .state-summary strong { font-size: 16px; }
.two-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.two-column .card { min-width: 0; }
.stack-form { display: grid; gap: 12px; }
.stack-form textarea { resize: vertical; }
.hint { color: var(--text-tertiary); font-size: 11px; }
.warning, .text-warning { color: var(--warning); }
.error { border-color: rgba(239, 91, 100, .35); color: var(--danger); }
.state-section .data-table-wrap, .plan-section .data-table-wrap { margin-top: 14px; }
.mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
.plan-list { display: grid; gap: 12px; }
.plan-row { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-secondary); padding: 13px; }
.plan-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.plan-head div { display: grid; gap: 3px; }
.plan-head span { color: var(--text-secondary); font-size: 11px; }
.compact-table th, .compact-table td { padding: 8px 9px; }
.empty-note { color: var(--text-secondary); font-size: 12px; }
.form-title { color: var(--text-primary); font-size: 13px; }
.review-list, .valuation-list { display: grid; gap: 8px; margin-top: 16px; }
.review-row, .valuation-list > div { display: flex; align-items: center; justify-content: space-between; gap: 12px; border-top: 1px solid var(--border); padding-top: 10px; font-size: 12px; }
.review-row div { display: grid; gap: 3px; }
.review-row span, .valuation-list span { color: var(--text-secondary); }
.page-status { margin: 0 0 16px; color: var(--success); font-size: 13px; }
.link-button { background: transparent; color: var(--accent); font-size: 12px; }
.link-button:hover { color: var(--accent-hover); }
@media (max-width: 900px) { .two-column { grid-template-columns: 1fr; gap: 0; } }
@media (max-width: 640px) {
  .manual-header-actions { align-items: stretch; flex-direction: column; }
  .manual-guardrail { align-items: flex-start; flex-wrap: wrap; }
  .guardrail-pill { margin-left: 38px; }
  .account-create, .form-grid { grid-template-columns: 1fr; }
  .form-grid .wide { grid-column: auto; }
  .account-toolbar { align-items: stretch; flex-direction: column; }
  .account-select { max-width: none; }
  .account-metrics, .state-summary { grid-template-columns: 1fr; }
  .plan-head { align-items: flex-start; flex-direction: column; }
}
</style>
