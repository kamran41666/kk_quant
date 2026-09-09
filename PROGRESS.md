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
