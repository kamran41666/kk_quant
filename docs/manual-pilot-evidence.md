# H2d 纸面观察证据边界

本文记录 H2d 首阶段的服务端证据边界。它描述代码已经具备的约束，不代表当前已有真实 30 个交易日观察或人工执行资格。

## 真实观察准入

`POST /api/v1/manual-trading/releases/{release_id}/start-paper` 是真实前瞻观察的唯一启动入口。请求体只接受 `actor` 和 `maximum_daily_loss`；`maximum_daily_loss` 是严格的 `0 < ratio <= 1` 定点值，并且必须携带非空 `Idempotency-Key`。服务端从已完成的 holdout 评价链、固定组合资金、策略指纹和交易日历创建 `ManualPilotBinding`，并把 release 原子推进到 `paper_observing`。响应只包含 pilot、binding、冻结日历和风险元数据。

旧的 `/pilots` 创建接口不再接受 `real_forward`。旧行仍可供审计读取，但以自报观察生成的 `passed` 状态不构成资格；旧 `real_forward` 行以及任何已有 `ManualPilotBinding` 的 pilot 调用旧观察记录或终结接口都会以 `requires_trusted_daily_checkpoint` 失败。`synthetic_engineering` 仅用于工程诊断，终结结果固定为失败且不能晋级。

## 原价行情到达回执

`POST /api/v1/manual-trading/pilots/{pilot_id}/market-receipts` 只接受 `actor` 和 `Idempotency-Key`。服务端使用上海时区今日日期，要求该日期属于 binding 冻结的连续 30 个交易日，且捕获时间不早于 16:30；股票池从冻结的 holdout manifest 读取，provider 固定为 BaoStock 当前日完整主板日线查询。调用方不能提交日期、股票代码、provider、行数或时间。

服务写出原始和规范化行、manifest 及其 SHA-256，并在读回时重新验证绑定、文件集合、文件哈希、字段、日期、股票池完整性和 OHLC 一致性。`GET /api/v1/manual-trading/pilot-market-receipts/{receipt_id}` 只返回固定元数据白名单，不返回行情行、manifest 路径或其他绝对 source 路径。

这一步只证明固定 provider 的原价日线已经到达。它不包含公司行动、复权因子、基准、每日决策、模拟成交、账本、对账、日终复盘或不可变 daily checkpoint，也不能产生 `paper_passed` 或 `manual_ready`。
