# Phase 3 实盘就绪施工计划

> 文档状态：历史阶段计划与验收记录。文中的“当前分支”“当前批次”和测试数均属于记录日期；实盘边界仍可参考，当前实现状态见 [`project-state-audit-2026-09-09.md`](project-state-audit-2026-09-09.md)。
> 当前分支：`codex/phase-3-live-readiness`  
> 当前施工批次：Phase 3A-M4-B / 3B-S / 3C-O / 3C-A / 3C-R / 3C-F（全市场宽度、行情工作台与本地故障恢复基线）
> 真实券商状态：未接入；默认拒绝任何实盘委托

## 目标与边界

Phase 3 的目标不是把模拟盘改成“看起来像实盘”，而是在用户选择官方券商、提供沙盒/API 权限之前，把真实交易所需的安全边界、状态机和审计链路先固定下来。

本批只交付：

- `BrokerGateway` 的 `LiveBrokerGateway` 扩展契约和能力描述（含券商订单 ID、未完成订单恢复和带游标成交回放接口）
- 券商连接登记；数据库只保存 `env:` / `keychain:` 凭证引用，不保存密钥
- 持久化全局 `kill switch`，默认开启；实盘配置默认关闭
- 服务端权威订单草案、报价来源/时间/新鲜度、费用预估和风控预检
- 人工确认门、草案取消、幂等重放和 append-only 审计事件
- 能力状态 API 和前端“实盘准备”入口；未配置适配器时只显示阻断原因
- 审计写入边界递归脱敏；Docker Compose 默认只把 API 端口发布到宿主机 loopback
- 市场页真实数据链：腾讯公开快照与日 K 源优先、AKShare 双源回退、本地窗口不足/部分覆盖时的远程日 K 回退，并对来源、时效、日期边界和超长区间显式标记

本批明确不做：

- 不猜测或伪造任何券商 API
- 不在浏览器、日志或 SQLite 写入 API key、secret、密码或 token
- 不允许通过环境变量单独打开实盘；必须有已审查适配器、连接健康、风控通过和人工确认

## 分段施工与独立验收

### Phase 3A：安全控制面与订单草案（历史计划，已验收）

退出条件：默认 `can_submit_live=false`；凭证明文被拒绝；草案只生成预检结果且不产生 `PaperOrder`；kill switch、幂等、报价时效、取消和审计有回归测试；入口和 lifespan 可导入/启动。

### Phase 3B：沙盒适配器与执行状态机

前置条件：用户选择券商并提供官方沙盒文档/账号。实现连接测试、下单、撤单、异步成交回报、重连和状态恢复；适配器只能通过 `LiveBrokerGateway` 注册。

在官方券商资料到位前，已先完成 **Phase 3B-S 本地合同沙盒**：
`SandboxBrokerGateway` 和 `/live/sandbox` 运行态接口可演示提交、部分成交、继续撮合、撤单、未完成订单查询、事件 cursor 增量回放及 JSON checkpoint 重启恢复。会话文件只保存本地沙盒状态，且采用原子替换；它明确 `supports_live=false`，不会改变实盘能力开关。详见 `docs/phase-3b-sandbox.md`。官方券商沙盒接入仍未完成，不能以本地沙盒代替。

### Phase 3C：实盘安全与运营

增加权限/会话认证、凭证轮换、备份恢复、对账告警、监控、审计导出、故障注入和部署加固。默认配置必须在测试中证明无法提交真实订单。

已先交付 **Phase 3C-O 审计导出基线**：`/live/audit/export` 支持脱敏 JSON/CSV 下载，页面提供导出入口；随后已补充操作员令牌门（3C-A）、凭证引用轮换（3C-R）和本地故障演练（3C-F），但这些增量仍不等于完整 Phase 3C。详见 `docs/phase-3c-ops.md`。

已补充 **Phase 3C-A 操作员令牌门**：设置 `QUANT_OPERATOR_TOKEN` 后，所有 `/live/**` 与 `/live/sandbox/**` 路由要求 `X-Operator-Token`，健康检查保持可用；默认空令牌只适合 loopback 开发体验，Compose 启动则强制要求显式令牌，避免容器网络请求被错误地当作回环。

已补充 **Phase 3C-R 凭证引用轮换**：`/live/connections/{id}/credential-ref/rotate` 只更新 `env:`/`keychain:` 定位符，自动停用并要求重新测试；真实凭证轮换由用户的操作系统密钥存储或环境管理系统负责，平台不接触明文。

已补充 **Phase 3C-F 本地故障演练**：本地沙盒可对提交、推进成交和事件回放注入确定性故障；故障开关写入 JSON checkpoint，失败操作返回固定 503，服务端保留旧 checkpoint，页面可验证恢复和幂等重试。该批不代表官方券商故障验收；真实券商对账监控、监控告警和柜台级故障演练仍未完成。

Phase 3A 的控制面仍是本机使用边界：API 路由尚未提供用户会话认证，因此容器部署必须使用 Compose 中的 loopback 端口发布；在任何真实适配器接入前，Phase 3C 必须补齐认证/授权并通过未授权访问测试。

每一段由未参与实现的 agent 独立运行全量后端测试、前端类型/构建、入口生命周期和关键 API 场景；外部券商网络不可用时必须显式标记为未验收，不得用 mock 结果代替。

本轮市场源独立验收：后端变更相关测试 **20 passed**；其余回归测试 **242 passed, 1 deselected**（唯一排除项是会等待外部 AKShare 的既有默认股票池用例）；`compileall`、前端 **6 passed**、`vue-tsc` 和 Vite 构建（678 modules）均通过。运行服务实际返回 3/3 腾讯快照、242 条腾讯 qfq 日 K，健康状态为 `ok`；当前日期为周六，快照源时间为上一交易日，因此页面正确标记 `stale`，不宣称实时成交。

## Phase 3A-M：全市场行情工作台增量计划（历史计划，M1-M4-B 已验收）

### 目标

把当前只展示 3 只自选证券的 `/market` 页面升级为“全 A 股行情工作台”：用户可以先看大盘和指数，再搜索任意 A 股，打开证券详情查看日 K/周 K/月 K 与常用指标；交互保持支付宝理财页面式的卡片、标签和搜索，不要求用户理解数据源、复权或策略实现细节。

### 适用边界

- “全 A 股”指证券主数据可搜索、行情按分页/批量加载、指数和市场宽度可查看，不在浏览器一次性渲染全部股票。
- 当前腾讯公开源和 AKShare 适合低频研究展示；没有 SLA，也不能作为真实下单确认源。页面必须持续显示来源、源时间、接收时间和 freshness；源不可用时显示明确状态，不补造价格。
- 日 K、周 K、月 K 统一从同一套日线数据和复权口径生成。分钟/Tick 实时与真实交易柜台仍属于 Phase 3B 的 MiniQMT/券商适配范围。

### 分段施工

**M1：证券主数据与市场摘要契约**

- 建立 `SecurityMasterProvider`，同步代码、名称、交易所、板块、上市状态和最近更新时间；本地 `meta.db` 为查询缓存，AKShare 为可选补充源。
- 新增指数快照契约（上证综指、深证成指、创业板指、沪深 300、上证 50、中证 500），每条数据带 `source/as_of/received_at/freshness`。
- 新增市场宽度摘要：上涨/下跌/平盘数量、涨停/跌停（源支持时）、成交额汇总和统计时间；不把缺失源当成 0。

**M2：全市场查询与 K 线服务**

- 保留现有 `/market/quotes` 批量接口，增加分页/搜索接口：`/market/universe`、`/market/indexes`、`/market/breadth`。
- 增加证券详情接口：`/market/candles/{code}?interval=1d|1w|1mo&adjust=event_driven`。周 K、月 K 由已验证的日 K 聚合，确保 OHLC、成交量和日期严格升序、无前视。
- 增加指标查询：MA/EMA、RSI、MACD、BOLL、KDJ；实现复用当前策略/指标模块，参考 KhQuant 的指标思想，不复制 `MyTT.py`。
- 采用服务器端搜索、请求去重、批量分片（单次不超过 provider 契约上限）、30 秒快照缓存和 stale-while-revalidate，避免一次刷新打满公共源。

**M3：支付宝式行情工作台前端**

- 顶部为“市场总览”卡片：指数、涨跌、成交额和市场宽度；中部为搜索框和结果列表，支持代码/名称模糊搜索、交易所/板块筛选和分页。
- 证券详情采用标签切换“分时（源支持时）/日 K/周 K/月 K”，指标开关保持少量推荐项；默认展示价格、涨跌幅、成交量、均线和 tooltip，不堆叠专业配置。
- 保留自选入口，但自选只是工作台的一部分；缺失证券、回退源、stale/unknown、历史窗口不足均用可理解的状态提示。
- 图表组件必须保持日期升序、空值留白、缩放和悬浮明细；不因刷新重置用户当前选中的证券和周期。

**M4：独立验收与稳定性**

- 未参与实现的 agent 验收：证券搜索、指数卡片、市场宽度、四种周期、指标数值、来源/时效提示、分页和断网恢复。
- 合同测试覆盖空股票池、重复代码、非法代码、无交易日、停牌/缺失 K 线、部分 provider 成功、公共源超时和超大查询；不得用固定价格伪造成功。
- 运行态验收至少包括：可搜索的证券主数据更新时间、指数来源、任意证券的日/周/月 K、断网后的明确错误和恢复后的缓存刷新。

### 交付判断

M1-M4 全部通过后，才可称为“全市场行情工作台完成”。在 MiniQMT 或正式数据授权接入前，只能称为“低频公开行情研究工作台”，不能称为交易所级实时终端，也不能解除 Phase 3 的 `can_submit_live=false`。

### M1 施工状态（2026-09-05）

M1 已完成并提交（`ac26312`）。证券主数据、六大指数、市场宽度契约、服务器端搜索/分页、来源与 freshness 标记、缓存回退和任意证券日 K 详情均已部署到 `/market`；独立复核未发现 P0/P1。下一批为 M2：统一的日/周/月 K 线聚合与推荐指标接口，仍保持研究/模拟边界。

### M2 施工状态（2026-09-05）

M2 核心实现已完成并通过独立最终审查：`/market/candles/{code}` 已提供日/周/月 K 线、推荐指标、复权口径告警和 30 秒按键缓存；`MarketCenter.vue` 已提供周期切换、指标开关及 MACD/RSI/KDJ 子图。详见 `docs/market-workbench-m2.md`。后续 M3/M4 将继续完善成交量窗格、缩放保持、批量行情列和运行态稳定性验收。

### M3 施工状态（2026-09-05）

M3 已完成并通过独立最终审查：行情快照支持 500 代码上限、每批 100 个代码的服务端分片；全市场当前页显示价格/涨跌；顶部与 30 秒刷新覆盖当前页；K 线增加成交量子图、跨子图缩放和浏览器偏好持久化。详见 `docs/market-workbench-m3.md`。M4 仅剩真实浏览器交互、断网恢复和多子图视觉密度验收。

### M4 施工状态（2026-09-05）

M4 独立运行态验收已通过：已验证日/周/月周期、四类子图、当前页行情、分页、刷新竞态和来源/过期提示；窄屏响应式样式已通过类型与生产构建检查。全量后端 `274 passed, 1 deselected`，前端 `6 passed` 且生产构建通过。详见 `docs/market-workbench-m4.md`。M1-M4 交付判断为“低频公开行情研究工作台完成”；Phase 3B/3C 的官方券商沙盒、认证、对账和真实交易仍未实施。

### M4-B 施工状态（2026-09-05）

M4-B 已补齐全市场宽度的实际公开源路径：默认 `/market/breadth` 从 5,556 条证券主数据生成代码列表，按 100 只、最多 6 路并发请求 Tencent，并使用 30 秒缓存；运行态已验证 `5,556/5,556` 返回、`status=ok`，第二次读取命中缓存。统计接口同时返回指标覆盖率、完整/部分状态、接收时间和聚合时效，页面按 `N/M` 展示有效样本；缓存按 provider 对象身份隔离，避免地址复用误命中。详见 `docs/market-workbench-m4-breadth.md`。该增量仍属于低频公开行情研究，不是交易所级实时终端，也不解除实盘阻断。

### 历史数据适配器稳定性增量（2026-09-05）

针对 Ashare 探查暴露出的旧历史入口风险，`AKShareAdapter` 已完成一轮不改变 `DataSource` 接口的加固：每个上游调用有明确 deadline，并对同一操作实施 single-flight；代码、日期、OHLCV 值域和重复记录在进入 `PriceStore` 前校验；批次返回通过 `DataFrame.attrs["source_meta"]` 标记 `ok/partial/failed`、请求/返回/失败代码和接收时间；`DataPipeline` 将状态写入 `data_update_log`。指数成分接口没有历史日期契约，现改为 fail-closed，禁止当前成分造成回测前视偏差。

该增量已通过适配器、证券主数据和 DataPipeline 边界测试；仍未声称 AKShare 具备交易所级 SLA，真实交易确认继续禁止使用公开聚合源。

独立复审后又补齐：重复日期会使批次降级为 `partial`；`failed` 批次禁止写入 `PriceStore`；上游异常会写入失败日志；无点时股票池的回测拒绝使用硬编码生存者列表。历史指数成分快照仍是后续必须建设的数据资产，未完成前不能宣称默认股票池回测已具备无偏保证。

随后补充有限值校验与 `PriceStore.write_batch()` 临时文件/回滚机制；写入失败不会留下已替换的前半批文件。该批仍不等于历史指数成分数据已经补齐。

最后补齐通用 `DataSource` 的完整 OHLCV 列门槛：缺列、非有限值和无效价格不会写入 `PriceStore`，相应批次会落 `failed` 审计记录。

点时回测资产已形成闭环：MetaDB 新增按 `index_code + as_of` 保存的成分快照表，DataAPI 只返回生效日不晚于回测日的最近快照；市场 API 提供单期归档、批量导入、`dry_run` 预校验、查询和“今日当前快照”入口。批量导入在全部期间校验通过后以单事务写入，HTTP、DataAPI、MetaDB 三层拒绝未来日期。没有快照时默认回测和模拟交易均 fail-closed，均不再使用固定幸存者股票列表；当前快照仍不能伪装成历史数据。

批量资产入口另提供 `scripts/import_index_snapshots.py`，支持 CSV/Parquet/JSON、SHA-256 文件指纹和 `--dry-run`；模拟交易的首次运行在没有点时成分快照时返回 `PIT_STOCK_POOL_UNAVAILABLE`，不会创建默认交易。

纸面账户在行情为空、股票池行情部分覆盖、持仓缺少收盘价或价格为 NaN/无穷/非正数时返回明确阻断码，不更新估值也不保存错误快照；导入器和底层存储同时提供文件一致性、时区和旧版 SQLite 迁移保护。

### Phase 4A 基本面数据层增量（2026-09-05）

已补齐原规划中长期缺失的基本面 PIT 资产层：`fundamentals` 表、按公告日期的可见性查询、离线 CSV/Parquet/JSON 导入、SHA-256 校验、批量事务和市场 API 查询。当前没有经过授权和公告日期验证的默认联网财务源，因此无数据时明确返回 `empty`，不会将当前财报用于历史回测。详见 `docs/phase-4a-fundamentals.md`。

### Phase 4B 策略观察期（模拟）增量（2026-09-06）

已新增纸面策略观察控制面：只有存在已完成回测的策略才能创建 7 天或 30 天观察任务；任务支持启动、暂停、恢复、停止、到期和同日 tick 幂等，自动目标调整通过现有纸面订单链执行，手动订单仍可并行并继续遵守整手、T+1、现金、仓位、亏损和报价时效规则。历史窗口、策略信号或行情数据不完整时 fail-closed，并将原因写入事件时间线。前端 `/paper` 已提供策略选择、资金比例、自动模拟下单开关、生命周期控制和事件查看。详见 `docs/phase-4b-observations.md`。

该增量仍严格为 `paper_only`，不读取券商凭据、不调用真实交易接口，也不代表真实资金观察已经接入；主 agent 复核、最新全量回归和浏览器点击链路验收完成后，才能将其标记为 Phase 4B 完成。

### Phase 4B 最终验收状态（2026-09-06）

Phase 4B 已按上述 A 股纸面观察范围完成验收：历史回放的成交/Fill/Ledger/Lot 日期与 `as_of` 一致，调度器会为观察新开仓补齐估值报价；`PaperLot.owner` 区分 `strategy` 与 `manual` 批次，策略减仓不会误卖同证券的手动加仓；旧版 SQLite 会将缺失归属字段迁移为 `manual`。独立 agent 复核无 P0/P1，后端全量 **377 passed**（1 个既有 matcher warning），前端测试 **6 passed**、类型检查和生产构建通过，浏览器已验证 `/market` → 详情页 → 返回、基金/美股切换、589px 无横向溢出及 `/paper` 观察边界提示。

因此，Phase 4B 可标记为“按定义范围完成”：范围严格限于单账户 A 股策略观察和纸面订单；基金/美股行情详情已可用，但跨市场纸面策略观察、多策略批次隔离和真实券商执行转入 Phase 4C/后续阶段。`paper_only=true`、`live_execution=false`、`can_submit_live=false` 继续保持，不得据此宣称真实资金可交易。

### Phase 4C 当前施工与验收状态（2026-09-06）

Phase 4C 已落地市场维度的纸面账户、A 股/国内基金/美股代码与数量规则、交易日校验、基金 Eastmoney NAV 证据登记、历史日期防未来数据泄漏、策略批次 `owner_id` 隔离和非 A 股观察边界。市场页与独立详情页支持指数/证券点击跳转、日 K/周 K/月 K、指标切换和详情到模拟交易的预填链路；指数详情明确为“仅供观察”，不提供交易入口。

当前验证：后端全量 **392 passed**（1 个既有 matcher warning），跨市场与观察边界定向 **13 passed**，前端 **6 passed**，`vue-tsc`/Vite 构建、`compileall`、Ruff 和 `git diff --check` 通过；Playwright 已验证桌面/589px 窄屏市场链路。独立 agent 复核无 P0/P1。

该阶段仍不能标记为完整 Phase 4C：跨市场策略信号和基金/美股自动估值适配器尚未开放；基金 NAV 证据已持久化到 SQLite，但多实例部署的锁与共享存储仍需专项验证；直接服务调用省略 `trade_date` 仍保留离线回放兼容路径。详见 `docs/phase-4c-plan.md` 与 `docs/phase-4c-validation.md`。真实券商执行边界继续保持关闭。

## 当前可验证的 API

- `GET /api/v1/live/capabilities`
- `GET /api/v1/live/control`
- `GET/POST /api/v1/live/connections`
- `POST /api/v1/live/connections/{id}/test`
- `POST /api/v1/live/connections/{id}/enable`
- `GET/POST /api/v1/live/drafts`
- `POST /api/v1/live/drafts/{id}/confirm`
- `POST /api/v1/live/drafts/{id}/cancel`
- `GET /api/v1/live/audit`
- `GET/POST /api/v1/live/sandbox/sessions`
- `POST /api/v1/live/sandbox/sessions/{id}/faults`（仅本地故障演练）

这些接口只构成“准备和阻断”控制面；在没有真实适配器之前，`confirm` 不会发送订单。

## Phase 3A 验证记录

- 独立审查结论：无 P0、无 P1，Phase 3A 通过；审查未修改工作树。
- 后端：`python -m pytest -q --basetemp .pytest-phase3a-final3` → **233 passed, 5 warnings**；`compileall` 通过；`from server.main import app` 导入通过。
- 前端：`npm test` → **6 passed**；`npm run build` 通过（`vue-tsc` + Vite，678 modules transformed）。
- 运行态：`GET /api/health` 返回 `ok`；`GET /api/v1/live/capabilities` 返回 `phase=3A`、`can_submit_live=false`、`paper_only=true`，并同时报告配置关闭、Kill Switch、无适配器、无启用连接和执行未实现五项阻断原因。
- 安全反向测试：`password=SUPER_SECRET` 只产生固定原因码 `user_reason_provided` 和 `reason_present=true`，不进入 `LiveControl.reason`、审计详情或 API 响应；Compose 的 8000/80 端口均绑定宿主机 loopback。
