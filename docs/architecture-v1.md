# kk_quant 产品架构 v1

> 状态：Accepted  
> 日期：2026-09-03  
> 适用范围：A 股日线研究、回测、模拟组合和受控实盘接入

## 1. 产品边界

kk_quant 是面向个人投资者的单用户量化工作台。产品默认给出可解释的推荐、目标仓位和风险提示；默认不会自动提交真实订单。只有在数据新鲜、风控通过、券商连接正常、账户完成模拟验证且用户在执行前确认时，实盘网关才允许提交订单。

第一版明确支持 A 股和 ETF 的日线研究、周频调仓。分钟级、高频、期权、融资融券、自动选取“最优策略”和承诺收益均不在 v1 范围内。

## 2. 已核实的现状

- Python 单元测试基线：182 passed；前端生产构建通过。
- 本地行情只有 2022-06-01 至 2024-12-31 的 30 只左右样本文件；股票元数据表为空。
- AKShare 东方财富实时快照在当前网络代理下失败；新浪全市场快照可返回约 5,500 条，耗时约 14 秒。
- 服务端没有真正启动 APScheduler，没有代码调用 WebSocket `broadcast`，因此页面显示“已连接”不等于存在实时推送。
- 模拟账户是进程级单例；策略、资金、持仓和快照没有账户隔离；异常被静默吞掉。
- 前端没有 K 线或统计图实现，没有前端单测、端到端测试、可访问性测试与响应式验收。
- 当前没有券商适配器、实盘凭证管理、订单幂等、日亏损熔断、审计日志和人工确认门。

## 3. 确定性技术方案

不整体迁移到第三方量化框架。保留现有 Python 事件驱动回测内核，逐步补齐契约和产品层：

```text
Vue 3 工作台
  ├─ 今日行动（推荐、风险、待确认订单）
  ├─ 市场（实时快照、K 线、数据状态）
  ├─ 研究（策略模板、回测、对比）
  ├─ 组合（模拟账户、目标仓位、成交与归因）
  └─ 系统（数据源、任务、券商、审计）
                    │ REST + typed WebSocket
FastAPI 应用层      │
  ├─ MarketDataService ─ HistoricalDataProvider / LiveMarketDataProvider
  ├─ ResearchService ── BacktestEngine / Analytics
  ├─ TradingService ─── TargetPortfolio → RiskEngine → BrokerGateway
  ├─ LedgerService ───── Orders / Fills / Positions / Cash / NAV
  └─ JobService ──────── data sync / signal / reconciliation / report
                    │
  Parquet（不可变行情） + SQLite（单用户业务账本）
```

### 3.1 数据

- `HistoricalDataProvider` 与 `LiveMarketDataProvider` 分开，避免用实时抓取接口承担可复现回测。
- 免费默认源：AKShare。结构化日线生产主源预留 Tushare Pro（需要用户 token，受其服务条款约束）。
- 每条响应必须携带 `source`、`as_of`、`received_at`、`freshness` 和 `is_fallback`；过期缓存不能标记为实时。
- 采集采用超时、有限重试、断路、原子落盘、schema 校验和最近成功缓存；数据源失败不返回伪造价格。
- 复权、成分股、财务数据必须使用 point-in-time 可见日期；无法证明时间点正确的数据不能进入严肃回测。
- 基本面数据按 `report_date` 与 `announce_date` 双时间建模；查询必须传递 `as_of`，只选择 `announce_date <= as_of` 的最新版本。没有公告日期或授权来源的数据只能作为待导入文件，不能进入价值/质量回测。

### 3.2 研究与回测

- 保留现有事件驱动引擎和 A 股 T+1、涨跌停、停牌、佣金、印花税模型。
- 修复当前策略直接访问内部 `DataHandler`、以成交量近似市值、信号日与成交日不分离等问题。
- 每次回测记录代码版本、策略参数、数据版本、费用模型、股票池版本、随机种子和运行日志。
- 标准验收包括无前视、幸存者偏差声明、样本内/外拆分、基准、换手、容量与压力成本。

### 3.3 模拟与实盘统一链路

```text
TargetPortfolio
  → RiskEngine（拒绝或缩量，并给出原因）
  → OrderIntent（幂等键）
  → BrokerGateway（Paper / Live）
  → ExecutionReport
  → Ledger（不可变事件）
  → Reconciliation（券商对账）
```

- `PaperBroker` 和 `LiveBroker` 只能在网关层分叉；策略不允许直接调用券商。
- 实盘默认关闭。开启条件：明确选择已支持的券商、配置凭证、通过连接测试、模拟观察期达标、设置资金上限和风控、逐批确认订单。
- 不在代码或数据库明文保存密钥；部署时只从环境变量或操作系统密钥存储读取。
- v1 风控硬限制：账户资金上限、单标的权重、单笔金额、总仓位、现金缓冲、日成交额占比、日亏损熔断、重复订单、陈旧行情、非交易时段和一键停机。

### 3.4 前端

- 保留 Vue 3 + TypeScript + Vite + Pinia。
- ECharts 负责净值、回撤、归因、持仓、月度热力图和当前的日 K 线视图；后续如需更高阶盘口交互，再评估单独引入 KLineCharts，避免并存两套 K 线引擎。
- 服务端状态采用 query/cache 模式；WebSocket 使用一个带类型、心跳、指数退避和可见性恢复的连接管理器。
- A 股默认红涨绿跌；颜色之外同时用正负号、文字和图形编码。
- 页面必须有 loading / empty / stale / error / partial / success 六类状态，不允许用 0 代替“尚无数据”。

## 4. 开源方案筛选结论

| 方案 | 用法 | 结论 |
|---|---|---|
| vn.py | XTP/TORA 等网关 sidecar、接口语义参考 | 实盘优先候选；不替换当前研究内核 |
| Qlib | 可选 ML/因子研究流水线 | Phase 3 后按需接入，初学者默认隐藏 |
| vectorbt | 可选快速参数初筛 | 仅研究工具，不作为真实撮合依据 |
| RQAlpha | A 股规则与接口参考 | 非商业许可限制，且迁移收益不足，不作为内核 |
| LEAN | 生产工程和经纪商模型参考 | 依赖 .NET/Docker，A 股个人券商适配弱，不迁移 |
| NautilusTrader | 统一回测/模拟/实盘语义参考 | Rust/微观结构复杂度超出日线 A 股 v1，不迁移 |
| Backtrader / Zipline | 回测 API 参考 | 维护与实盘链路不匹配，不迁移 |
| AKShare | 免费探索、实时回退、交叉校验 | 保留，但必须承认网页源易变和学术用途提示 |
| Tushare Pro | 结构化 EOD 候选主源 | 需要 token；接入前由用户确认服务与权限 |

主要依据：

- <https://github.com/vnpy/vnpy>
- <https://github.com/microsoft/qlib>
- <https://github.com/QuantConnect/Lean>
- <https://github.com/nautechsystems/nautilus_trader>
- <https://github.com/ricequant/rqalpha>
- <https://github.com/akfamily/akshare>
- <https://github.com/tradingview/lightweight-charts>
- <https://github.com/klinecharts/KLineChart>

## 5. 分支与阶段

```text
main
└─ codex/phase-1-foundation
   └─ codex/phase-2-paper-trading
      └─ codex/phase-3-live-readiness
```

每阶段完成后由未参与该阶段实现的 agent 审查。审查必须给出可复现命令、失败项和判定，不以作者说明代替测试。

### Phase 1：可信研究闭环

交付：数据源契约和健康度、可中止/可观测回测任务、示例策略可运行、市场与回测图表、输入校验、统一错误结构、单测和基础端到端测试。

退出条件：离线样本回测在限定时间内完成；结果可复现；行情接口明确标注来源/时间/降级；182 项旧测试无回归；新增测试通过；前端构建和核心页面交互通过。

### Phase 2：持久化模拟交易

交付：多模拟账户、资金分配、统一订单/成交/账本、风控、交易日任务、WebSocket 推送、偏差跟踪、日报和恢复/对账。

退出条件：重启后资金与持仓一致；同一幂等键不重复下单；陈旧行情必拒绝；风险规则均有拒绝测试；模拟订单、成交、持仓、现金和净值满足会计恒等；WebSocket 断线可恢复。

### Phase 3：实盘就绪与产品化

交付：BrokerGateway 插件边界、至少一个由用户账户决定的真实券商适配、凭证安全、预交易确认、kill switch、审计、备份恢复、监控告警、完整 UI 和部署手册。

退出条件：券商沙盒或测试账户完成下单/撤单/成交回报/重连/对账；故障注入不会重复下单；默认配置不可能提交实盘；安全与威胁模型审查通过；用户用新手向导可以完成“选模板→回测→模拟→生成实盘订单草案”。

## 6. 不能伪造为已完成的外部条件

完整实盘验收依赖用户选择实际券商并提供其官方 API 权限或沙盒账户。没有账户类型、柜台/网关、凭证和测试环境时，只能交付并验证 `BrokerGateway`、PaperBroker、风控与禁用状态，不能声称真实账户已跑通，更不能代替用户确认资金交易。

## 7. 本轮实现检查点

- Phase 1 已落地：行情源契约/降级与时效、只读健康接口、无前视的次日开盘撮合、费用现金缓冲、T+1/涨跌停拒单、回测取消与 30 分钟截止、前端状态视图和日 K 线。
- Phase 2 当前施工结果：`PaperAccount`、账户持仓、订单、成交、账本、估值和买入批次均已持久化；`PaperBrokerGateway` 通过 `RiskEngine` 执行资金、单笔金额、仓位、日亏损和持仓数量检查；同一幂等键会复用原订单，修改价格会返回冲突；行情必须显式标注来源、时间和新鲜度，陈旧/未知行情拒绝；买入批次按 A 股 T+1 解锁日控制卖出；页面只操作当前持久化账户，网络未确认时保留幂等键用于安全重试；订单成交和估值会广播到 `paper:{account_id}` 与 `dashboard` WebSocket，账户报告、偏差、日报和对账接口已提供。
- Phase 2 自动化已接入：`PaperDailyScheduler` 按上海交易日运行，收盘前自动任务只记录“尚未收盘”而不冻结当天结果；使用实时源对所有活跃账户做估值并生成日报，后续成交会触发当日任务重新估值；行情源不可用、缺失或时间戳不新鲜时，任务记录 `skipped` 而不伪造价格；实时源禁止回写历史日期，历史回放必须使用明确支持历史数据的测试/回放 provider；SQLite 文件数据库使用 `BEGIN IMMEDIATE` 配合进程锁防止跨进程并发写入；调度运行记录可查询，未来日期拒绝执行。旧 `/paper/init|trigger|reset` 接口仅为兼容旧模拟引擎，Phase 2 页面不再调用它们。
- 当前验证：后端 `pytest` **226 passed, 4 warnings**；前端 Node 测试 **6 passed**，`npm run build` 成功；应用入口导入与 Phase 2 lifespan 启停已验证；运行时接口已验证健康检查、持久账户、订单、T+1 拒单、估值、对账和调度器未来日期/实时源历史日期保护。由于外部行情连接在当前环境受限，自动任务会明确记录 `market_data_unavailable`，不能据此声称实时行情已接通。
- Phase 3 当前施工结果：已建立 `LiveBrokerGateway` / `BrokerCapabilities` 扩展契约（含券商订单 ID、未完成订单恢复和带游标成交回放接口）；新增默认关闭的实盘能力状态、持久化 kill switch、券商连接登记（仅允许 `env:` / `keychain:` 凭证引用）、服务端订单草案预检、费用估算、报价来源与人工确认门、草案取消和 append-only 审计接口。审计写入边界统一递归脱敏，Compose API 端口默认只发布到宿主机 loopback。当前没有注册任何真实券商适配器，`can_submit_live=false`，确认接口会安全阻断；详见 `docs/phase-3-plan.md`。
- Phase 3B-S / 3C-O 增量：新增 `SandboxBrokerGateway` 与 `/api/v1/live/sandbox` 本地确定性演练接口，可验证提交、部分成交、继续撮合、撤单、未完成订单恢复和事件 cursor；新增 `/api/v1/live/audit/export` 脱敏 JSON/CSV 导出。沙盒会话通过本地 JSON checkpoint 原子持久化，`supports_live=false`，不读取凭证、不联网、不创建 `PaperOrder`，且不改变实盘能力阻断。官方券商沙盒、认证授权、真实券商备份和对账监控仍未接入。
- Phase 3C-O 运维增量：新增 `/api/v1/live/ops/status` 控制面健康摘要与告警，以及带 SHA-256 校验和的安全备份 envelope 和只读完整性校验。备份排除凭证，恢复接口保持关闭；这不等同于多用户认证、真实券商凭证轮换、跨主机恢复或远程监控。
- Phase 3C-A 认证增量：新增可配置 `QUANT_OPERATOR_TOKEN`。令牌非空时，`/api/v1/live/**` 和 `/api/v1/live/sandbox/**` 统一要求 `X-Operator-Token`，使用常量时间比较；空令牌仅保留 loopback 开发体验，`/api/health` 不受影响。
- Phase 3C-R 凭证引用轮换：新增连接凭证定位符更新接口；只接受 `env:`/`keychain:` 引用，更新后自动停用连接并要求重新测试，不接触明文凭证。
- Phase 3C-F 本地故障演练：本地确定性沙盒可持久化 `submit`/`advance`/`reconcile` 故障开关，用固定 503 验证前端降级、checkpoint 回滚、重启恢复和幂等重试；不连接券商，也不解除 `can_submit_live=false`。官方券商故障、对账和监控仍待适配器接入后验收。
- 市场行情数据源已扩展：市场页优先请求腾讯公开快照 `tencent:qt`，失败或缺码时回退到 AKShare Eastmoney/Sina；本地日线窗口不足或仅部分覆盖时，日 K 页面回退到腾讯 `qfqday` JSON 接口。每条数据保留来源、源时间、接收时间和 freshness，全部源失败返回结构化 503；非法日期、倒序日期和超长远程区间均显式拒绝；详见 `docs/market-data-sources.md`。
