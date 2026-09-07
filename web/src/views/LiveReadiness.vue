<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>账户接入</h1>
        <p class="page-note">连接账户后查看余额、持仓和资产状态；当前仅支持研究与模拟，不会发送真实委托。</p>
      </div>
      <button class="btn-secondary" type="button" @click="refreshAll" :disabled="loading">{{ loading ? '刷新中...' : '刷新状态' }}</button>
    </div>

    <details class="advanced-settings">
      <summary>高级设置：操作员令牌</summary>
      <div class="operator-token-row" aria-label="操作员令牌">
        <label>当前标签页令牌<input v-model="operatorToken" type="password" autocomplete="off" placeholder="设置 QUANT_OPERATOR_TOKEN 后填写" /></label>
        <div class="operator-token-actions"><button class="btn-secondary" type="button" @click="applyOperatorToken">{{ operatorToken ? '应用令牌' : '清除令牌' }}</button><small>令牌不会写入项目或 localStorage。</small></div>
      </div>
    </details>

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
        <span class="pill" :class="opsStatus.status === 'ok' ? 'pill-safe' : 'pill-warning'">
          {{ opsStatus.status === 'ok' ? '运维检查正常' : opsStatus.status === 'unknown' ? '运维状态未知' : '运维需关注' }}
        </span>
        <button v-if="!capabilities.kill_switch_active" class="btn-danger" type="button" @click="lockLive">重新锁定实盘</button>
      </div>
    </section>
    <div v-if="opsStatus.alerts.length" class="ops-alerts" role="status"><span v-for="alert in opsStatus.alerts.slice(0, 3)" :key="alert.code">{{ alert.message }}</span></div>

    <section class="card account-overview" aria-labelledby="account-overview-title">
      <div class="section-header compact"><div><h2 id="account-overview-title">账户概览</h2><p>余额、持仓和资产状态集中展示。</p></div><span class="sandbox-badge">连接后同步</span></div>
      <div v-if="accounts.length" class="account-grid">
        <article v-for="account in accounts" :key="account.id" class="account-card">
          <div class="account-card-head"><div><strong>{{ account.name }}</strong><span>{{ account.market === 'us-equity' ? '美股 · USD' : account.market === 'cn-fund' ? '国内基金 · CNY' : 'A股 · CNY' }}</span></div><span class="status-text">模拟账户</span></div>
          <div class="account-metrics"><div><span>总权益</span><strong>¥{{ money(account.equity ?? account.initial_capital) }}</strong></div><div><span>可用现金</span><strong>¥{{ money(account.cash) }}</strong></div><div><span>持仓市值</span><strong>¥{{ money(account.market_value) }}</strong></div><div><span>持仓数量</span><strong>{{ account.positions?.length ?? 0 }}</strong></div></div>
          <button class="link-button" type="button" @click="router.push({ name: 'Paper', query: { account_id: account.id } })">查看模拟账户详情 →</button>
        </article>
      </div>
      <div v-else class="account-empty"><strong>尚未加载账户</strong><span>连接后将在这里显示余额与持仓；当前可先创建模拟账户。</span><button class="btn-secondary" type="button" @click="router.push({ name: 'Paper' })">进入模拟账户</button></div>
    </section>

    <section class="card sandbox-card" aria-labelledby="sandbox-title">
      <div class="section-header compact">
        <div><h2 id="sandbox-title">本地沙盒演练</h2><p>用确定性价格走一遍提交、部分成交、撤单和重启恢复；不会连接券商，也不会影响模拟账户。</p></div>
        <span class="sandbox-badge">仅本地 · 不可实盘</span>
      </div>
      <div class="sandbox-grid">
        <form class="stack-form" @submit.prevent="createSandbox">
          <div class="inline-fields"><label>演示资金<input v-model.number="sandboxForm.initial_cash" type="number" min="1000" step="1000" required /></label><label>首笔成交<select v-model.number="sandboxForm.initial_fill_ratio"><option :value="1">全部成交</option><option :value="0.5">先成交一半</option><option :value="0">先挂单</option></select></label></div>
          <button class="btn-secondary" type="submit" :disabled="sandboxSaving">{{ sandboxSession ? '重新创建沙盒' : '创建本地沙盒' }}</button>
        </form>
        <div v-if="sandboxSession" class="sandbox-summary">
          <div><span>会话</span><strong>{{ sandboxSession.id }}</strong></div>
          <div><span>权益</span><strong>¥{{ money(sandboxSession.snapshot?.equity) }}</strong></div>
          <div><span>现金</span><strong>¥{{ money(sandboxSession.snapshot?.cash) }}</strong></div>
          <div><span>事件游标</span><strong>{{ sandboxSession.event_cursor }}</strong></div>
        </div>
        <p v-else class="empty-note sandbox-empty">创建后即可在这里演练订单生命周期。</p>
      </div>
      <div v-if="sandboxSession" class="sandbox-workflow">
        <div class="sandbox-fields">
          <label>演示代码<input v-model="sandboxForm.code" pattern="\d{6}\.(SH|SZ|BJ)" /></label>
          <label>演示价格<input v-model.number="sandboxForm.price" type="number" min="0.01" step="0.01" /></label>
          <label>方向<select v-model="sandboxForm.order_side"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
          <label>股数<input v-model.number="sandboxForm.order_quantity" type="number" min="100" step="100" /></label>
        </div>
        <div class="sandbox-actions">
          <button class="btn-secondary" type="button" :disabled="sandboxSaving" @click="setSandboxPrice">设置价格</button>
          <button class="btn-accent" type="button" :disabled="sandboxSaving" @click="submitSandboxOrder">提交演示订单</button>
          <button class="btn-secondary" type="button" :disabled="sandboxSaving" @click="advanceSandbox">推进成交</button>
          <button class="btn-secondary" type="button" :disabled="sandboxSaving" @click="restartSandbox">重启并恢复</button>
        </div>
        <details class="sandbox-advanced">
          <summary>故障演练（高级）</summary>
          <p>只在本地沙盒中模拟服务不可用，验证页面提示、状态回滚和恢复流程；不会影响券商连接。</p>
          <div class="sandbox-fault-fields">
            <label>模拟环节<select v-model="sandboxFaultForm.operation"><option value="submit">提交订单</option><option value="advance">推进成交</option><option value="reconcile">事件回放</option></select></label>
            <label class="sandbox-fault-toggle"><input v-model="sandboxFaultForm.active" type="checkbox" /> 注入故障</label>
            <button class="btn-secondary" type="button" :disabled="sandboxSaving" @click="setSandboxFault">{{ sandboxFaultForm.active ? '启用故障' : '恢复操作' }}</button>
          </div>
          <small v-if="Object.keys(sandboxSession.faults || {}).length" class="sandbox-fault-active">当前故障：{{ Object.keys(sandboxSession.faults || {}).join('、') }}</small>
        </details>
        <div v-if="sandboxOrders.length" class="sandbox-order-list"><div v-for="order in sandboxOrders" :key="order.intent.intent_id" class="sandbox-order-row"><span>{{ order.intent.side === 'buy' ? '买入' : '卖出' }} {{ order.intent.code }} · {{ order.filled_quantity }}/{{ order.intent.quantity }} 股</span><div class="sandbox-order-actions"><strong :class="order.status">{{ sandboxStatusText(order.status) }}</strong><button v-if="['accepted', 'partially_filled'].includes(order.status)" class="link-button" type="button" @click="cancelSandboxOrder(order.broker_order_id)">撤单</button></div></div></div>
        <div v-if="sandboxEvents.length" class="sandbox-event-list"><span v-for="event in sandboxEvents.slice(-6).reverse()" :key="`${event.event_cursor}-${event.intent_id}`">#{{ event.event_cursor }} {{ sandboxStatusText(event.status) }} · {{ event.code }}</span></div>
      </div>
      <p v-if="sandboxStatus" class="page-status" role="status">{{ sandboxStatus }}</p>
    </section>

    <section class="card connection-card" aria-labelledby="connection-title">
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
            <div class="connection-actions"><span class="status-text" :class="connection.status">{{ connectionStatus(connection.status) }}</span><button class="link-button" type="button" @click="probeConnection(connection.id)">测试</button><button class="link-button" type="button" @click="beginCredentialRotation(connection.id)">更新引用</button></div>
            <form v-if="rotatingConnectionId === connection.id" class="connection-rotate" @submit.prevent="rotateCredentialReference(connection.id)">
              <input v-model="rotationCredentialRef" type="text" autocomplete="off" placeholder="env:NEW_API_KEY 或 keychain:broker" required />
              <button class="btn-secondary" type="submit" :disabled="saving">保存引用</button>
              <button class="link-button" type="button" @click="cancelCredentialRotation">取消</button>
            </form>
          </article>
        </div>
        <p v-else class="empty-note">尚未登记券商连接。</p>
    </section>

    <section class="card section" aria-labelledby="audit-title">
      <div class="section-header compact"><div><h2 id="audit-title">审计记录</h2><p>只显示操作结果和脱敏详情。</p></div><div class="audit-actions"><button class="link-button" type="button" :disabled="auditExporting" @click="exportAudit('json')">导出 JSON</button><button class="link-button" type="button" :disabled="auditExporting" @click="exportAudit('csv')">导出 CSV</button><button class="link-button" type="button" :disabled="auditExporting" @click="exportSafeBackup">下载安全备份</button></div></div>
      <div v-if="audits.length === 0" class="empty-note">暂无审计事件。</div>
      <div v-else class="audit-list"><div v-for="event in audits.slice(0, 12)" :key="event.id" class="audit-row"><span class="audit-time">{{ event.created_at }}</span><strong>{{ event.action }}</strong><span class="status-text" :class="event.outcome">{{ event.outcome }}</span></div></div>
    </section>

    <p v-if="status" class="page-status" role="status">{{ status }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '@/composables/useApi'

const { api } = useApi()
const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const status = ref('')
function readOperatorToken() {
  try { return typeof window !== 'undefined' ? window.sessionStorage.getItem('quant.operator.token') ?? '' : '' } catch { return '' }
}
const operatorToken = ref(readOperatorToken())
const capabilities = reactive({ can_submit_live: false, kill_switch_active: true, blocked_reasons: [] as string[] })
const opsStatus = reactive({ status: 'unknown', alerts: [] as Array<{ code: string; severity: string; message: string }> })
const connections = ref<any[]>([])
const accounts = ref<any[]>([])
const audits = ref<any[]>([])
const sandboxSession = ref<any | null>(null)
const sandboxOrders = ref<any[]>([])
const sandboxEvents = ref<any[]>([])
const sandboxSaving = ref(false)
const sandboxStatus = ref('')
const auditExporting = ref(false)
const connectionForm = reactive({ provider: 'ibkr', account_ref: 'sandbox-account', mode: 'sandbox', credential_ref: '' })
const rotatingConnectionId = ref<string | null>(null)
const rotationCredentialRef = ref('')
const sandboxForm = reactive({ initial_cash: 100_000, initial_fill_ratio: 1, code: '000001.SZ', price: 10, order_side: 'buy', order_quantity: 100 })
const sandboxFaultForm = reactive({ operation: 'submit', active: false })

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
function sandboxStatusText(value: string) { return ({ accepted: '已接收', partially_filled: '部分成交', filled: '已成交', rejected: '已拒绝', cancelled: '已撤单' } as Record<string, string>)[value] ?? value }

async function refreshAll() {
  loading.value = true
  const results = await Promise.allSettled([
    api.get('/live/capabilities'),
    api.get('/live/connections'),
    api.get('/live/audit', { params: { limit: 50 } }),
    api.get('/paper/accounts'),
    api.get('/live/ops/status'),
  ])
  const [capabilityResult, connectionResult, auditResult, accountResult, opsResult] = results
  if (capabilityResult.status === 'fulfilled') Object.assign(capabilities, capabilityResult.value.data)
  if (capabilityResult.status === 'rejected' && capabilityResult.reason?.response?.status === 401) status.value = '当前部署要求操作员令牌，请在上方输入后应用。'
  if (connectionResult.status === 'fulfilled') connections.value = connectionResult.value.data
  if (auditResult.status === 'fulfilled') audits.value = auditResult.value.data
  if (accountResult.status === 'fulfilled') accounts.value = accountResult.value.data
  if (opsResult.status === 'fulfilled') Object.assign(opsStatus, opsResult.value.data)
  if (sandboxSession.value) await refreshSandbox()
  loading.value = false
}

async function refreshSandbox() {
  if (!sandboxSession.value) return
  const id = sandboxSession.value.id
  const results = await Promise.allSettled([
    api.get(`/live/sandbox/sessions/${id}`),
    api.get(`/live/sandbox/sessions/${id}/orders`),
    api.get(`/live/sandbox/sessions/${id}/events`),
  ])
  if (results[0].status === 'fulfilled') sandboxSession.value = results[0].value.data
  if (results[0].status === 'rejected') sandboxStatus.value = `沙盒状态读取失败：${results[0].reason?.response?.data?.detail ?? results[0].reason?.message ?? '服务不可用'}`
  if (results[1].status === 'fulfilled') sandboxOrders.value = results[1].value.data
  if (results[1].status === 'rejected') sandboxStatus.value = `订单状态读取失败：${results[1].reason?.response?.data?.detail ?? results[1].reason?.message ?? '服务不可用'}`
  if (results[2].status === 'fulfilled') sandboxEvents.value = results[2].value.data
  if (results[2].status === 'rejected') sandboxStatus.value = `事件回放暂不可用：${results[2].reason?.response?.data?.detail ?? results[2].reason?.message ?? '服务不可用'}`
}

async function createSandbox() {
  sandboxSaving.value = true
  try {
    const result = await api.post('/live/sandbox/sessions', {
      initial_cash: sandboxForm.initial_cash,
      initial_fill_ratio: sandboxForm.initial_fill_ratio,
    })
    sandboxSession.value = result.data
    sandboxOrders.value = []
    sandboxEvents.value = []
    sandboxStatus.value = '本地沙盒已创建；当前所有价格和成交均为演示数据。'
  } catch (error: any) { sandboxStatus.value = `创建失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

function applyOperatorToken() {
  const token = operatorToken.value.trim()
  if (typeof window !== 'undefined') {
    try {
      if (token) window.sessionStorage.setItem('quant.operator.token', token)
      else window.sessionStorage.removeItem('quant.operator.token')
    } catch {
      status.value = '当前浏览器禁止标签页存储，请检查隐私设置后重试。'
      return
    }
  }
  status.value = token ? '操作员令牌已应用到当前标签页。' : '操作员令牌已清除。'
  void refreshAll()
}

async function setSandboxPrice() {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try { const result = await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/prices`, { code: sandboxForm.code, price: sandboxForm.price }); sandboxSession.value = result.data; sandboxStatus.value = '演示价格已设置，未读取外部行情。' } catch (error: any) { sandboxStatus.value = `设置失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

async function submitSandboxOrder() {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try { await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/orders`, { intent_id: `sandbox-ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, code: sandboxForm.code, side: sandboxForm.order_side, quantity: sandboxForm.order_quantity }); sandboxStatus.value = '演示订单已提交；请观察成交状态和事件游标。'; await refreshSandbox() } catch (error: any) { sandboxStatus.value = `提交失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

async function setSandboxFault() {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try {
    const result = await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/faults`, {
      operation: sandboxFaultForm.operation,
      active: sandboxFaultForm.active,
    })
    sandboxSession.value = result.data
    sandboxStatus.value = sandboxFaultForm.active
      ? `已注入${sandboxFaultForm.operation}故障；下一次操作会返回可恢复的 503。`
      : `已恢复${sandboxFaultForm.operation}操作。`
  } catch (error: any) {
    sandboxStatus.value = `故障设置失败：${error.response?.data?.detail ?? error.message}`
  } finally { sandboxSaving.value = false }
}

async function advanceSandbox() {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try { const result = await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/advance`); sandboxStatus.value = result.data.length ? '未完成订单已推进撮合。' : '当前没有可推进的未完成订单。'; await refreshSandbox() } catch (error: any) { sandboxStatus.value = `推进失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

async function cancelSandboxOrder(brokerOrderId: string) {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try { await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/orders/${brokerOrderId}/cancel`); sandboxStatus.value = '演示订单已撤销；已成交部分不会回滚。'; await refreshSandbox() } catch (error: any) { sandboxStatus.value = `撤单失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

async function restartSandbox() {
  if (!sandboxSession.value) return
  sandboxSaving.value = true
  try { const result = await api.post(`/live/sandbox/sessions/${sandboxSession.value.id}/restart`); sandboxSession.value = result.data; sandboxStatus.value = '已从 checkpoint 重建沙盒；订单和事件游标保持一致。'; await refreshSandbox() } catch (error: any) { sandboxStatus.value = `恢复失败：${error.response?.data?.detail ?? error.message}` } finally { sandboxSaving.value = false }
}

async function exportAudit(format: 'json' | 'csv') {
  auditExporting.value = true
  try {
    const result = await api.get('/live/audit/export', { params: { format, limit: 500 }, responseType: 'blob' })
    const blob = new Blob([result.data], { type: format === 'json' ? 'application/json' : 'text/csv' })
    const href = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = `kk-quant-audit.${format}`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(href), 1000)
    status.value = `审计记录已导出为 ${format.toUpperCase()}。`
  } catch (error: any) { status.value = `导出失败：${error.response?.data?.detail ?? error.message}` } finally { auditExporting.value = false }
}

async function exportSafeBackup() {
  auditExporting.value = true
  try {
    const result = await api.get('/live/ops/backup', { responseType: 'blob' })
    const href = URL.createObjectURL(new Blob([result.data], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = 'kk-quant-safe-backup.json'
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(href), 1000)
    status.value = '安全备份已导出；备份不含凭证，当前仅支持完整性校验。'
  } catch (error: any) { status.value = `备份失败：${error.response?.data?.detail ?? error.message}` } finally { auditExporting.value = false }
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

function beginCredentialRotation(id: string) {
  rotatingConnectionId.value = id
  rotationCredentialRef.value = ''
}

function cancelCredentialRotation() {
  rotatingConnectionId.value = null
  rotationCredentialRef.value = ''
}

async function rotateCredentialReference(id: string) {
  saving.value = true
  try {
    await api.post(`/live/connections/${id}/credential-ref/rotate`, { credential_ref: rotationCredentialRef.value.trim() })
    status.value = '凭证引用已更新；连接已自动停用，需重新测试后才能进入就绪状态。'
    cancelCredentialRotation()
    await refreshAll()
  } catch (error: any) { status.value = `更新失败：${error.response?.data?.detail ?? error.message}` } finally { saving.value = false }
}

async function lockLive() {
  try { await api.post('/live/control/kill-switch', { active: true, reason: 'user_relocked_from_phase3_ui' }); status.value = 'Kill Switch 已重新开启。'; await refreshAll() } catch (error: any) { status.value = `操作失败：${error.response?.data?.detail ?? error.message}` }
}

onMounted(() => { void refreshAll() })
</script>

<style scoped>
.page-header { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:20px; }
.page-note { margin:4px 0 0; color:var(--text-secondary); font-size:13px; }
.advanced-settings { margin-bottom:12px; border:1px solid var(--border); border-radius:6px; background:rgba(15,22,34,.2); }
.advanced-settings summary { padding:10px 12px; color:var(--text-secondary); font-size:11px; cursor:pointer; }
.advanced-settings[open] summary { border-bottom:1px solid var(--border); color:var(--text-primary); }
.advanced-settings .operator-token-row { margin:0; border:0; border-radius:0; background:transparent; }
.operator-token-row { display:flex; align-items:flex-end; gap:12px; margin-bottom:12px; padding:10px 12px; border:1px solid var(--border); border-radius:6px; background:rgba(15,22,34,.2); }
.operator-token-row label { display:grid; flex:1; gap:5px; color:var(--text-secondary); font-size:11px; }
.operator-token-row input { width:100%; min-height:34px; border:1px solid var(--border-strong); border-radius:5px; background:var(--bg-secondary); color:var(--text-primary); padding:7px 10px; }
.operator-token-actions { display:grid; gap:5px; justify-items:start; }
.operator-token-actions small { color:var(--text-tertiary); font-size:10px; }
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
.connection-row { flex-wrap:wrap; }
.connection-row strong, .draft-main strong { display:block; font-size:12px; }
.connection-row span, .draft-main span { display:block; margin-top:3px; color:var(--text-tertiary); font-size:10px; }
.connection-actions, .draft-actions { display:flex; align-items:center; gap:10px; }
.connection-rotate { display:flex; flex-basis:100%; align-items:center; gap:8px; }
.connection-rotate input { flex:1; min-height:32px; border:1px solid var(--border-strong); border-radius:5px; background:var(--bg-secondary); color:var(--text-primary); padding:7px 10px; }
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
.audit-actions { display:flex; align-items:center; gap:10px; }
.page-status { margin-top:14px; color:var(--text-secondary); font-size:12px; }
.sandbox-card { margin-top:12px; border-color:rgba(86,151,224,.28); background:linear-gradient(135deg,rgba(86,151,224,.06),rgba(54,179,126,.035)); }
.ops-alerts { display:grid; gap:5px; margin-top:8px; color:var(--text-tertiary); font-size:11px; }
.account-overview { margin-top:12px; }
.account-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.account-card { display:grid; gap:12px; min-width:0; padding:13px; border:1px solid var(--border); border-radius:7px; background:rgba(15,22,34,.24); }
.account-card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.account-card-head > div { display:grid; gap:4px; min-width:0; }
.account-card-head strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
.account-card-head span { color:var(--text-tertiary); font-size:10px; }
.account-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.account-metrics > div { display:grid; gap:4px; min-width:0; }
.account-metrics span { color:var(--text-tertiary); font-size:9px; }
.account-metrics strong { overflow:hidden; color:var(--text-primary); font-family:"SFMono-Regular",Consolas,monospace; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }
.account-empty { display:flex; align-items:center; flex-wrap:wrap; gap:12px; padding:13px; border:1px dashed var(--border-strong); border-radius:7px; color:var(--text-secondary); font-size:11px; }
.account-empty strong { color:var(--text-primary); }
.account-empty span { flex:1; min-width:220px; color:var(--text-tertiary); }
.connection-card { margin-top:12px; }
.sandbox-badge { flex:0 0 auto; border:1px solid rgba(86,151,224,.32); border-radius:999px; padding:4px 9px; color:#8bb9ec; font-size:10px; }
.sandbox-grid { display:grid; grid-template-columns:minmax(260px, .9fr) minmax(0, 1.1fr); gap:14px; align-items:start; }
.sandbox-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.sandbox-summary > div { border:1px solid var(--border); border-radius:6px; padding:9px; background:rgba(15,22,34,.24); }
.sandbox-summary span { display:block; color:var(--text-tertiary); font-size:10px; }
.sandbox-summary strong { display:block; margin-top:5px; overflow:hidden; color:var(--text-primary); font-family:"SFMono-Regular",Consolas,monospace; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }
.sandbox-empty { margin:0; padding:13px 0; }
.sandbox-workflow { display:grid; gap:11px; margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
.sandbox-fields { display:grid; grid-template-columns:1.2fr 1fr .8fr 1fr; gap:10px; }
.sandbox-fields label { display:grid; gap:5px; color:var(--text-secondary); font-size:11px; }
.sandbox-fields input, .sandbox-fields select { width:100%; min-height:36px; border:1px solid var(--border-strong); border-radius:5px; background:var(--bg-secondary); color:var(--text-primary); padding:8px 10px; }
.sandbox-actions { display:flex; flex-wrap:wrap; gap:8px; }
.sandbox-advanced { border-top:1px solid var(--border); padding-top:10px; color:var(--text-tertiary); font-size:11px; }
.sandbox-advanced summary { color:var(--text-secondary); cursor:pointer; }
.sandbox-advanced p { margin:8px 0; }
.sandbox-fault-fields { display:flex; flex-wrap:wrap; align-items:end; gap:10px; }
.sandbox-fault-fields label { display:grid; gap:5px; }
.sandbox-fault-fields select { min-height:32px; border:1px solid var(--border-strong); border-radius:5px; background:var(--bg-secondary); color:var(--text-primary); padding:6px 8px; }
.sandbox-fault-toggle { display:flex !important; align-items:center; min-height:32px; }
.sandbox-fault-active { display:block; margin-top:8px; color:#edbd62; }
.sandbox-order-list, .sandbox-event-list { display:grid; gap:0; border-top:1px solid var(--border); }
.sandbox-order-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); color:var(--text-secondary); font-size:11px; }
.sandbox-order-actions { display:flex; align-items:center; gap:10px; }
.sandbox-order-row strong { font-size:10px; font-weight:600; }
.sandbox-order-row strong.filled { color:#69c99e; }
.sandbox-order-row strong.partially_filled, .sandbox-order-row strong.accepted { color:#edbd62; }
.sandbox-order-row strong.rejected, .sandbox-order-row strong.cancelled { color:#f5838a; }
.sandbox-event-list { display:flex; flex-wrap:wrap; gap:6px 12px; padding-top:9px; color:var(--text-tertiary); font-family:"SFMono-Regular",Consolas,monospace; font-size:10px; }
@media (max-width:860px) { .two-column { grid-template-columns:1fr; } .safety-banner { align-items:flex-start; flex-wrap:wrap; } .safety-actions { width:100%; justify-content:flex-start; margin-left:46px; } }
@media (max-width:860px) { .account-grid { grid-template-columns:1fr; } }
@media (max-width:860px) { .sandbox-grid { grid-template-columns:1fr; } .sandbox-summary { grid-template-columns:repeat(2,minmax(0,1fr)); } .sandbox-fields { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width:620px) { .page-header { flex-direction:column; } .operator-token-row { align-items:stretch; flex-direction:column; } .operator-token-actions { justify-items:stretch; } .inline-fields { grid-template-columns:1fr; } .connection-row, .draft-row { align-items:flex-start; flex-direction:column; } .connection-actions, .draft-actions { width:100%; justify-content:space-between; } .draft-risk { min-width:0; text-align:left; } .audit-row { align-items:flex-start; flex-direction:column; gap:4px; } .audit-time, .audit-row strong { min-width:0; } .sandbox-fields { grid-template-columns:1fr; } .sandbox-summary { grid-template-columns:1fr 1fr; } }
</style>
