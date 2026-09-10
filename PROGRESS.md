# kk_quant Progress

本文件从 2026-09-09 起统一记录各次对话对项目造成的修改和交接信息。它采用倒序追加，不回填此前所有项目历史，也不复制专业文档的完整内容。需要了解建立本机制时的项目全貌，请先读 [`docs/project-state-audit-2026-09-09.md`](docs/project-state-audit-2026-09-09.md)；文档导航见 [`docs/README.md`](docs/README.md)。

## 记录规则

任何对话只要修改了代码、配置、文档、测试、数据协议或研究结论，都必须在结束前在“记录”顶部追加一条。每条记录包含：

- 北京时间和任务名称；
- 当前分支、提交状态，以及是否已经推送；
- 最终修改内容和重要文件；
- 实际完成的验证及结果；
- 尚未解决的边界或下一步；
- 需要继续阅读的专业文档链接。

只写最终事实，不记录逐步试错过程。专业文档负责完整设计、协议和证据，Progress 只写摘要与指针。后续对话若解决了旧遗留，在新记录中明确写出，不回改旧记录。冻结研究协议、数据哈希和结果不得为了更新进度而修改。

## 记录

### 2026-09-10 14:26 CST — H2b cohort原子成交与共享容量

- Git：`phase4-factor-develop`；提交`0847559`已创建，本条Progress随后提交并推送。
- 修改：新增纯计算`ManualResearchLedger`、`ResearchExecutionIntent`和cohort lot。原子执行覆盖指定cohort、T+1、A股整手/零股清仓、有效日期费用、开/收盘价格限制、现金与历史资格；所有intent、attempt、拒绝和fill分别保存，不调用paper或券商路径。
- 容量：同证券同交易日的全部cohort和买卖共享一份由前一完整交易日volume计算的容量；失败计划保留在金额加权成交率分母。同票旧cohort退出不会消费新cohort持仓，逐cohort快照加总等于账户持仓与权益。
- 验证：当前macOS后端全量`767 passed, 2 warnings`；新原子账本专项`4 passed`；compileall、关键错误级Ruff、`git diff --check`和变更Markdown链接检查通过。
- 边界：原子账本尚未接入正式v3组合循环；公司行动cohort子账、benchmark、完整12项输出、独立replay和portfolio artifact resolver仍未完成，因此不能产生`portfolio_passed`证据。
- 入口：[`H2b原子成交记录`](docs/manual-daily-trading-implementation-plan.md#31-h2b-cohort原子成交检查点)、[`组合证据合同`](docs/research-runs/manual-daily-portfolio-evidence-v1.md)、[`研究执行账本`](quant_engine/backtest/manual_research_ledger.py)。

### 2026-09-10 14:19 CST — H2b组合证据第一检查点

- Git：`phase4-factor-develop`；提交`f63bcf2`已创建，本条Progress随后提交并一并推送。
- 修复：日频组合开盘容量不再读取当日最终volume，固定使用前一完整交易日成交量；baseline/stress参与率与`ResearchLedger`统一为1%/0.5%。新增反例证明当日巨量不能覆盖前日零量并制造成交。
- 协议：`ManualDailyFactorBundleV2`新增排除cost scenario的`strategy_core_hash`，baseline/stress共享core且保持各自bundle hash。`StrategyPromotionEvaluation`新增上一条通过评价引用和自身评价hash，形成追加式晋级链；SQLite旧库增加兼容列。
- 文档：新增[`日频组合证据合同v1`](docs/research-runs/manual-daily-portfolio-evidence-v1.md)，冻结输入manifest、公司行动/cohort会计、共享容量、全量intent/attempt、基准、指标、12项输出、独立重放和artifact pair resolver标准。
- 验证：当前macOS工作区后端全量`763 passed, 2 warnings`；组合/H2b专项`10 passed`；compileall、关键错误级Ruff、`git diff --check`和变更Markdown链接检查通过。
- 边界：旧`manual-daily-portfolio-v2`仍是不可晋级工程模拟。H2b下一步必须提取cohort-aware原子成交和共享容量池，接入历史资格与公司行动子账，再保存全量intent、benchmark和独立replay；当前没有release可达到`portfolio_passed`。
- 入口：[`H2b记录`](docs/manual-daily-trading-implementation-plan.md#30-h2b组合证据第一检查点)、[`组合证据合同`](docs/research-runs/manual-daily-portfolio-evidence-v1.md)、[`证据解析器`](server/services/manual_evidence.py)。

### 2026-09-10 14:07 CST — H1/H2a远端同步

- Git：`phase4-factor-develop`已将`fb92672..96160d3`推送到`origin/phase4-factor-develop`，包含H1加固、审查记录、H2a研究证据解析器及对应Progress；此前两条记录中的“尚未推送”是阶段当时状态，本条确认远端已更新。
- 边界不变：当前最高只允许数据库证据resolver把release推进到`research_passed`；组合、holdout、真实前瞻及人工执行门仍保持关闭。

### 2026-09-10 14:05 CST — H2a研究证据锚定与晋级解析器

- Git：`phase4-factor-develop`；H2a提交`03ddc4e`已创建，本条Progress随后单独提交。连同H1，本地分支尚未推送，领先`origin/phase4-factor-develop`。
- 修改：新增`ResearchEvidenceArtifact`和`StrategyPromotionEvaluation`追加表及`manual_evidence.py`深模块。日频训练/验证worker完成后登记受控目录内三份Parquet与报告的路径、大小、逐文件SHA-256、身份hash和总证据hash；解析器重新核验数据库候选/实验、表达式、manual label、数据版本、policy、时间切分、报告语义及落盘字节，使用状态CAS原子追加晋级决定。备份协议升级为`manual-backup-v2`并覆盖新证据表。
- 接口：新增证据绑定release创建、release列表/详情/证据和基于artifact ID的promotion接口。客户端不能提交复制的指标或`passed`布尔值；旧`promote_release(..., evidence=...)`状态提升入口已关闭。`draft → research_passed`已有严格resolver，其余阶段证据不足时追加blocked evaluation并保持release原状态。
- 验证：当前macOS工作区后端全量`762 passed, 2 warnings`；H2a及相关专项`15 passed, 1 warning`；compileall、关键错误级Ruff、`git diff --check`和变更Markdown链接检查通过。warning仍为Starlette TestClient弃用提示和既有撮合截断提示。
- 边界：H2尚未整体完成。现有manual portfolio缺公司行动、基准、完整容量分母、独立账务重放和可信manifest，因此resolver明确阻断`portfolio_passed`；holdout经济结果和pilot daily checkpoint也尚未接入。当前release最多只能由新resolver达到`research_passed`，没有人工执行资格。
- 入口：[`H2a实施记录`](docs/manual-daily-trading-implementation-plan.md#29-h2a研究证据解析记录)、[`建设审查H2状态`](docs/research-runs/manual-daily-implementation-audit-2026-09-10.md#h2a-实施状态2026-09-10)、[`证据解析模块`](server/services/manual_evidence.py)。

### 2026-09-10 09:40 CST — 远端日频闭环大审查与H1加固

- Git：`phase4-factor-develop`从`5c225a4`安全快进到远端`fb92672`，只审查其日频人工闭环建设；H1修复提交为`92ddb68`，本条Progress随后单独提交。当前本地提交尚未推送，分支领先`origin/phase4-factor-develop`。
- 审查：对`5c225a4...fb92672`的M1—M9新增代码并行核对研究/晋级、计划/账本、API/调度/前端。实际复现单日创建两个45%批次、同票跨批误卖、自由成交绕过授权、跨服务提前commit、成交更正后状态漂移、未来30日pilot即时通过、未对账review仍ready等问题；完整证据和遗留见[`日频建设审查`](docs/research-runs/manual-daily-implementation-audit-2026-09-10.md)。其余远端新增不纳入本轮审查或修改。
- 修复：每个信号日只创建一个交替sleeve，计划和账本卖出均按cohort隔离；到期退出不被blocked/reconcile取消。人工计划成交、授权激活、账本和计划状态改为顶层单事务；成交携带cohort，幂等冲突和更正链严格校验并重算item/plan/cohort。通用API拒绝无计划经济成交，前端改为查看计划、确认项后绑定回填。同步加固日频label字段、回测缺价/退出重试/ST停牌容量、晋级回撤/冻结证据/顺序门、授权竞争和有效期、holdout重封、同日对账review、上海交易日、bundle CLI往返、备份scope及API测试依赖。
- 验证：当前macOS工作区后端全量`759 passed, 2 warnings`；日频专项`46 passed, 1 warning`；前端`15 passed`且`vue-tsc`、Vite构建通过，保留ECharts核心557.93KB构建提示；compileall、关键错误级Ruff、`git diff --check`和变更Markdown链接检查通过。两条warning分别为Starlette TestClient兼容弃用提示和既有撮合截断提示。
- 边界：当前仍无可人工执行策略。日频组合回测缺公司行动/历史资格/manifest同等级证据；release/pilot缺数据库证据resolver；worker无生产handler和数据库级CAS；闭环API与统一幂等记录未补齐；日损失/回撤尚未接入成交前门；没有真实30日观察或用户真实成交。后续严格按审查文档H2—H6推进。
- 入口：[`实施方案及H1记录`](docs/manual-daily-trading-implementation-plan.md#28-2026-09-10远端建设审查与h1加固记录)、[`日频建设审查`](docs/research-runs/manual-daily-implementation-audit-2026-09-10.md)、[`人工执行领域语言`](CONTEXT.md)。

### 2026-09-09 — M8前瞻门禁加固

- 施工：M8终结报告现在必须注入经验证的交易日历，并严格要求观察日等于pilot启动日之后的连续30个交易日；观测少于30天、缺日或跨日后补齐均不能通过。API在终结前重新检查日历覆盖；`datetime`输入按其日期处理，日历不可用显式失败关闭。
- 施工：P0/P1、日亏损次数和最大回撤等报告证据的数字解析改为显式校验，格式错误返回`ManualPilotError`，不再泄漏为未处理异常；负的日亏损门槛也被拒绝。
- 验证：`python -m pytest tests/test_manual_pilot.py -q`为`4 passed`；相关`manual_pilot.py`、`manual_trading.py`和测试文件`compileall`通过。
- 边界：本次仍未伪造30个真实前瞻交易日，也未连接券商、发送订单或声称用户已完成首笔人工成交；M8真实前瞻与M9用户侧验收仍需真实时间和用户回填事实。Windows/macOS兼容设计继续只使用Python标准库路径/时间处理及现有跨平台数据库接口，未做macOS实机运行宣称。
- Git：本记录与M8加固代码待本阶段提交并推送；既有`docs/README.md`修改及未跟踪审查/输出目录继续不纳入本提交。

### 2026-09-09 23:51 CST — M6—M9远端同步补记

- 事实更正：M9提交`ba8842a`的push已成功，`origin/phase4-factor-develop`当前与本地`HEAD`同为`ba8842a38c11a1bc5146d2302b40ba3eef5ebbe7`；该提交链包含M6 `1d98974`、M7 `981a7d9`、M8 `2f95342`和M9 `ba8842a`，此前各阶段记录中的网络失败状态不再是当前远端状态。
- 未改动：`.playwright-cli/`、`output/`、`tmp_*`及本阶段之外的`docs/README.md`和`docs/research-runs/tradingagents-subjective-analysis-review.md`继续按接手时状态保留，未纳入本次同步补记提交。

### 2026-09-09 23:51 CST — 外部 TradingAgents 主观分析框架评估

- Git：`phase4-factor-develop`；记录时`HEAD=ba8842a`，与`origin/phase4-factor-develop`同步；`PROGRESS.md`、`docs/README.md`和本专题文档为本次未提交修改，既有未跟踪目录继续保留。本次只新增文档，没有创建提交或推送。
- 修改：新增[`TradingAgents/TradingAgents-CN专题评估`](docs/research-runs/tradingagents-subjective-analysis-review.md)，覆盖两个仓库的完整Git跟踪文件清单与目录分类、核心分析/数据/记忆/输出/纸面链路、测试和许可证边界；同步将专题入口加入[`文档地图`](docs/README.md)。没有修改代码、数据、策略协议、研究结果或实盘能力。
- 结论：上游`TradingAgents`的多角色分析、反方论证、风险复核和报告组织可作为`kk_quant`的主观研究复核层；必须改接`kk_quant`冻结as-of快照、manifest/哈希、结构化`SubjectiveReview`和fail-closed状态。CN版适合选择性参考中国数据适配和展示，不整体引入`app/`、`frontend/`、Chroma记忆、默认目标价/默认Hold信号解析或纸面订单链路。两个项目都不能直接生成量化交易动作、仓位或订单。
- 验证：两个临时审计副本的Python文件均通过`compileall`；上游全量pytest因当前环境缺少`langchain_core`、`langgraph`、`yfinance`、`typer`等依赖，在收集阶段出现45个导入错误；CN版pytest收集到79个测试并出现28个收集错误，其中包含缺少`GOOGLE_API_KEY`后主动`SystemExit(1)`的环境检查脚本。上述结果仅记录当前机器可执行性，未被解释为生产正确性证明。
- 边界：本次没有调用真实LLM、真实券商或下单接口；没有把外部项目测试数字、README能力或纸面成交描述为`kk_quant`已验收能力。后续若实施，仍须经过数据/PIT、结构化输出、审计、重复性、量化策略单独对照和真实开始后的前瞻观察门；当前M8/M9和既有Phase 4剩余门禁不变。
- 入口：[`专题评估`](docs/research-runs/tradingagents-subjective-analysis-review.md)、[`文档地图`](docs/README.md)、[`人工日频实施方案`](docs/manual-daily-trading-implementation-plan.md)。

### 2026-09-09 23:48 CST — M9首次人工执行与持续循环门禁

- Git：`phase4-factor-develop`；M8本地提交`2f95342`已生成但push因GitHub `443`连接失败；M9修改尚未提交。M6/M7 push同样待网络恢复，既有未跟踪目录未触碰。
- 修改：新增`ManualExecutionConfirmation`和`manual_execution_cycle.py`；人工计划项现在必须先由用户确认，随后才能通过计划绑定的回填接口记录成交；首笔成交后仅将已批准授权激活，成交数量不得超过计划数量或授权单笔限额，部分成交/未成交会更新计划状态，A股T+1和账本哈希重放继续由M2服务负责。新增授权创建/批准、计划项确认、确认成交、未成交和授权查询API；无订单提交路径。
- 验证：M9首笔闭环专项`1 passed`；Ruff、compileall通过。测试确认未确认的计划项不能成交，首笔`user_reported`成交会激活授权、完成计划并写入A股影子持仓。
- 边界：测试只验证工程闭环，不代表用户已在券商侧真实买入；系统没有券商认证、订单发送、真实资金转移能力，M9真实验收必须由用户自行创建授权、在券商端逐项执行并回填，且对账/日终复盘无异常。
- 入口：[`M9实施记录`](docs/manual-daily-trading-implementation-plan.md#26-m9实施记录)、[`执行循环服务`](server/services/manual_execution_cycle.py)、[`授权与确认API`](server/api/manual_trading.py)。

### 2026-09-09 23:46 CST — M8三十日真实前瞻纸面观察框架

- Git：`phase4-factor-develop`；M7本地提交`981a7d9`已生成但push因GitHub连接重置失败；M8修改尚未提交。M6仍为本地提交`1d98974`，远端同步待网络恢复后重试；既有未跟踪目录继续保留。
- 修改：新增`ManualProspectivePilot`和`ManualPilotObservation`，以及`manual_pilot.py`和人工API的pilot创建、逐日观察、终结报告接口。观察目标天数强制为30个交易日，真实前瞻数据的`received_at`必须晚于pilot启动日，输入/信号hash、每日对账、研究/组合/holdout/replay/risk/data-health证据和P0/P1、回撤、日亏损门缺一不可；合成/回放明确只能工程验收，不能生成通过状态。pilot报告不可变且带报告hash，备份范围同步覆盖pilot证据。
- 验证：M8 pilot专项`2 passed`；连同M7/M6人工域专项`9 passed`；Ruff、compileall通过。测试中的30日生成只用于验证状态机，不代表现实市场已完成30日观察。
- 边界：当前日期没有被伪造为30日真实前瞻观察完成；策略release不会因pilot自动晋级，必须由真实开始日之后到达的数据和独立证据满足原方案门禁。M9授权/计划确认/首笔成交代码仍未提交，后续单独完成并推送。
- 入口：[`M8实施记录`](docs/manual-daily-trading-implementation-plan.md#25-m8实施记录)、[`前瞻pilot`](server/services/manual_pilot.py)、[`pilot API`](server/api/manual_trading.py)。

### 2026-09-09 23:31 CST — M7持久worker、备份与日终复盘

- Git：`phase4-factor-develop`；M6本地提交`1d98974`已生成但两次push均因GitHub `443`连接失败未确认远端同步；M7修改尚未提交。既有未跟踪目录继续保留。
- 修改：新增`ManualDailyJob`持久任务、lease/heartbeat/过期恢复/重试/阻断/幂等队列；新增跨平台Path原子写入、SHA-256内容寻址的人工表备份/空库恢复/校验；新增用户报告估值、现金流中和日终收益、执行偏差、对账阻断、研究修订队列和公司行动事实表。worker脚本为短周期可恢复进程，未配置显式handler的工作会失败关闭，不生成虚假行情/成交，不连接券商。人工API和操作台补充估值、复盘、研究修订、公司行动和任务只读入口。
- 验证：M7调度/备份/复盘专项`6 passed`；连同HTTP回归共`7 passed`；前端测试`15 passed`；`npm run build`、Ruff、`compileall`通过。现金流中和测试确认首日开户余额不被误算为次日外部入金。
- 边界：M7只提供持久化和人工事实处理，不自动估值、不自动交易；M8仍需真实开始后到达的30个交易日数据和不可变观察报告，M9仍需用户实际创建授权、在券商侧操作并回填首笔成交。M6/M7提交仍待本阶段结束后推送重试。
- 入口：[`M7实施记录`](docs/manual-daily-trading-implementation-plan.md#24-m7实施记录)、[`持久worker`](server/services/manual_scheduler.py)、[`备份恢复`](server/services/manual_backup.py)、[`日终复盘`](server/services/manual_review.py)。

### 2026-09-09 23:10 CST — M6人工执行HTTP接口与操作台

- Git：`phase4-factor-develop`；M1—M5提交链已在本阶段开始前确认同步到`origin/phase4-factor-develop`，当前M6修改尚未提交；既有`.playwright-cli/`、`output/`、`tmp_*`未跟踪目录继续保留。此前M5/M4/M3记录中的“待提交/待推送”是当时快照，本记录确认其后已由`c9a13b0`统一推送。
- 修改：新增`/api/v1/manual-trading`操作员保护接口，覆盖人工账户、现金事实、用户成交回填、追加式成交更正、账户对账、计划查看；新增人工执行Vue操作台、类型和幂等/错误工具，接入路由、导航和操作员令牌。账户创建幂等键落库；人工接口统一返回`manual_execution=true`、`broker_connected=false`、`live_order_submission=false`，没有提交券商委托路由；人工状态中的Decimal以字符串输出，避免前端精度漂移。
- 验证：人工账本与HTTP专项`5 passed`；前端Node契约测试`15 passed`；`npm run build`通过；Ruff、`compileall`通过；Playwright真实页面在桌面、390px、320px检查通过，320px下`scrollWidth=320`且控制台错误为0。M6截图保存在既有未跟踪目录`output/playwright/`，未纳入代码提交。
- 边界：M6只允许用户回填券商侧已发生的资金/成交事实，不能认证券商、发送订单或把回填事实伪装为broker确认；M7持久worker/备份/日终复盘、M8真实前瞻30日观察、M9首次人工购入仍未完成。后端全量回归将在M6提交后及后续阶段收口时再运行。
- 入口：[`M6实施记录`](docs/manual-daily-trading-implementation-plan.md#23-m6实施记录)、[`人工HTTP接口`](server/api/manual_trading.py)、[`人工操作台`](web/src/views/ManualTrading.vue)。

### 2026-09-09 22:59 CST — M5日决策、双cohort与持久计划

- Git：`phase4-factor-develop`；最新本地提交`be2be68`，M4 push仍因网络连接重置未同步；M5尚未提交。已有未跟踪目录未触碰。
- 修改：新增`DailyDecision`、`ManualCohort`、`ManualExecutionPlan/Item`模型；新增数据就绪、风险前置检查、日决策revision、T+1/T+2双cohort和纯计划持久化/标记已读服务。计划落库只写`manual_*`，买入只依赖确认现金，买卖项按顺序保存；标记已读不会改变影子现金/持仓。
- 验证：M5集成专项`2 passed`，Ruff通过；M5后的后端全量尚未再次运行。
- 边界：M5尚未提供HTTP/API和前端操作台、worker/备份/复盘、30日前瞻纸面和首次人工执行；M2—M4提交仍待网络恢复补推。live路径仍`can_submit_live=false`。
- 入口：[`M5实施记录`](docs/manual-daily-trading-implementation-plan.md#22-m5实施记录)、[`计划持久化`](server/services/manual_plan_persistence.py)、[`人工决策`](server/services/manual_decision.py)。

### 2026-09-09 22:51 CST — M4发布晋级、holdout与人工授权

- Git：`phase4-factor-develop`；最新本地提交`1e59cfd`，包含M3；M2和M3 push均因网络连接GitHub失败，未确认远端同步；M4尚未提交。已有未跟踪目录未触碰。
- 修改：新增`StrategyRelease`、`ManualExecutionAuthorization`、`ResearchHoldoutWindow/Access`模型；新增发布证据门、`ManualDailyPromotionPolicyV1`、sealed→opened→completed/invalidated holdout生命周期、账户级限额授权、用户批准/首笔成交激活/撤销和审计。发布与授权分离，manual_ready要求研究、组合、holdout、30日观察证据全部满足，历史paper结果不自动继承。
- 验证：M4专项`7 passed`；Ruff通过。M4后的后端全量尚未再次运行。
- 边界：M4尚未接入日决策/计划、HTTP/UI、worker/备份/日终复盘、30日真实前瞻观察和首次人工成交；本地三个阶段提交待网络恢复后补推。live路径仍关闭。
- 入口：[`M4实施记录`](docs/manual-daily-trading-implementation-plan.md#21-m4实施记录)、[`晋级服务`](server/services/strategy_promotion.py)、[`授权服务`](server/services/manual_authorization.py)、[`holdout服务`](server/services/research_holdout.py)。

### 2026-09-09 22:44 CST — M3日频标签、策略包与双批组合回测

- Git：`phase4-factor-develop`；M2本地提交为`6f8c4db`，两次push均因外网连接失败未同步远端；本条M3尚未提交。已有`.playwright-cli/`、`output/`、`tmp_*`未跟踪目录继续保留。
- 修改：新增独立`ManualDailyFactorBundleV2`和原价输入的`run_manual_daily_portfolio`及CLI；固定收盘信号、T+1开盘进入、T+2收盘退出、最多双cohort、每批45%、卖出全部持仓、baseline/stress费用和确定性结果哈希，数据/日历不完整时阻断。因子实验身份新增完整`label_spec`并加入SQLite旧库加列迁移，未改写v1 bundle或旧研究结果。
- 验证：M3专项5项及因子/研究回归71项通过；M2后的后端全量尚未再次运行。代码仍无券商调用，组合结果标记`research_only=true`、`paper_authorized=false`、`auto_submit=false`。
- 边界：M3未实现发布晋级/holdout/授权、日决策计划、HTTP/UI、worker/备份/复盘、30日观察和首次人工成交；M2 push待网络恢复后补推。Windows/macOS继续只使用Python、Path、Parquet/SQLite等跨平台接口，尚未在两套系统分别运行。
- 入口：[`M3实施记录`](docs/manual-daily-trading-implementation-plan.md#20-m3实施记录)、[`日频bundle`](quant_engine/factor/manual_daily_bundle.py)、[`组合回测`](quant_engine/backtest/manual_daily_portfolio.py)。

### 2026-09-09 22:36 CST — M2人工账户与可重放影子账本

- Git：`phase4-factor-develop`；基于远端最新 `5c225a4` 开发。本条记录与M1、M2代码尚未提交或推送；接手前已有的 `.playwright-cli/`、`output/`、`tmp_*` 未跟踪目录保持原样。
- 修改：新增 `manual_account`、`manual_execution_event`、`manual_cash_event`、`manual_ledger_event`、`manual_position_lot`、账户/持仓快照、对账模型；新增 `server/services/manual_ledger.py`。人工成交和现金只接受 `user_reported`，按账户顺序追加哈希链，使用 Decimal/Numeric，成交后重放现金和批次，A股买入要求注入已验证交易日历并执行T+1，卖出按可用批次支持零股清仓；幂等键、券商成交引用、费用分项、追加式更正/冲正和对账状态已实现。人工链不调用 `paper_trading`，不写入 `Paper*` 表。
- 验证：M2定向测试 `5 passed`；M2模型编译和 Ruff 通过。新增 SQLite 旧库迁移测试覆盖 `init_db()` 的新表创建和既有字段追加；尚未运行M2后的后端全量回归。
- 边界：M2尚未实现公司行动、HTTP/UI、策略发布/晋级、holdout、日决策/计划、worker、备份和30日观察；仍无券商认证、下单或真实资金路径。Windows/macOS代码设计继续不依赖平台专属路径、shell或系统API，尚未在两套系统分别运行。
- 入口：[`M2实施记录`](docs/manual-daily-trading-implementation-plan.md#19-m2实施记录)、[`人工账本服务`](server/services/manual_ledger.py)、[`人工执行领域语言`](CONTEXT.md)。

### 2026-09-09 22:16 CST — M1规则注册与纯计划模块

- Git：已从远端最新 `origin/phase4-factor-develop` 切换并保持同步；当前 `HEAD=5c225a4`，工作树含本轮未提交修改及接手前已有的未跟踪目录，未提交、未推送。
- 修改：新增 `quant_engine/trading/manual_protocol.py`、`quant_engine/factor/manual_daily_label.py`、`quant_engine/trading/effective_rules.py` 和 `server/services/manual_planning.py`；实现 `manual-daily-label-v1` 的交易日步进与 `T+1 open → T+2 close` 标签、两批45%人工策略协议对象、稳定哈希、按市场/板块/生效日期解析规则、A股买入整手/卖出零股清仓、卖出优先、只使用已确认现金的纯计划、刷新/版本修订和状态优先级/前置检查。`paper_market_rules.py` 改为调用独立的历史纸面费用适配器，保留旧纸面卖出印花税0.001口径，不改写既有纸面结果。
- 验证：M1专项 `17 passed`；既有纸面交易与跨市场回归 `23 passed`；后端全量 `717 passed, 83 warnings`；Python compileall、Ruff和`git diff --check`通过。警告为既有 matcher 用户警告及研究图表中文字体缺字提示。
- 边界：本轮没有建数据库表、实现影子账本、HTTP、前端、worker、晋级/holdout或人工成交回填，也没有新增真实券商能力；M2及后续阶段尚未开始。计划对象不代表已成交，`auto_submit`仍被拒绝。Windows/macOS兼容性通过不使用平台专属路径、shell或系统调用保持，尚未在两套系统分别运行。
- 入口：[`M1实施记录`](docs/manual-daily-trading-implementation-plan.md#18-m1实施记录)、[`人工执行领域语言`](CONTEXT.md)、[`文档地图`](docs/README.md)。

### 2026-09-09 19:00 CST — 冻结A股日频人工执行工程方案

- Git：`phase4-factor-develop`；工程方案提交 `7beb7d9` 已创建，本条Progress记录随后的文档提交一并推送到 `origin/phase4-factor-develop`。
- 方向：真实交易采用日频决策和用户券商端人工买入/卖出/撤单，系统只负责研究晋级、确定性计划、风险把关、用户成交回填、影子账本、对账和日终复盘。默认首版为T日收盘信号、T+1开盘进入、T+2收盘退出、两个最多45%权益批次滚动；修改该协议必须新建策略版本。
- 修改：新增根目录人工执行领域语言和M0—M9实施方案；精确定义日频标签、研究/执行循环隔离、发布与账户授权双门、不可变计划版本、追加式用户执行事件、定点数哈希账本、公司行动、资金事件、幂等API、持久worker、前端操作台和分阶段验收。`AGENTS.md`与文档地图已加入强制阅读入口。
- 验证：三路只读代码/研究审查后修正双批`hold`、卖出资金、计划刷新、成交更正、holdout和任务恢复语义；`git diff --check`通过，4个变更Markdown文件没有新增断链，9项关键契约结构检查全部通过。既有代码事实已核对：现有factor bundle仅接受周/月频，纸面提交会即时生成模拟成交，纸面与研究印花税口径存在差异。本轮没有业务代码变化，未运行后端或前端测试。
- 边界：M0仅冻结可施工方案，人工执行链尚未实现；现有5日反转和量价背离未通过成本稳健性，振幅压缩仍为filter，当前没有策略具备人工执行资格。2015、2019—2022、2023—2026.08均已打开，不能伪装成新sealed holdout。
- 入口：[`人工日频实施方案`](docs/manual-daily-trading-implementation-plan.md)、[`人工执行领域语言`](CONTEXT.md)、[`组合回测反例`](docs/research-runs/factor-portfolio-validation-v1.md)、[`文档地图`](docs/README.md)。

### 2026-09-09 18:20 CST — 提交并推送P0—P2累计进度

- Git：`phase4-factor-develop`，功能快照提交 `633908e` 已推送到 `origin/phase4-factor-develop`；本条Progress记录随后单独提交并推送。
- 提交范围：文档治理与跨对话记录、研究正确性修复、受限因子表达式与持久实验、自动模板生成、训练/验证门、结构化memory、冻结策略bundle、事件驱动成本组合回测和因子研究前端，共62个文件。
- 验证基线：后端`700 passed, 1 warning`；前端`13 passed`、TypeScript与Vite构建通过；390px浏览器验收、compileall、文档链接、JSON解析和`git diff --check`通过。
- 边界：本地数据库和`backtest_result/`未进入Git；可移植组合结果已保存到`docs/research-runs/factor-portfolio-validation-v1.json`。实盘继续关闭，因子候选没有观察或交易授权。
- 入口：[`因子工作流`](docs/factor-research-workflow.md)、[`组合回测结果`](docs/research-runs/factor-portfolio-validation-v1.md)、[`文档地图`](docs/README.md)。

### 2026-09-09 14:12 CST — P2 冻结因子策略与成本组合回测

- Git：`phase4-factor-develop` @ `55e5156`；累计P0—P2与文档治理改动仍在工作树中，尚未提交或推送。
- 修改：动态因子进入Strategy Protocol和`ResearchBacktestEngine`；逐历史日应用上市/ST/停牌/退市资格。新增不可变`FrozenFactorStrategyBundle`，显式绑定training/validation证据、产物/数据/代码哈希与组合规则；阶段排队进一步要求同数据、持有期、门政策且前后区间不重叠。新增因子研究前端，支持候选、生成、实验、门禁、memory和retry。
- 验证：后端全量`700 passed, 1 warning`；前端`13 passed`、TypeScript与Vite构建通过，ECharts核心557.93KB提示保留；compileall、文档链接和`git diff --check`通过。浏览器实测`/factors`正确展示5个候选、9个实验和训练memory，390px视口无横向溢出、8项底部导航可见，控制台无错误。
- 真实组合回测：只运行rank角色的5日反转和量价背离，固定2019—2022、周频Top50、90%仓位、baseline/stress。反转累计−16.53%/−57.46%；量价背离8.97%/−36.82%；年化换手56—74倍，均未通过成本稳健性，不晋级。filter角色的振幅压缩未被错误包装。
- 审计：四条曲线的现金+市值+应收=总权益、逐日复利重建、bundle/summary/report哈希一致；全部`validated=false`、`paper_authorized=false`。未读取2023—2026.08作为新holdout，也未改写既有36组结果。
- 边界：尚无模型候选Adapter和真正新holdout；组合结果尚未在前端展示。现有因子通过只用于反例研究，不创建观察或交易策略。
- 入口：[`组合回测结果`](docs/research-runs/factor-portfolio-validation-v1.md)、[`因子工作流`](docs/factor-research-workflow.md)、[`前端记录`](docs/frontend-iteration-log.md)。

### 2026-09-09 11:55 CST — P1 自动候选生成与独立验证

- Git：`phase4-factor-develop` @ `55e5156`；累计快速迭代改动仍在工作树中，尚未提交或推送。
- 修改：新增 `FactorCandidateGenerator` 接口和无密钥 `template-v1` Adapter；生成器结合训练memory与已登记表达式去重。实验增加`training/validation/holdout`阶段，validation要求`training_passed`，holdout要求`validation_passed`，memory在查询层排除所有非训练结果。
- 验证：后端全量 `696 passed, 1 warning`，compileall与`git diff --check`通过；生成API、阶段迁移和运行服务正常。
- 真实训练：自动生成4个候选并完成2015全年500股训练。5日反转、量价背离和振幅压缩通过；低波与现有波动基线相关性1.0且方向后半段翻转，被训练门拒绝。
- 独立验证：3个训练通过候选在2019-01-02—2022-12-30完成966个有效IC截面，全部通过同一门禁。方向IC分别为反转0.03264、量价背离0.03438、振幅压缩0.03073；当前只允许进入事件驱动组合回测。
- 边界：2023—2026.08数据此前已经打开，本轮没有运行或伪装成新候选holdout；结果仍未计入组合换手、费用、容量和公司行动。模型生成Adapter、策略包装和前端实验台尚未完成。
- 入口：[`受限因子研究工作流`](docs/factor-research-workflow.md)、[`自动因子路线`](docs/research-runs/forum-502342-factor-mining-review.md)。

### 2026-09-09 11:44 CST — P1 因子训练门与结构化 memory

- Git：`phase4-factor-develop` @ `55e5156`；累计快速迭代改动仍在工作树中，尚未提交或推送。
- 修改：新增可冻结的 `FactorGatePolicy`；实验计算覆盖率、Rank IC、方向IC、前后半段稳定性、与20日动量/波动/换手基线的逐日pairwise相关性和五分组收益；保存IC/分层Parquet、独立gate结果和候选拒绝原因；新增训练memory接口，旧v1完成记录按`legacy_evaluated`兼容。
- 验证：后端全量 `694 passed, 1 warning`，compileall和`git diff --check`通过；新数据库字段已由启动迁移加载，服务保持scheduler关闭。
- 真实运行：实验 `3e396094a009` 完成2015全年500股训练门。121,756行中96,191个有限值，覆盖率79.00%，238个IC截面；覆盖与样本数通过，方向IC−0.09516、基线相关性1.0、两半方向均负、Q5−Q1均值−1.597%，四门失败，正确输出`training_rejected`。
- 边界：默认阈值是显式训练政策而非通用定律；t/p值仍未调整重叠标签和多重尝试。没有模型生成Adapter、独立验证队列、成本/容量策略回测或自动晋级。本轮无前端代码变化，未重复前端构建。
- 入口：[`受限因子研究工作流`](docs/factor-research-workflow.md)、[`自动因子路线`](docs/research-runs/forum-502342-factor-mining-review.md)。

### 2026-09-09 11:31 CST — P1 受限因子实验基础闭环

- Git：`phase4-factor-develop` @ `55e5156`；P0/P1代码及文档治理仍在工作树中，尚未提交或推送。
- 修改：新增 `FactorExpressionSpec` JSON AST与白名单解释器，递归推导预热并限制字段/算子/复杂度；新增 `factor_candidate/factor_experiment` 持久记录、表达式与实验去重、worker租约/接管、失败记录和显式retry；新增候选/实验HTTP入口及CLI worker，产物按attempt不可覆盖保存。
- 验证：后端全量 `692 passed, 1 warning`，新增因子/评价专项30项通过，compileall和`git diff --check`通过；API可列出候选和实验，OpenAPI包含5个因子研究路径，前后端继续返回HTTP 200。
- 真实运行：工程候选 `engineering_lagged_momentum_20` 已绑定 `normalized-v2`，完成2015-01-05—2015-06-30的500股/5日标签实验；输出59,381行、48,892个有限值，覆盖率82.34%，产物位于本机 `backtest_result/factor-experiments/ceeaf8ad8630/`。短区间Rank IC均值−0.09233，仅作链路验证，状态固定为 `evaluated_not_promoted`。
- 边界：当前没有模型候选生成Adapter、训练反馈memory、相关性/分层/成本晋级门或正式因子策略回测；IC显著性未调整重叠标签和多重搜索，不能据此宣称候选有效。本轮无前端代码变化，未重复前端构建。
- 入口：[`受限因子研究工作流`](docs/factor-research-workflow.md)、[`自动因子路线`](docs/research-runs/forum-502342-factor-mining-review.md)。

### 2026-09-09 10:27 CST — P0 研究正确性快速迭代

- Git：`phase4-factor-develop` @ `55e5156`；本轮代码和前一轮文档治理均在工作树中，尚未提交或推送。
- 修改：修复因子中性化的截距与非法市值语义；旧 `SimulationAccount` 快照新增锁仓批次持久化和同日幂等替换；`ResearchDataPortal` 接通非基本面面板因子，隔离研究策略可以声明 factor requirement。
- 验证：后端全量 `682 passed, 1 warning`，Python compileall 与 `git diff --check` 通过；真实 `normalized-v2` 500股截面在2026-08-31返回500行、463个有限因子值，均值约0、总体标准差约1；本地 SQLite 已增加 `paper_position.unlock_date/locked_lots`，重启后的健康接口返回正常。
- 边界：PIT基本面因子继续阻断；本轮没有自动候选生成、持久实验队列或500股因子策略正式回测，也没有改写36组冻结研究结果。没有前端代码变化，因此未重复运行前端测试/构建。触及文件的 Ruff 扫描仍报告91项现存风格/格式债务，本阶段未将批量格式化混入功能修复。
- 入口：[`因子基线`](docs/research-runs/factor-materials-v1.md)、[`自动因子差距`](docs/research-runs/forum-factor-workflow-code-gap.md)、[`研究平台路线`](docs/research-platform-roadmap.md)。

### 2026-09-09 10:06 CST — 启动本地开发环境

- Git：`phase4-factor-develop` @ `55e5156`；文档治理改动仍在工作树中，本次没有提交或推送。
- 运行：FastAPI 已启动于 `http://127.0.0.1:8000`，Vue/Vite 已启动于 `http://127.0.0.1:5173`；为避免浏览项目时触发观察任务，后端使用 `QUANT_SCHEDULER_ENABLED=false`。
- 验证：健康接口返回 `status=ok`、版本 `0.2.0`；Strategy Protocol 2.0 返回 6 个策略；研究数据接口返回 normalized-v1/v2 两个归档；首页、数据、回测和模拟页面均返回 HTTP 200。
- 边界：这是当前 Mac 的本地开发进程，不是生产部署；终端会话退出或机器重启后需要重新启动。实盘能力仍为 `can_submit_live=false`、`paper_only=true`。
- 入口：前端 `http://127.0.0.1:5173/`，API 文档 `http://127.0.0.1:8000/docs`。

### 2026-09-09 09:42 CST — 建立跨对话项目记录机制

- Git：开始时为 `phase4-factor-develop` @ `55e5156`，与 `origin/phase4-factor-develop` 同步；本次文档修改尚未提交、未推送。
- 修改：新增 `AGENTS.md`、`PROGRESS.md`、`docs/README.md` 和中期状态审查；统一接手、文档分级、验证及 Progress 追加规则。
- 清理：`docs/project-status.md` 改为兼容入口，`CLAUDE.md` 改为 Agent 兼容入口；历史阶段文档标明其状态，避免旧分支、旧测试数和旧“当前”描述继续充当项目现状。
- 验证：后端全量 `678 passed, 1 warning`；前端 `10 passed`、`vue-tsc` 与 Vite 构建通过；49 份 Markdown 本地链接检查无断链，`git diff --check` 通过。数据/研究专项另有 125 项测试通过，冻结研究 manifest 的 4 个 Parquet SHA-256 均匹配。
- 边界：本次只做现状审查与文档治理，不修复审查中发现的业务代码问题。
- 入口：[`中期状态审查`](docs/project-state-audit-2026-09-09.md)、[`文档地图`](docs/README.md)。

## 新记录模板

```markdown
### YYYY-MM-DD HH:mm CST — 任务名称

- Git：分支、HEAD/工作树状态、提交与推送状态。
- 修改：最终发生的代码、配置、文档、数据协议或研究结论变化。
- 验证：实际运行的测试、构建、数据检查及结果。
- 边界：仍未完成或不能据此声称的能力。
- 入口：相关专业文档、研究产物或问题记录。
```
