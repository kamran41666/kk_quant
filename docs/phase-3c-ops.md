# Phase 3C-O：审计导出与本地运营基线

日期：2026-09-05

## 已交付

- `GET /api/v1/live/audit/export?format=json|csv&limit=1..500` 导出最近审计事件。
- 导出复用 `list_audit_events` 的脱敏结果；不会因为导出而重新读取或暴露凭证。
- `/live` 页面提供 JSON/CSV 两个下载入口，成功或失败都会给出明确状态。
- JSON 保留结构化 `details`，CSV 将 `details` 序列化为单列，便于表格查看。
- `GET /api/v1/live/ops/status` 提供本地控制面健康摘要与告警计数，不返回凭证。
- `GET /api/v1/live/ops/backup` 导出带 SHA-256 校验和的安全备份 envelope；`POST /api/v1/live/ops/backup/verify` 只做完整性校验，不执行恢复或覆盖。
- 设置 `QUANT_OPERATOR_TOKEN` 后，`/api/v1/live/**`（包括沙盒）要求 `X-Operator-Token`，使用常量时间比较；未设置时也只放行 `127.0.0.1`/`::1`/`localhost`，非回环来源 fail-closed。
- `POST /api/v1/live/connections/{id}/credential-ref/rotate` 支持更新 `env:`/`keychain:` 凭证定位符；更新后连接自动停用并要求重新测试，接口和审计均不接收或保存密钥值。平台只轮换定位符，真实密钥由环境管理或操作系统密钥存储负责轮换。
- 本地沙盒会话使用 `data/sandbox_sessions/sbx-*.json` 原子 checkpoint，服务进程重启后可自动恢复订单、持仓、价格和事件游标；该恢复仅适用于本地沙盒，不是凭证备份或真实券商对账。
- 本地沙盒支持 `POST /api/v1/live/sandbox/sessions/{id}/faults` 注入 `submit`、`advance` 或 `reconcile` 故障；故障状态随 checkpoint 保存，服务端以固定 `503 sandbox_fault_injected` 响应，执行失败会回滚内存状态。该能力只用于验证客户端降级、重试幂等和重启恢复，不触碰任何券商或真实账户。

## 边界

- 这是本机运营能力，不等同于多用户权限、远程审计归档或不可抵赖签名。
- 备份不包含 API key/secret，也不包含可直接恢复的凭证明文；恢复 API 明确关闭，避免未认证覆盖运行态。
- `/api/health` 保持公开用于存活探针；高风险 live/sandbox 路由不复用健康探针权限。
- Phase 3C 剩余的多用户会话认证/授权、真实券商备份恢复、对账告警、监控和官方券商故障演练仍需在正式券商接入前完成；本地沙盒故障注入不等价于柜台故障验收。
- `can_submit_live=false` 与 kill switch 约束不因导出功能改变。

## 验收

- JSON/CSV 均只包含已脱敏事件，非法格式和超出范围的参数由 API 明确拒绝。
- 备份 checksum 可验证篡改，运维状态在默认 kill switch 下保持安全状态。
- 本地故障演练可稳定复现 503，失败提交不产生订单，清除故障后可继续操作，重启仍保留 checkpoint 状态。
- 全量后端测试、前端测试和生产构建保持通过。
