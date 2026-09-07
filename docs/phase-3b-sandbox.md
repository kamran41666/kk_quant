# Phase 3B-S：本地确定性沙盒闭环

日期：2026-09-05  
分支：`codex/phase-3-live-readiness`

## 目标

在等待用户选定券商并提供官方沙盒文档/账号期间，先把 Phase 3B 的网关边界、订单生命周期和恢复语义变成可运行的合同测试。该实现是本地确定性沙盒，不代表任何券商接入，也不提供交易所级行情。

## 已交付能力

- `SandboxBrokerGateway` 实现 `LiveBrokerGateway`：提交、拒单、全成、部分成交、继续撮合、撤单、查询未完成订单、事件游标回放。
- 同一 `intent_id` 幂等重放，返回同一订单状态，不重复扣减资金或持仓。
- `checkpoint()` / `from_checkpoint()` 支持在测试和本地演示中模拟重启恢复；恢复后保留订单状态与事件游标。
- `/api/v1/live/sandbox/sessions` 提供本地沙盒会话创建和查询。
- `/prices` 设置演示价格，`/orders` 提交 A 股 100 股整数倍订单，`/advance` 推进未完成订单，`/cancel` 撤单，`/events?since=` 增量回放，`/restart` 演示恢复。
- 沙盒能力明确 `supports_sandbox=true`、`supports_live=false`；不会读取 `env:`/`keychain:` 凭证，不产生 `PaperOrder`，不改变 `/live/capabilities` 的 `can_submit_live=false`。

## 边界与限制

- 会话状态由进程内缓存和 `data/sandbox_sessions/sbx-*.json` 原子 checkpoint 组成；服务进程重启后首次读取会自动恢复会话，`restart` 仍可用于显式验证恢复协议。该文件不是包含凭证的生产备份，也不提供跨主机恢复。
- 撮合价格是用户在沙盒会话中设置的确定性价格，不是实时行情；不得用于真实交易决策或收益判断。
- 该适配器不接收网络请求，不支持 `supports_live`，不能被用于绕过实盘 kill switch、配置开关或人工确认门。
- 官方券商沙盒适配器仍必须另行实现，并由未参与实现的 agent 按连接健康、订单状态、重连、对账和权限边界独立验收。

## 验收标准

- 单元测试覆盖部分成交后继续撮合、撤单幂等、拒单不改变账户、事件 cursor 增量回放、checkpoint 恢复。
- API 输入对代码、方向、价格、100 股整数倍和未知会话做明确校验。
- 全量后端测试、`compileall`、前端测试与生产构建均不得回归。
- 运行态仍必须报告 `can_submit_live=false`；任何 `live` 连接/订单确认路径不因沙盒注册而解锁。
