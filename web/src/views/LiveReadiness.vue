<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>实盘准备</h1>
        <p class="page-note">Phase 3A 只准备连接、风控和订单草案；当前不会向任何真实券商发送委托。</p>
      </div>
      <button class="btn-secondary" type="button" @click="refreshAll" :disabled="loading">{{ loading ? '刷新中...' : '刷新状态' }}</button>
    </div>

    <section class="card safety-banner" :class="{ safe: !capabilities.can_submit_live }" aria-labelledby="live-safety-title">
      <div class="safety-icon" aria-hidden="true">!</div>
      <div class="safety-copy">
        <h2 id="live-safety-title">{{ capabilities.can_submit_live ? '实盘条件已满足' : '实盘委托已阻断' }}</h2>
        <p v-if="capabilities.can_submit_live">仍需在执行前逐笔确认订单。</p>
        <p v-else>{{ blockedReasonsText }}</p>
      </div>
      <div class="safety-actions">
        <span class="pill" :class="capabilities.kill_switch_active ? 'pill-safe' : 'pill-warning'">
          {{ capabilities.kill_switch_active ? 'Kill Switch 已开启' : 'Kill Switch 未开启' }}
        </span>
        <button v-if="!capabilities.kill_switch_active" class="btn-danger" type="button" @click="lockLive">重新锁定实盘</button>
      </div>
    </section>

    <div class="two-column">
      <section class="card" aria-labelledby="connection-title">
        <div class="section-header compact">
          <div><h2 id="connection-title">券商连接登记</h2><p>只登记连接信息，不保存 API 密钥本身。</p></div>
        </div>
        <form class="stack-form" @submit.prevent="createConnection">
          <label>券商标识<input v-model="connectionForm.provider" placeholder="例如 ibkr" pattern="[a-z0-9][a-z0-9_.-]{1,79}" required /></label>
          <label>账户标识<input v-model="connectionForm.account_ref" placeholder="例如 sandbox-account" required /></label>
          <label>模式<select v-model="connectionForm.mode"><option value="sandbox">官方沙盒</option><option value="live">真实账户（默认仍阻断）</option></select></label>
          <label>凭证引用（可选）<input v-model="connectionForm.credential_ref" placeholder="env:IBKR_API_KEY" /><small>只能填写 env: 或 keychain: 引用，不能填写 secret。</small></label>
          <button class="btn-secondary" type="submit" :disabled="saving">{{ saving ? '登记中...' : '登记连接' }}</button>
        </form>
        <div v-if="connections.length" class="connection-list">
          <article v-for="connection in connections" :key="connection.id" class="connection-row">
            <div><strong>{{ connection.provider }}</strong><span>{{ connection.account_ref }} · {{ connection.mode }}</span></div>
            <div class="connection-actions"><span class="status-text" :class="connection.status">{{ connectionStatus(connection.status) }}</span><button class="link-button" type="button" @click="probeConnection(connection.id)">测试</button></div>
          </article>
        </div>
        <p v-else class="empty-note">尚未登记券商连接。</p>
      </section>

      <section class="card" aria-labelledby="draft-title">
        <div class="section-header compact">
          <div><h2 id="draft-title">订单草案预检</h2><p>服务端会重新计算金额、费用、风控和安全阻断原因。</p></div>
        </div>
        <form class="stack-form" @submit.prevent="createDraft">
          <label>模拟账户<select v-model="draftForm.paper_account_id" required><option value="" disabled>选择用于风险影子计算的账户</option><option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }} · ¥{{ money(account.cash) }}</option></select></label>
          <label>券商连接<select v-model="draftForm.connection_id" required><option value="" disabled>选择已登记连接</option><option v-for="connection in connections" :key="connection.id" :value="connection.id">{{ connection.provider }} · {{ connection.account_ref }}</option></select></label>
          <div class="inline-fields"><label>代码<input v-model="draftForm.code" pattern="\d{6}\.(SH|SZ|BJ)" required /></label><label>方向<select v-model="draftForm.side"><option value="buy">买入</option><option value="sell">卖出</option></select></label></div>
          <div class="inline-fields"><label>股数<input v-model.number="draftForm.quantity" type="number" min="100" step="100" required /></label><label>限价<input v-model.number="draftForm.limit_price" type="number" min="0.01" step="0.01" required /></label></div>
          <button class="btn-accent" type="submit" :disabled="saving || !accounts.length || !connections.length">生成草案</button>
        </form>
        <p class="form-note">手动输入价格会被明确标记为 manual_input；实时价格必须同时提供时间戳和新鲜度。</p>
      </section>
    </div>

    <section class="card section" aria-labelledby="draft-history-title">
      <div class="section-header compact"><div><h2 id="draft-history-title">订单草案记录</h2><p>草案与真实成交严格分开；取消不会产生订单。</p></div></div>
      <div v-if="drafts.length === 0" class="empty-note">暂无订单草案。</div>
      <div v-else class="draft-list">
        <article v-for="draft in drafts" :key="draft.id" class="draft-row">
          <div class="draft-main"><strong>{{ draft.side === 'buy' ? '买入' : '卖出' }} {{ draft.code }}</strong><span>{{ draft.quantity.toLocaleString('zh-CN') }} 股 · ¥{{ money(draft.limit_price) }} · 预计费用 ¥{{ money(draft.estimated_fee) }}</span></div>
          <div class="draft-risk" :class="draft.risk_status"><span>{{ draft.risk_status === 'approved' ? '风控通过' : '已阻断' }}</span><small v-if="draft.risk_reason">{{ draft.risk_reason }}</small></div>
          <div class="draft-actions"><button v-if="draft.status === 'draft'" class="btn-secondary" type="button" @click="confirmDraft(draft.id)">确认草案</button><button v-if="!['cancelled', 'submitted', 'filled'].includes(draft.status)" class="link-button" type="button" @click="cancelDraft(draft.id)">取消</button><span v-else class="status-text">{{ draftStatus(draft.status) }}</span></div>
        </article>
      </div>
    </section>

    <section class="card section" aria-labelledby="audit-title">
      <div class="section-header compact"><div><h2 id="audit-title">审计记录</h2><p>只显示操作结果和脱敏详情。</p></div></div>
      <div v-if="audits.length === 0" class="empty-note">暂无审计事件。</div>
      <div v-else class="audit-list"><div v-for="event in audits.slice(0, 12)" :key="event.id" class="audit-row"><span class="audit-time">{{ event.created_at }}</span><strong>{{ event.action }}</strong><span class="status-text" :class="event.outcome">{{ event.outcome }}</span></div></div>
    </section>

    <p v-if="status" class="page-status" role="status">{{ status }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useApi } from '@/composables/useApi'

const { api } = useApi()
const loading = ref(false)
const saving = ref(false)
const status = ref('')
const capabilities = reactive({ can_submit_live: false, kill_switch_active: true, blocked_reasons: [] as string[] })
const connections = ref<any[]>([])
const accounts = ref<any[]>([])
const drafts = ref<any[]>([])
const audits = ref<any[]>([])
const connectionForm = reactive({ provider: 'ibkr', account_ref: 'sandbox-account', mode: 'sandbox', credential_ref: '' })
const draftForm = reactive({ connection_id: '', paper_account_id: '', code: '000001.SZ', side: 'buy', quantity: 100, limit_price: 10 })

const reasonLabels: Record<string, string> = {
  live_trading_disabled_by_config: '配置仍处于实盘关闭状态',
  kill_switch_active: 'Kill Switch 已开启',
  broker_adapter_not_configured: '尚未安装经审查的券商适配器',
  no_enabled_broker_connection: '尚无通过测试的券商连接',
  connection_not_enabled: '券商连接未启用',
  live_execution_not_implemented: '实盘执行链路尚未启用',
}
const blockedReasonsText = computed(() => capabilities.blocked_reasons.map(reason => reasonLabels[reason] ?? reason).join('；') || '系统未满足实盘安全条件。')

function money(value: number | null | undefined) { return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }
function connectionStatus(value: string) { return ({ disabled: '未启用', unavailable: '不可用', ready: '已就绪' } as Record<string, string>)[value] ?? value }
function draftStatus(value: string) { return ({ confirmed: '已确认（未提交）', cancelled: '已取消', submitted: '已提交', filled: '已成交' } as Record<string, string>)[value] ?? value }

async function refreshAll() {
  loading.value = true
  const results = await Promise.allSettled([
    api.get('/live/capabilities'),
    api.get('/live/connections'),
    api.get('/live/drafts', { params: { limit: 50 } }),
    api.get('/live/audit', { params: { limit: 50 } }),
    api.get('/paper/accounts'),
  ])
  const [capabilityResult, connectionResult, draftResult, auditResult, accountResult] = results
  if (capabilityResult.status === 'fulfilled') Object.assign(capabilities, capabilityResult.value.data)
  if (connectionResult.status === 'fulfilled') connections.value = connectionResult.value.data
  if (draftResult.status === 'fulfilled') drafts.value = draftResult.value.data
  if (auditResult.status === 'fulfilled') audits.value = auditResult.value.data
  if (accountResult.status === 'fulfilled') accounts.value = accountResult.value.data
  if (!draftForm.paper_account_id && accounts.value[0]) draftForm.paper_account_id = accounts.value[0].id
  if (!draftForm.connection_id && connections.value[0]) draftForm.connection_id = connections.value[0].id
  loading.value = false
}

async function createConnection() {
  saving.value = true
  try {
    await api.post('/live/connections', { ...connectionForm, credential_ref: connectionForm.credential_ref || null })
    status.value = '连接已登记；在适配器和官方沙盒就绪前仍保持阻断。'
    await refreshAll()
  } catch (error: any) { status.value = `登记失败：${error.response?.data?.detail ?? error.message}` } finally { saving.value = false }
}

async function probeConnection(id: string) {
  try { await api.post(`/live/connections/${id}/test`); status.value = '连接测试完成，结果已写入状态和审计记录。'; await refreshAll() } catch (error: any) { status.value = `测试失败：${error.response?.data?.detail ?? error.message}` }
}

async function createDraft() {
  saving.value = true
  try {
    await api.post('/live/drafts', { ...draftForm, idempotency_key: `live-ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, price_source: 'manual_input', price_freshness: 'manual' })
    status.value = '订单草案已生成，未创建模拟成交，也未发送真实委托。'
    await refreshAll()
  } catch (error: any) { status.value = `草案失败：${error.response?.data?.detail ?? error.message}` } finally { saving.value = false }
}

async function confirmDraft(id: string) {
  try { await api.post(`/live/drafts/${id}/confirm`, { confirmation_phrase: 'CONFIRM LIVE ORDER' }); status.value = '确认结果已返回。'; await refreshAll() } catch (error: any) { status.value = `确认被阻断：${error.response?.data?.detail ?? error.message}` }
}

async function cancelDraft(id: string) {
  try { await api.post(`/live/drafts/${id}/cancel`, { reason: 'cancelled_by_user' }); status.value = '订单草案已取消。'; await refreshAll() } catch (error: any) { status.value = `取消失败：${error.response?.data?.detail ?? error.message}` }
}

async function lockLive() {
  try { await api.post('/live/control/kill-switch', { active: true, reason: 'user_relocked_from_phase3_ui' }); status.value = 'Kill Switch 已重新开启。'; await refreshAll() } catch (error: any) { status.value = `操作失败：${error.response?.data?.detail ?? error.message}` }
}

onMounted(() => { void refreshAll() })
</script>

<style scoped>
.page-header { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:20px; }
.page-note { margin:4px 0 0; color:var(--text-secondary); font-size:13px; }
.safety-banner { display:flex; align-items:center; gap:14px; border-color:rgba(228,168,58,.35); background:rgba(228,168,58,.07); }
.safety-banner.safe { border-color:rgba(54,179,126,.32); background:rgba(54,179,126,.06); }
.safety-icon { display:grid; width:32px; height:32px; flex:0 0 auto; place-items:center; border:1px solid currentColor; border-radius:50%; color:#edbd62; font-weight:750; }
.safe .safety-icon { color:#69c99e; }
.safety-copy { min-width:0; flex:1; }
.safety-copy h2 { margin:0; font-size:15px; }
.safety-copy p { margin:4px 0 0; color:var(--text-secondary); font-size:12px; line-height:1.5; }
.safety-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
.pill { border:1px solid var(--border-strong); border-radius:999px; padding:4px 9px; font-size:11px; }
.pill-safe { border-color:rgba(54,179,126,.3); color:#69c99e; }
.pill-warning { border-color:rgba(228,168,58,.3); color:#edbd62; }
.two-column { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:12px; }
.compact { margin-bottom:14px; }
.section-header h2 { margin:0; font-size:15px; }
.section-header p { margin:4px 0 0; color:var(--text-tertiary); font-size:11px; }
.stack-form { display:grid; gap:11px; }
.stack-form label { display:grid; gap:5px; color:var(--text-secondary); font-size:11px; }
.stack-form input, .stack-form select { width:100%; min-height:36px; border:1px solid var(--border-strong); border-radius:5px; background:var(--bg-secondary); color:var(--text-primary); padding:8px 10px; }
.stack-form small, .form-note { color:var(--text-tertiary); font-size:10px; line-height:1.45; }
.inline-fields { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.connection-list, .draft-list, .audit-list { display:grid; gap:0; margin-top:16px; border-top:1px solid var(--border); }
.connection-row, .draft-row, .audit-row { display:flex; align-items:center; justify-content:space-between; gap:12px; border-bottom:1px solid var(--border); padding:11px 0; }
.connection-row strong, .draft-main strong { display:block; font-size:12px; }
.connection-row span, .draft-main span { display:block; margin-top:3px; color:var(--text-tertiary); font-size:10px; }
.connection-actions, .draft-actions { display:flex; align-items:center; gap:10px; }
.status-text { color:var(--text-secondary); font-size:10px; }
.status-text.unavailable, .status-text.blocked { color:#edbd62; }
.status-text.error, .status-text.failed { color:#f5838a; }
.link-button { border:0; background:none; color:var(--accent); cursor:pointer; padding:2px; font-size:11px; }
.empty-note { color:var(--text-tertiary); font-size:12px; }
.section { margin-top:12px; }
.draft-risk { min-width:180px; color:#edbd62; font-size:11px; text-align:right; }
.draft-risk.approved { color:#69c99e; }
.draft-risk small { display:block; max-width:260px; margin-top:3px; color:var(--text-tertiary); font-size:10px; overflow-wrap:anywhere; }
.audit-row { justify-content:flex-start; }
.audit-time { min-width:190px; color:var(--text-tertiary); font-family:"SFMono-Regular",Consolas,monospace; font-size:10px; }
.audit-row strong { min-width:230px; font-size:11px; font-weight:560; }
.page-status { margin-top:14px; color:var(--text-secondary); font-size:12px; }
@media (max-width:860px) { .two-column { grid-template-columns:1fr; } .safety-banner { align-items:flex-start; flex-wrap:wrap; } .safety-actions { width:100%; justify-content:flex-start; margin-left:46px; } }
@media (max-width:620px) { .page-header { flex-direction:column; } .inline-fields { grid-template-columns:1fr; } .connection-row, .draft-row { align-items:flex-start; flex-direction:column; } .connection-actions, .draft-actions { width:100%; justify-content:space-between; } .draft-risk { min-width:0; text-align:left; } .audit-row { align-items:flex-start; flex-direction:column; gap:4px; } .audit-time, .audit-row strong { min-width:0; } }
</style>
