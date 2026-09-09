# Phase 4B：策略观察期（模拟）

> 文档状态：Phase 4B 历史交付说明。其“基金/跨市场未开放”边界已由 Phase 4C/4D 后续实现更新；当前状态见 [`project-state-audit-2026-09-09.md`](project-state-audit-2026-09-09.md)，现行证据门见 [`phase-4d-evidence-gated-observation.md`](phase-4d-evidence-gated-observation.md)。

## 交付边界

Phase 4B 提供一个由回测结果进入有限期模拟观察的控制面。用户选择已完成回测的策略、7 天或 30 天时长、账户资金分配比例，并可以启动、暂停、恢复和停止任务。观察记录和事件写入本地数据库，自动订单与用户手动订单共用 `paper_trading.submit_order`，因此继续遵守 A 股 100 股整手、T+1、现金、仓位、日亏损和报价时间戳校验。

该功能的 `execution_mode` 固定为 `paper_only`，`live_execution` 固定为 `false`。它不读取券商凭据、不调用真实交易接口，也不把模拟成交当作真实成交。

## 运行规则

- 观察时长只允许 7 或 30 个日历日，结束日期包含在观察窗口内；到结束日期完成最后一次 tick 后自动进入 `completed`。
- 一个观察必须关联已有模拟账户、已有策略和至少一个 `completed` 的历史回测。没有回测证据不能创建观察任务。
- 每个观察使用 `allocated_capital` 作为自动目标权重的预算；它不会把资金从账户中划拨，也不会限制用户手动下单，手动订单仍以账户全局风控为准。
- 同一观察同一日期只允许一次成功/跳过 tick。数据不足的 `blocked` tick 可重试；不会使用零价、旧价或自行拼接的价格生成信号。
- 默认 tick 使用本地点时数据窗口生成策略信号，再使用带 `as_of` 和 freshness 的受控行情提供方；任一历史窗口、信号或报价不完整，整个 tick fail-closed。
- 自动目标调整得到的订单拥有确定的幂等键；重复请求不会重复成交。订单被 T+1 或风控拒绝时保留拒绝结果和原因。
- 观察记录持久化策略管理的证券代码集合，并为自动买入的批次标记 `strategy` 所有权。策略调仓只消耗策略批次；观察期间用户在同一证券上手动加仓或买入其他证券，手动批次均不会被策略自动卖出。
- 历史回放必须注入支持历史日期的信号/报价提供方，成交、批次、解锁日期统一使用 `as_of`；生产行情路径禁止把过去日期伪装成当日实时成交。
- 多策略观察的策略批次按 `observation_id` 隔离；账户全局现金、仓位权重和最大订单金额仍是共同风控边界。跨市场策略信号和冲突调度需在后续 C2/C3 门禁中继续验证。

## API

```
POST /api/v1/paper/accounts/{account_id}/observations
GET  /api/v1/paper/accounts/{account_id}/observations
GET  /api/v1/paper/accounts/{account_id}/observations/{observation_id}
GET  /api/v1/paper/accounts/{account_id}/observations/{observation_id}/events
POST /api/v1/paper/accounts/{account_id}/observations/{observation_id}/start
POST /api/v1/paper/accounts/{account_id}/observations/{observation_id}/pause
POST /api/v1/paper/accounts/{account_id}/observations/{observation_id}/resume
POST /api/v1/paper/accounts/{account_id}/observations/{observation_id}/stop
POST /api/v1/paper/accounts/{account_id}/observations/{observation_id}/tick
```

`tick` 的响应总是包含 `execution_mode`、`live_execution`、signals/order 数量和阻断原因，方便前端用事件时间线解释“为什么没有下单”。

## 当前明确未覆盖

后台收盘调度器会在账户估值前转发运行中的观察 tick，并在 tick 后为新开仓补齐报价；前端观察面板同时每 60 秒轮询并提供“运行一次”按钮，两条入口都使用同一日期幂等和订单幂等规则。当前观察信号、A 股交易规则和自动订单接口严格限定 A 股（`.SH/.SZ/.BJ`）；国内基金、美股已接入行情详情，基金纸面订单只接受由行情接口登记、且代码/价格/源时间/freshness 完全匹配的 Eastmoney 官方 NAV，美股可使用带时效的公开报价。跨市场纸面策略观察列入 Phase 4C，不能在当前阶段宣称支持。真实券商认证、订单回报、撤单、对账及真实资金安全链路不在本阶段，也不能由此 API 推断已经接入。

Phase 4C C2 的第一道服务端边界已经落地：对 `cn-fund` 或 `us-equity` 账户创建观察会稳定返回 HTTP 409 `strategy_observation_requires_a_share_account`；调度器即使遇到历史遗留的非 A 股运行中观察记录，也不会调用 A 股信号引擎。该错误不是数据源故障，而是当前策略能力范围的明确提示。
