# 工程链路验证（仅模拟）

`engineering_validation` 用于验证调度、幂等、断点恢复和审计链路，不验证策略有效性，也不产生研究晋级或实盘资格。

## 边界

- 使用独立入口 `POST /api/v1/paper/accounts/{account_id}/engineering-validations`，固定 7 天并要求显式 `backtest_run_id`。
- 账户必须以 `validation_only=true` 创建，首次创建任务时没有持仓和其他观察；该账户拒绝标准研究观察和手工订单，策略批次继续由 `PaperLot.owner_id` 隔离。
- 完整回测证据、冻结参数、数据集/覆盖/股票池、成本场景、市场、指纹、日历、协议和执行代码仍必须匹配；仅允许忽略“绩效 checks 未通过”和 `Run.eligible_for_observation=false`。
- 当前 COPA 的拆送股成交量与历史涨跌停/停牌证据仍未通过完整性门，因此工程验证入口也会拒绝；该模式不能用来绕过数据证据缺口。
- 响应固定标记 `validation_only=true`、`promotion_eligible=false`、`paper_only=true`、`live_execution=false`。
- start、resume 和每次 tick 都重新读取持久化 `purpose` 并复核证据。工程验证完成不会修改策略研究状态、Run 研究资格或任何实盘能力。

标准研究观察继续使用原入口，研究门失败时保持拒绝。
