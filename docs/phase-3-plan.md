# Phase 3 实盘就绪施工计划

> 当前分支：`codex/phase-3-live-readiness`  
> 当前施工批次：Phase 3A（安全控制面与订单草案）  
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

### Phase 3A：安全控制面与订单草案（当前）

退出条件：默认 `can_submit_live=false`；凭证明文被拒绝；草案只生成预检结果且不产生 `PaperOrder`；kill switch、幂等、报价时效、取消和审计有回归测试；入口和 lifespan 可导入/启动。

### Phase 3B：沙盒适配器与执行状态机

前置条件：用户选择券商并提供官方沙盒文档/账号。实现连接测试、下单、撤单、异步成交回报、重连和状态恢复；适配器只能通过 `LiveBrokerGateway` 注册。

### Phase 3C：实盘安全与运营

增加权限/会话认证、凭证轮换、备份恢复、对账告警、监控、审计导出、故障注入和部署加固。默认配置必须在测试中证明无法提交真实订单。

Phase 3A 的控制面仍是本机使用边界：API 路由尚未提供用户会话认证，因此容器部署必须使用 Compose 中的 loopback 端口发布；在任何真实适配器接入前，Phase 3C 必须补齐认证/授权并通过未授权访问测试。

每一段由未参与实现的 agent 独立运行全量后端测试、前端类型/构建、入口生命周期和关键 API 场景；外部券商网络不可用时必须显式标记为未验收，不得用 mock 结果代替。

本轮市场源独立验收：后端变更相关测试 **20 passed**；其余回归测试 **242 passed, 1 deselected**（唯一排除项是会等待外部 AKShare 的既有默认股票池用例）；`compileall`、前端 **6 passed**、`vue-tsc` 和 Vite 构建（678 modules）均通过。运行服务实际返回 3/3 腾讯快照、242 条腾讯 qfq 日 K，健康状态为 `ok`；当前日期为周六，快照源时间为上一交易日，因此页面正确标记 `stale`，不宣称实时成交。

## Phase 3A-M：全市场行情工作台增量计划（当前阶段新增）

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

这些接口只构成“准备和阻断”控制面；在没有真实适配器之前，`confirm` 不会发送订单。

## Phase 3A 验证记录

- 独立审查结论：无 P0、无 P1，Phase 3A 通过；审查未修改工作树。
- 后端：`python -m pytest -q --basetemp .pytest-phase3a-final3` → **233 passed, 5 warnings**；`compileall` 通过；`from server.main import app` 导入通过。
- 前端：`npm test` → **6 passed**；`npm run build` 通过（`vue-tsc` + Vite，678 modules transformed）。
- 运行态：`GET /api/health` 返回 `ok`；`GET /api/v1/live/capabilities` 返回 `phase=3A`、`can_submit_live=false`、`paper_only=true`，并同时报告配置关闭、Kill Switch、无适配器、无启用连接和执行未实现五项阻断原因。
- 安全反向测试：`password=SUPER_SECRET` 只产生固定原因码 `user_reason_provided` 和 `reason_present=true`，不进入 `LiveControl.reason`、审计详情或 API 响应；Compose 的 8000/80 端口均绑定宿主机 loopback。
