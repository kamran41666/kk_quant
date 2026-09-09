# A股日频人工执行闭环实施方案

> 类型：active-implementation-plan
> 版本：manual-daily-v1
> 日期：2026-09-09（Asia/Shanghai）
> 用户决定：日频决策；所有真实买入和卖出均由用户在券商端人工完成；用户承诺严格按系统计划操作并回填实际结果
> 当前状态：方案已冻结，尚未实施；现有因子候选没有人工执行资格

## 1. 施工目标

交付以下闭环：

```text
候选因子
→ 训练/验证/组合回测
→ 策略版本晋级
→ 前瞻纸面观察
→ 用户批准人工执行授权
→ T日收盘生成日决策
→ T+1人工买卖并回填用户成交
→ 更新影子账本
→ 收盘后生成日终复盘
→ 次日保持/调仓/减仓/空仓/阻断/待对账
→ 循环
```

系统负责生成有证据的目标、风险检查、执行计划、成交回填、账本、对账和复盘；系统不登录券商、不保存券商凭证、不调用券商下单接口、不把用户成交标成券商API验证。人工执行涵盖买入、卖出、撤单和未操作，不仅是首次购入。

本方案只定义工程流程，不批准任何现有策略。当前5日反转和量价背离均未通过成本稳健性，振幅压缩仍是filter角色，全部保持研究状态。证据见 [`research-runs/factor-portfolio-validation-v1.md`](research-runs/factor-portfolio-validation-v1.md)。

## 2. 不可变决策

### 2.1 两个循环必须隔离

**执行循环**使用一个已批准、不可变的策略版本。每天的新行情只能令该版本输出新的目标状态或触发其冻结风险规则。

**研究循环**接收日终复盘产生的 `Research Revision`，可以修改表达式、权重、阈值、持有期、仓位或风控；任何修改都生成新策略版本并重新经过训练、验证、组合回测和前瞻观察。研究修订不能直接改变次日真实计划。

### 2.2 五个动作必须分离

1. 系统生成日决策。
2. 系统生成执行计划。
3. 用户确认已阅读计划。
4. 用户在券商端人工操作。
5. 用户回填成交后，系统更新影子账本。

生成、查看和确认计划均不等于已成交。用户成交、显式资金事件和有来源的公司行动是仅有的影子账本输入；禁止直接改写确认现金或持仓。

### 2.3 独立数据模型

人工执行链不得写入 `PaperOrder`、`PaperFill`、`PaperLot` 或纸面账户余额。现有 `paper_trading.submit_order` 会立即生成模拟成交，不能复用于用户成交。可以抽取其中的纯数量计算和风险校验，但新模块必须写入独立 `manual_*` 表。

### 2.4 首版范围

- 市场：`a-share`，仅项目当前研究引擎明确支持的沪深主板代码范围。
- 频率：每个交易日收盘后产生一次信号。
- 默认执行协议：`daily_two_sleeve_open_close_v1`。
- 目标总仓位上限：90%；每个执行批次预算为已批准总仓位的50%，默认每批最多45%账户权益。
- 默认时序：T日收盘信号；T+1开盘时段人工进入；T+2收盘时段人工退出该批次。
- 两个批次可以重叠，现金共享；系统使用交易日序号和cohort ID，不使用日历日期奇偶。
- 买入数量遵守系统登记的证券规则；首版主板按100股整手。卖出允许“可卖数量全部退出”产生零股清仓，不强制卖出数量为100股倍数。
- 真实费用以用户成交回填为准；计划费用只能使用带生效日期和来源的估算表。
- 真实资金上限、单笔上限、允许最大回撤和日损失停止线没有系统默认值，必须由用户在授权时显式填写。

调仓频率、两批持有和执行时段属于策略版本的一部分。修改其中任意一项都必须新建版本。

现有 `factor-experiment-v2` 使用T+1开盘到T+1+h开盘标签，现有 `FrozenFactorStrategyBundle` 只允许周频/月频；二者都不等于本方案。M1必须新增 `manual-daily-label-v1`：`signal=T close`、`entry=T+1 open`、`exit=T+2 close`。M3必须新建bundle v2并重新执行训练、验证和组合回测；现有`training_passed/validation_passed`和bundle v1只能作为历史参考，不能转成日频人工执行资格。

已打开区间固定记录：2015用于当前模板训练；2019—2022已用于因子验证和组合验证；2023—2026.08已用于既有36组研究。三者均不能重新命名为日频新版本的sealed holdout。新holdout必须在表达式、组合、执行、成本和晋级policy全部冻结后创建，记录`created_at/opened_at/opened_by/purpose`；打开后的收益不得返回候选生成或调参。

首版标签和批次日期必须由交易日历计算，不允许使用自然日加法：

```python
entry_date = next_trading_session(signal_date)
exit_date = next_trading_session(entry_date)
factor_label = adjusted_close[exit_date] / adjusted_open[entry_date] - 1
```

研究标签使用可复权的进入日开盘价和退出日收盘价；真实影子账本使用用户报告原始成交价，并以独立公司行动事件维护权益。连续交易日示例：

| 时点 | 批次A | 批次B | 当日收盘信号 |
|---|---|---|---|
| T0收盘 | 创建A，计划T1开盘进入、T2收盘退出 | 无 | 冻结A的目标证券和预算 |
| T1开盘 | 人工进入A | 无 | 无 |
| T1收盘 | A持有 | 创建B，计划T2开盘进入、T3收盘退出 | 冻结B的目标证券和预算 |
| T2开盘 | A持有 | 人工进入B | 无 |
| T2收盘 | 人工退出A | B持有 | 创建C；退出A与信号是否换股无关 |
| T3收盘 | 已关闭 | 人工退出B | 继续同样滚动 |

## 3. 领域语言

实现前完整阅读根目录 [`CONTEXT.md`](../CONTEXT.md)。代码、数据库、API、前端和测试统一使用其中的 `Strategy Release`、`Promotion`、`Manual Execution Authorization`、`Daily Decision`、`Execution Plan`、`User-Reported Fill`、`Cohort`、`Shadow Ledger`、`Daily Review` 和 `Research Revision`，不得用 `PaperOrder/PaperFill` 指代真实人工流程。

## 4. 每日确定性流程

### 4.1 T日收盘后

前置输入：已批准策略版本、人工执行授权、最后一次已对账账户快照、T日最终日线、交易日历、证券状态、公司行动状态和未完成计划。

按顺序执行：

1. `DataReadinessModule.check(T)` 验证日历、数据截止、OHLC、重复、停牌/ST、数据内容哈希和策略依赖；缺证据时返回 `blocked`。
2. `DailyDecisionModule.generate(T)` 使用已批准策略版本生成目标、风险状态和原因码。
3. `CohortModule.advance(T)` 识别T+1应进入的新批次、T+2应退出的旧批次和风险强制退出批次。
4. `ExecutionPlanModule.draft()` 根据确认现金、实际持仓、可卖批次和未完成计划形成下一执行日草案。
5. `draft`可以根据新报价或账户事实原地刷新；一旦变为`ready`即冻结`plan_hash`和全部输入引用。`ready/viewed`不得原地刷新，任何变化都创建新版本并把旧版标为`superseded`。

完成条件：同一授权、策略版本、信号日和计划时段只存在一个有效计划版本；重复调用返回同一记录。

### 4.2 T+1开盘执行

1. 执行前刷新原始可交易报价、停牌和涨跌停状态；前一日收盘参考价不能冒充实际开盘价。
2. `ManualRiskModule.preflight()` 重新检查授权、现金、可卖数量、整手、单笔/总仓位、价格偏离和未完成计划。
3. 系统展示卖出项、买入项、计划数量、参考价格/来源/时间、价格容忍区间、费用估算、原因和阻断项。
4. 用户将计划标为 `viewed`，在券商端人工操作。
5. 用户逐笔回填成交、部分成交、未成交、撤单或拒单。
6. 只有 `User-Reported Fill` 写入后，`ShadowLedgerModule.post()` 才改变现金、持仓和批次。

预计卖出收入不能提前成为可用现金。每一版买入计划数量只使用该版本创建时已经确认的现金；卖出成交回填释放现金后，系统必须创建新的买入计划版本、将旧版标为`superseded`，并要求用户重新查看。系统不能以“计划已确认”推定成交。

### 4.3 T+2收盘退出

到期批次独立于新信号处理。即使T+2没有新买入信号，旧批次退出计划仍存在。收盘执行前必须按4.2相同规则重新执行报价、停牌、涨跌停、可卖数量、授权和未完成计划检查。用户回填真实卖出；跌停、停牌或未成交时，批次进入 `blocked`/`exiting`，不得从账本删除，也不得把计划价格当成交价格。

### 4.4 日终复盘

`DailyReviewModule.build(T)` 必须包含：

- 数据和策略版本；
- 日决策及原因；
- 计划/成交数量、价格、费用和时间差异；
- 现金、持仓、可卖数量、批次和未完成计划；
- 当日/累计净值、回撤、实际换手和执行偏差；
- 当日资金事件及剔除外部现金流后的收益；
- 因子覆盖、排名变化、风险状态和数据健康；
- 次日动作：`hold/rebalance/reduce/flat/blocked/reconcile`；
- 是否创建 `Research Revision`。

复盘可以创建研究修订，但不得修改当前策略版本或已经冻结的次日计划。存在未确认成交、现金/持仓不平或重复流水号时，次日状态为 `reconcile`，停止新增买入。

## 5. 决策状态

冲突时采用固定优先级：`reconcile > blocked > flat > reduce > rebalance > hold`。高优先级状态决定当日新开仓权限，低优先级信号不能覆盖它。

| 动作 | 精确定义 | 计划行为 |
|---|---|---|
| `hold` | 目标证券和风险状态未变化 | 双批协议仍创建当日新批次并退出到期批次；同证券滚动也记录买卖、换手和费用 |
| `rebalance` | 数据有效，目标变化超过阈值 | 为新批次生成调整计划，保留未到期旧批次 |
| `reduce` | 冻结风险规则要求降低敞口 | 生成指定批次/证券卖出，禁止用研究反思临时触发 |
| `flat` | 策略或风险规则明确要求目标仓位为0 | 不创建新批次，并为授权管理的全部可卖批次生成退出项 |
| `blocked` | 数据、日历、授权、规则或报价证据不足 | 不生成新买入；保留真实持仓和待处理退出事实 |
| `reconcile` | 用户成交、现金、持仓或未完成委托尚未对平 | 阻断新买入，等待回填、更正或人工确认 |

`blocked`不是`flat`，数据错误不能静默变成空仓信号。`hold`只表示目标不变，不暂停冻结的批次生命周期。若要对相同证券跨批次净额抵消，必须创建新的执行policy和策略版本，并重新进行成本、风险和holdout验证。

## 6. 异常场景

| 场景 | 必须行为 |
|---|---|
| 日历或T日最终数据不可验证 | 日决策`blocked`，记录最后成功数据日和原因；不前填生成新买入 |
| 停牌 | 禁止假定买入或卖出；持仓保留，复牌后生成新计划 |
| 涨停买入/跌停卖出 | 计划标明不可保证成交；只有用户回填才能入账，不自动追价 |
| 部分成交 | 计划项派生状态为`partially_filled`，剩余数量保留；资金/持仓按已成交部分更新；`viewed`只表示用户已阅读 |
| 未成交/用户未操作 | 明确记录`unfilled/skipped`；计划到期后不得复制为新计划 |
| 重复成交流水号 | 幂等返回原记录；字段冲突则拒绝并进入`reconcile` |
| 错误回填 | 追加冲销/更正成交；原成交不可编辑或删除 |
| 买入零股 | 首版拒绝 |
| 全部卖出零股 | 若数量等于确认可卖余额则允许 |
| 手续费未知 | 成交可暂存为`fee_pending`，账户进入`reconcile`，不得生成新买入 |
| 分红/送转 | 按登记、除权、到账、红股上市分别记账，不把价格差当现金 |
| 配股/重整/差异化分红/退市 | 缺少用户权益和实际回款证据，或影响范围无法量化时默认阻断晋级并进入人工复核；不使用研究压力假设改真实账本 |
| 入金/出金/利息/后扣费用 | 追加`Cash Event`并进入对账；收益计算剔除外部现金流，禁止直接修改余额 |
| 策略或数据哈希变化 | 旧日决策和未执行计划`superseded`；需新策略版本或重新生成 |
| 授权暂停/撤销 | 停止新买入；是否退出已有持仓由冻结授权撤销策略决定并要求用户确认 |

到期exit因停牌、跌停或用户未成交而阻断时，批次继续存在并优先进入下一可交易收盘计划；在实际总敞口恢复到授权上限内之前禁止新增entry。

## 7. 模块与接口

### 7.1 `PromotionModule`

接口：

```python
promote(bundle, portfolio_runs, policy) -> PromotionDecision
freeze_release(decision, execution_policy, risk_policy) -> StrategyRelease
```

内部负责因子证据、组合回测、数据/代码哈希、前瞻观察和审批状态；调用方不需要知道各研究文件位置。失败返回逐门原因，不创建可执行版本。

### 7.2 `DailyDecisionModule`

接口：

```python
generate(release, signal_date, market_snapshot, account_snapshot) -> DailyDecision
```

必须是确定性纯计算；同一输入哈希得到同一输出哈希。任何模型生成的文字只能解释输出，不能改变目标、风险状态或原因码。

### 7.3 `DataReadinessModule`、`CohortModule`与`ManualRiskModule`

实现文件分别固定为`server/services/manual_data_readiness.py`、`server/services/manual_cohort.py`和`server/services/manual_risk.py`。接口：

```python
check(release, signal_date, data_manifest, calendar, security_states) -> ReadinessResult
advance(decision, trading_calendar, existing_cohorts) -> CohortTransitionSet
preflight(plan, authorization, account_snapshot, quote_snapshot) -> PreflightResult
```

`DataReadinessModule`只判断证据是否齐备，`CohortModule`只计算批次生命周期，`ManualRiskModule`只按冻结授权和规则表判断可执行性；三者不得修改账本或调用券商。

### 7.4 `ExecutionPlanModule`

接口：

```python
draft(decision, cohorts, account_snapshot, quote_snapshot) -> ExecutionPlan
refresh(draft_plan, quote_snapshot, account_snapshot) -> ExecutionPlan
revise(frozen_plan, quote_snapshot, account_snapshot, reason) -> ExecutionPlan
```

`refresh`只接受`draft`。`revise`创建新版本并将原`ready/viewed`版本标为`superseded`，调用方必须重新展示新计划。实现卖出优先、确认现金、整手、零股清仓、批次和价格证据。复用现有观察模块的纯sizing思想，但不调用模拟下单函数。

### 7.5 `ShadowLedgerModule`

接口：

```python
post(execution_cash_or_corporate_action_event) -> LedgerCheckpoint
correct(original_event_id, replacement) -> LedgerCheckpoint
reconcile(statement) -> ReconciliationResult
```

只接受用户成交、资金事件和明确公司行动。更正会追加原事件的等额反向事件，再追加完整替代事件；原事件不可改写。所有写入在账户级事务中分配递增序号并更新哈希链，物化现金和持仓必须可重建。

### 7.6 `DailyReviewModule`

接口：

```python
build(decision, plan, fills, ledger, market_snapshot) -> DailyReview
```

输出结构化指标、理由和下一动作；研究修订作为单独对象返回。

## 8. 数据模型

以下表新增到 `server/models/schema.py`，通过 `server/models/database.py` 进行SQLite加列/建表兼容。所有时间使用带时区的ISO 8601，交易日期使用Asia/Shanghai交易日；所有股数使用整数，所有价格、费用、现金、权益和比率使用`Numeric`定点数或整数最小货币单位，禁止经济字段使用`Float`。

### 8.1 `strategy_release`

| 字段 | 类型/约束 |
|---|---|
| `id` | String(36) PK |
| `version` | String(40)，与`strategy_key`唯一 |
| `strategy_key` | String(100)，索引 |
| `bundle_hash` | String(64)，索引；同一bundle允许绑定不同policy |
| `release_hash` | String(64)，唯一；覆盖bundle及研究、执行、风险、晋级policy全部哈希 |
| `strategy_fingerprint` | String(64) |
| `market` | 固定`a-share` |
| `research_evidence` | JSON Text，包含实验/组合/纸面观察ID与哈希 |
| `execution_policy` | JSON Text |
| `risk_policy` | JSON Text |
| `promotion_policy` | JSON Text |
| `status` | `draft/research_blocked/research_passed/portfolio_passed/holdout_passed/paper_observing/paper_passed/manual_ready/suspended/retired` |
| `approved_by/approved_at` | nullable；研究及纸面门全部通过后填写发布审批 |
| `created_at/updated_at` | ISO时间 |

唯一约束：`(strategy_key, version)`；状态提升只能经过显式服务函数。

### 8.2 `manual_account`

`id/name/currency/broker_label/confirmed_cash/ledger_checkpoint_hash/status/risk_policy/last_reconciled_at/created_at/updated_at`。`broker_label`仅为用户标签，不保存账号、密码或凭证。`confirmed_cash`是账本物化缓存，必须绑定`ledger_checkpoint_hash`且可由事件重放逐值验证。首版账户从空持仓开始，初始资金通过`opening_balance`资金事件入账；已有持仓迁移留给独立版本。`status`为`draft/active/reconcile/suspended/closed`。

### 8.3 `manual_execution_authorization`

`id/release_id/account_id/status/capital_limit/max_order_notional/max_gross_exposure/max_single_weight/max_daily_items/max_daily_loss/max_drawdown/revocation_policy/revocation_policy_hash/valid_from/valid_until/first_fill_event_id/first_fill_at/approved_by/approved_at/revoked_at/reason/created_at/updated_at`。

状态：`pending/approved/active/reconcile/suspended/revoked/expired`。同一账户同一时刻最多存在一个`approved/active/reconcile`授权。发布在全部研究和纸面门通过后成为全局`manual_ready`；授权再记录特定用户、账户、资金和期限的批准，首笔用户成交后进入`active`。所有数值和撤销后的持仓处理方式由用户明确输入；不得从回测资金或纸面账户复制为真实限额。

### 8.4 `daily_decision`

字段：`id/release_id/authorization_id/signal_date/revision/data_as_of/input_hash/decision_hash/action/risk_state/target_weights/reason_codes/status/blocked_reason/supersedes_id/created_at`。唯一约束`(authorization_id, signal_date, release_id, revision)`和`decision_hash`；每个授权/信号日最多一条非`superseded`决策。输入变化创建递增revision并指向旧记录。

状态：`draft/ready/blocked/superseded/reviewed`；action使用第5节六种值。

### 8.5 `manual_cohort`

`id/authorization_id/decision_id/signal_date/planned_entry_date/planned_exit_date/sleeve_index/budget/status/created_at/closed_at`。唯一约束`(authorization_id, signal_date, sleeve_index)`。状态：`planned/entering/open/exiting/closed/blocked/cancelled`。`sleeve_index`只能为0或1，但业务排序依据交易日序号。

### 8.6 `manual_execution_plan`

唯一约束 `(authorization_id, execution_date, execution_session, plan_type, version)`。字段：`id/idempotency_key/decision_id/authorization_id/account_id/execution_date/execution_session/plan_type/version/input_hash/plan_hash/account_snapshot_id/quote_snapshot_hash/authorization_hash/trading_rule_id/trading_rule_version/trading_rule_effective_date/status/cash_before/expected_cash_after/expected_fees/blocked_reason/expires_at/viewed_at/supersedes_plan_id/created_at/updated_at`。`plan_hash`唯一；`idempotency_key`由公共幂等记录管理。

- `execution_session`：`open/close`。
- `plan_type`：`entry/exit/rebalance/risk/mixed`；同一时段同时有风险卖出和进入买入时使用`mixed`。
- `status`：`draft/ready/viewed/partially_filled/completed/expired/cancelled/blocked/superseded`。
- `expected_cash_after`只是计划情景值，永远不能作为后续计划的`cash_before`。

### 8.7 `manual_execution_item`

`id/idempotency_key/plan_id/cohort_id/code/side/phase/pre_quantity/available_quantity/available_cash_before/target_quantity/target_weight/planned_quantity/cash_required/cash_dependency_type/depends_on_item_ids/reference_price/price_source/price_as_of/min_price/max_price/expected_notional/estimated_commission/estimated_tax/estimated_other_fee/order_sequence/reason_codes/status/confirmed_quantity/created_at/updated_at`。唯一约束`(plan_id, cohort_id, code, side)`。

`phase`为`sell/buy`；买入首版的`cash_dependency_type`只能为`confirmed_cash`，不得直接依赖计划卖出。状态：`planned/submitted/cancelled/skipped/partially_filled/filled/unfilled/rejected/expired/blocked`。`confirmed_quantity`是执行事件的派生缓存，必须在事件重放时核对，不能作为事实源。卖出排序先于买入；计划数量永远不改变影子持仓。

### 8.8 `manual_execution_event`

`id/client_event_id/item_id/account_id/cohort_id/user_trade_ref/event_type/side/quantity/price/commission/stamp_duty/other_fee/total_fee/traded_at/source/supersedes_event_id/payload_hash/created_at`。`client_event_id`全局唯一；`user_trade_ref`在账户内非空时唯一；重复键与相同规范化payload返回原事件，payload不同则冲突。`account_id/cohort_id/side`必须与计划项派生值一致，不接受相互矛盾的副本。

- `event_type`：`submitted/partial_fill/fill/cancelled/rejected/skipped/fee_adjustment/fill_reversal/fill_correction`。
- `source`首版固定`user_reported`，未来真实Adapter才能使用`broker_verified`。
- 数量使用整数股；价格、费用和金额使用`Numeric`定点数或整数分，不使用`Float`。
- 成交事件费用未知时派生状态为`fee_pending`；补齐费用后才可完成对账。
- 更正固定追加一笔引用原事件的有符号`fill_reversal`，再追加一笔完整`fill_correction`；禁止UPDATE/DELETE原经济事件。

### 8.9 `manual_account_snapshot`与`manual_position_snapshot`

账户快照字段：`id/account_id/as_of/cash/total_asset/source/status/idempotency_key/note/created_at`；持仓快照字段：`snapshot_id/code/total_quantity/available_quantity/avg_cost/market_value`。`source`首版固定`user_reported`，未来只有真实券商回报Adapter可以写`broker_verified`；`status`为`pending/reconciled/different`。下一份执行计划绑定最近一个`reconciled`快照；快照本身不直接改账本。

### 8.10 `manual_reconciliation`

`id/account_id/plan_id/snapshot_id/status/planned_quantity/filled_quantity/unfilled_quantity/quantity_differences/position_differences/cash_difference/reference_slippage/actual_fees/ledger_checkpoint_hash/calculated_at/resolved_at`。状态为`pending/matched/different/resolved`；`different`使账户和授权进入`reconcile`，直到追加成交、更正或用户确认差异来源。

### 8.11 `manual_cash_event`

`id/account_id/idempotency_key/event_type/amount/occurred_at/source/status/correction_of/note/created_at`。`event_type`为`opening_balance/deposit/withdrawal/interest/fee_adjustment`，`source`首版固定`user_reported`；金额使用定点数，更正使用新事件冲销，禁止直接UPDATE余额。日收益采用现金流中性口径。

### 8.12 `manual_ledger_event`与物化视图

事件字段：`id/account_id/account_sequence/event_type/reference_id/trade_date/cash_delta/quantity_delta/code/cohort_id/payload/previous_hash/event_hash/created_at`。唯一约束`(account_id, account_sequence)`；同一数据库事务锁定账户尾节点、分配严格递增序号，并以精确前序`event_hash`生成新哈希。金额使用定点数、股数使用整数。

物化表 `manual_position_lot` 保存`account_id/code/cohort_id/quantity/remaining_quantity/avg_cost/buy_at/unlock_date/planned_exit_date/status`；任何时刻均能从事件重建。

### 8.13 `research_holdout_window`与访问事件

`research_holdout_window`保存`id/dataset_id/data_content_hash/start_date/end_date/policy_hash/status/created_at/opened_at/invalidated_at`，状态`sealed/opened/invalidated/completed`。`research_holdout_access`追加保存`window_id/accessed_at/accessed_by/purpose/result_exposed`。任何收益读取都会将窗口从sealed变为opened；修改策略后旧窗口不能恢复sealed。

### 8.14 公司行动事实

`manual_corporate_action_fact`保存`id/account_id/code/action_type/record_date/ex_date/pay_date/share_listing_date/cash_per_share/share_ratio/eligible_quantity/source/source_reference/payload_hash/status/created_at`。只有来源、资格数量和到账事实足以确定权益时，才能生成账本事件；未知影响进入`reconcile`并阻断新进入。API至少提供创建、查看和冲销事实，不允许直接改物化持仓。

### 8.15 日终估值、`daily_review`与`research_revision`

`manual_valuation_snapshot`保存`id/account_id/valuation_date/cash/positions_json/total_equity/price_snapshot_hash/price_source/price_as_of/ledger_checkpoint_hash/created_at`；`positions_json`逐证券记录数量、价格和来源。`daily_review`保存`authorization_id/review_date/revision/decision_id/plan_ids/valuation_snapshot_id/ledger_checkpoint_hash/equity/daily_return/drawdown/turnover/execution_deviation/data_health/factor_health/reconciliation_status/next_action/reason_codes/status/supersedes_id/created_at`。唯一约束`(authorization_id, review_date, revision)`；成交更正或补费后追加新revision并保留旧复盘，每日最多一条非`superseded`记录。

`research_revision`保存`release_id/review_id/hypothesis/change_scope/evidence/status/created_at`；状态`proposed/queued/rejected/validated`。它只能进入研究队列，不能直接生成执行计划。

### 8.16 `manual_daily_job`与公共幂等记录

`manual_daily_job`保存`id/authorization_id/job_type/trading_date/scheduled_for/status/attempt/lease_owner/lease_until/heartbeat_at/idempotency_key/input_hash/error_code/error_message/created_at/started_at/completed_at`，状态为`queued/running/completed/failed/blocked`。

`manual_idempotency_record`保存`id/key/http_method/route_scope/account_or_authorization_id/request_hash/response_status/response_body_hash/resource_type/resource_id/created_at`。唯一作用域为`http_method + route_scope + account_or_authorization_id + key`：相同键和相同规范化payload返回原响应；相同键和不同payload返回HTTP 409。

### 8.17 派生状态与完成规则

- 执行事件是事实源；计划项、计划、账户和授权状态都是可重放的派生状态。
- 一个计划项存在有效成交且累计净成交量小于计划数量时为`partially_filled`；等于计划数量且费用完整时为`filled`。有效冲销和更正后重新计算。
- 一个计划只有在所有计划项都进入`filled/unfilled/cancelled/rejected/skipped/expired`终态，所有成交费用完整，且账本事件已成功写入时才能为`completed`；仍有部分成交或`fee_pending`时为`partially_filled`并令账户进入`reconcile`。
- `submitted/cancelled/rejected/skipped`只记录用户报告的券商端操作结果，不表示系统向券商发出了任何请求。
- 公司行动、成交更正、资金事件和账户快照均必须通过账户级串行账本事务或对账事务处理，禁止并发更新物化现金和持仓。

## 9. HTTP接口

新路由统一位于 `/api/v1/manual-trading`，实现文件为 `server/api/manual_trading.py`。所有路由沿用现有控制面的loopback边界；所有读写真实衍生账户数据的接口都调用`require_operator`。所有改变状态的请求必须携带`Idempotency-Key`请求头，按8.16的作用域处理并写入审计事件。

### 9.1 策略与授权

```text
POST /releases                         冻结研究证据，创建draft/research_blocked版本
GET  /releases                         列表
GET  /releases/{id}                    详情与门禁
POST /releases/{id}/evaluate           执行PromotionPolicy
POST /releases/{id}/start-paper        创建前瞻纸面观察
POST /releases/{id}/approve-manual     研究/纸面发布审批，状态变为manual_ready
POST /releases/{id}/suspend            暂停新计划

POST /accounts                         创建人工执行账户
GET  /accounts/{id}                    影子现金/持仓/批次/对账状态
POST /authorizations                   创建pending授权
POST /authorizations/{id}/approve      用户批准资金和风险上限
POST /authorizations/{id}/suspend      暂停
POST /authorizations/{id}/revoke       撤销
```

### 9.2 每日闭环

```text
POST /authorizations/{id}/decisions/generate
GET  /authorizations/{id}/decisions/{signal_date}
GET  /authorizations/{id}/today

POST /authorizations/{id}/decisions/{signal_date}/plans
GET  /plans/{plan_id}
POST /plans/{plan_id}/refresh              仅draft原地刷新
POST /plans/{plan_id}/revise               创建新版本并supersede旧版
POST /plans/{plan_id}/mark-viewed
GET  /plans/{plan_id}/export.csv
POST /plans/{plan_id}/items/{item_id}/events
POST /execution-events/{event_id}/correct

POST /accounts/{account_id}/snapshots
GET  /accounts/{account_id}/snapshots/latest
POST /accounts/{account_id}/cash-events
POST /accounts/{account_id}/corporate-action-facts
GET  /accounts/{account_id}/corporate-action-facts
POST /corporate-action-facts/{id}/reverse
POST /accounts/{account_id}/reconcile
GET  /accounts/{account_id}/ledger
GET  /accounts/{account_id}/reviews/{date}
POST /accounts/{account_id}/reviews/{date}/create-revision
```

禁止新增`submit`、`send-to-broker`或把计划转换为`LiveOrderDraft`的接口。CSV导出只导出清单，不包含券商导入格式承诺。

### 9.3 返回公共字段

所有响应包含：`manual_execution=true`、`broker_connected=false`、`user_reported_fills=true`、`live_order_submission=false`、`as_of`和`evidence_status`。计划和成交必须分别显示`planned`与`user_reported`。写接口遇到相同幂等键及相同规范化payload时返回首次资源和响应；同键不同payload固定返回409及`IDEMPOTENCY_CONFLICT`。

## 10. 晋级标准

新增版本化 `ManualDailyPromotionPolicy v1`。以下为工程默认门，不是收益承诺；任何阈值调整都会产生新policy hash。

### 10.1 数据与研究硬门

- 数据、策略、表达式、执行引擎和成本哈希完整。
- 无P0时间泄漏、账务或数据覆盖错误。
- 因子training和validation均通过冻结门。
- 策略角色语义完整；filter必须有阈值、基础rank和消融结果。
- 公司行动、历史资格和交易规则限制全部在报告中；存在会改变收益结论的未解决错误则阻断。
- 配股、重整权益、差异化分配、退市回收等影响无法量化的关键未知默认阻断；只有范围可证明且保守界限预先冻结时才能继续评估。
- sealed holdout有创建和访问事件；2015、2019—2022及2023—2026.08均标记为已打开，不得复用。

### 10.2 组合硬门

- baseline和stress净收益均大于0。
- baseline和stress相对预注册基准的净超额均大于0。
- stress Sharpe至少0.8。
- stress最大回撤不超过20%。
- 年化换手不超过policy中用户明确接受的上限，且baseline/stress均按实际测得换手计费。换手固定使用`全年绝对买卖成交额之和 / 平均权益`双边口径。
- 容量约束下金额加权目标成交率至少95%；分母包含全部计划金额，拒单、未成交和用户未操作均计为未完成，禁止删除失败项改善比例。
- 四舍五入、费用、公司行动和逐日权益重放100%一致。

### 10.3 人工可执行硬门

- 每日计划项数量不超过授权中的`max_daily_items`。
- 计划生成到预期执行时段的可操作时间满足用户配置。
- 回测包含延迟、跳过、部分成交和价格偏离情景。
- `gross_exposure <= authorization.max_gross_exposure`，每批预算不超过总敞口的50%。
- 任一真实资金/单笔/损失/回撤限额缺失时阻断授权。

### 10.4 前瞻纸面门

首次小额人工执行前，使用完全冻结版本完成至少30个交易日：

- 30/30日决策有输入哈希和明确状态；
- 计划、模拟成交、账本和日终复盘无重复记录；
- 中断、重启、重复回填和备份恢复测试通过；
- 所有账本日100%对账；
- 数据异常场景均阻断新买入；
- 没有P0/P1未解决问题；
- 策略经济指标仍满足冻结风险预算。

30日只允许进入由用户设置资金上限的`manual_pilot`。扩大授权至少需要120个前瞻交易日和新的用户批准；系统不自动扩大资金。

日频双批在每天满额进入/退出45%权益时，按上述双边口径理论换手约为`0.45 × 2 × 252 = 226.8倍/年`。因此本policy不会假装日频等于低换手：实施者必须预注册用户可接受的换手上限，并证明扣除实际测得费用后仍通过组合硬门；若无法通过则该满额日滚动方案自然被拒绝，不得在验证后临时放宽。

当前所有因子组合在stress下为负，因此目前均在组合硬门被阻断。

## 11. 费用与规则

建立 `EffectiveDatedTradingRuleRegistry`，统一按市场、板块和生效日期读取整手、T+1、印花税、价格限制和费用估算。计划估算与用户成交费用并存：

- `estimated_fee`只用于计划；
- `manual_execution_event`中的实际费用用于影子账本；
- 不得用估算覆盖用户费用；
- `fee_pending`阻断新买入直到补齐或用户明确对账。

当前持久纸面费用函数仍固定卖出印花税0.001，而研究账本在2023-08-28后使用0.0005；实施M1时先解决此口径差异。当前纸面数量校验统一要求100股，人工模块需单独实现全部零股卖出。

## 12. 前端

新增 `/manual` 页面，不把人工执行放进“账户接入”或“模拟交易”中：

1. **今日行动**：日决策、风险状态、计划买卖、到期批次、阻断原因。
2. **计划详情**：卖出优先顺序、数量、参考价/时间/来源、价格区间、费用、原因码；标记已阅读和导出CSV。
3. **成交回填**：部分成交、多笔成交、费用、时间、用户流水号、未成交/撤单/拒单。
4. **影子账户**：确认现金、持仓、可卖数量、批次、未完成计划和对账状态。
5. **日终复盘**：收益、回撤、换手、执行偏差、因子/数据健康、次日动作。
6. **策略授权**：研究门、组合门、纸面门、资金/风险限额和用户批准记录。

UI固定显示“人工执行、用户回填、未经券商API验证”。任何`blocked/reconcile`状态置顶，并禁用新买入确认。移动端必须让用户在一个屏幕内看见证券、方向、数量、时段和阻断状态。

## 13. 调度与恢复

- API进程不直接承担长期任务。新增持久`manual_daily_job`记录和CLI worker，状态`queued/running/completed/failed/blocked`，带lease、heartbeat、attempt和幂等键。
- 收盘任务只在验证日线完成后生成日决策；盘前任务只刷新计划证据；日终任务只在用户成交回填和行情齐备后生成复盘。
- worker重启后从数据库恢复，不重复创建决策、计划、成交或账本事件。
- 每日备份必须覆盖manual表、策略版本、授权和审计哈希；恢复演练后账本hash、现金和持仓一致。
- 告警仅提示用户处理`blocked/reconcile/plan_ready/exit_due`，不代表系统代为操作。

## 14. 分阶段施工

低级模型必须按顺序施工。一个阶段只修改列出的模块；完成验收并更新Progress后才能进入下一阶段。严禁把后续阶段能力写成当前已完成。

### M0：领域与协议冻结（本次完成）

文件：`CONTEXT.md`、本方案、`AGENTS.md`、`docs/README.md`、`PROGRESS.md`。

完成条件：术语、首版范围、日频时序、状态、表、接口、晋级门和验收项均有单一明确含义。

### M1：规则注册与纯计划模块

新增：

- `quant_engine/trading/manual_protocol.py`
- `quant_engine/factor/manual_daily_label.py`
- `quant_engine/trading/effective_rules.py`
- `server/services/manual_planning.py`
- `tests/test_manual_protocol.py`
- `tests/test_manual_planning.py`

修改：`paper_market_rules.py`只抽取共享的有效日期规则，不改变旧纸面结果；研究和纸面旧结果保留。

实现：`manual-daily-label-v1`；类型化policy/decision/cohort/plan/item；卖出优先；确认现金；整手买入和全部零股卖出；费用估算与实际费用分开；hold/rebalance/reduce/flat/blocked/reconcile纯状态机。

验收：至少覆盖第6节全部异常；纯函数不访问数据库、网络或当前时间；相同输入逐值相同。

### M2：持久模型与影子账本

新增/修改：

- `server/models/schema.py`
- `server/models/database.py`
- `server/services/manual_ledger.py`
- `tests/test_manual_ledger.py`
- `tests/test_manual_migrations.py`

实现第8节表、约束、append-only成交/更正、资金事件、哈希链、lot、现金重建和现金流中性收益。

验收：买入、T+1、部分卖出、全部零股卖出、多笔费用、重复流水号、更正、入金/出金、分红/送转和恢复重放逐项对账；资金事件不计为策略收益；人工事件不会写入任何paper表。

### M3：日频研究标签、策略包与组合回测

新增：

- `quant_engine/factor/manual_daily_bundle.py`中的独立`ManualDailyFactorBundleV2`；不得修改v1 dataclass字段
- `scripts/run_factor_manual_daily_portfolio.py`
- `tests/test_manual_daily_factor_research.py`
- `tests/test_manual_daily_portfolio.py`

修改：`server/services/factor_research.py`为实验身份加入完整label spec；`quant_engine/backtest/research_engine.py`和`research_ledger.py`增加开盘进入、收盘退出及双cohort语义，保留旧研究结果逐值兼容。

实现：`manual-daily-label-v1`独立training/validation、基准和stress回测、逐日权益重放与新bundle。研究引擎以原价账本模拟T+1开盘进入、T+2收盘退出和双cohort；研究标签按2.4公式使用可复权价格。bundle覆盖`signal_phase/entry_offset/entry_phase/exit_offset/exit_phase/cohort_count/cohort_gross_exposure/entry_expiry/exit_retry_policy/auto_submit=false`。

验收：同一输入的标签、批次、费用、权益和结果哈希稳定；T0—T3时序逐点验证；baseline/stress均包含失败成交和真实测得换手；现有两个因子组合重新运行后只能按新证据得出通过或阻断结论，不能继承v1状态。

### M4：晋级、holdout与授权

新增：

- `server/services/strategy_promotion.py`
- `server/services/manual_authorization.py`
- `server/services/research_holdout.py`
- `tests/test_strategy_promotion.py`
- `tests/test_manual_authorization.py`
- `tests/test_research_holdout.py`

实现`ManualDailyPromotionPolicy v1`、holdout创建/打开/失效记录、`StrategyRelease`、30/120日门、资金/风险必填和发布/授权分离的状态转换。撤销策略作为授权policy的一部分参与哈希。

验收：当前两个历史因子组合不能继承资格；缺stress、基准、重放、先sealed后opened的holdout访问证据、holdout结果或前瞻观察均不能使发布成为`manual_ready`；发布未就绪或任一用户限额缺失均不能批准授权；用户批准动作写审计。

### M5：日决策、批次和计划

新增：

- `server/services/manual_decision.py`
- `server/services/manual_data_readiness.py`
- `server/services/manual_cohort.py`
- `server/services/manual_risk.py`
- `server/services/manual_planning.py`
- `tests/test_manual_daily_cycle.py`

复用已冻结Strategy Protocol和因子bundle；实现T信号、T+1进入、T+2退出及两个45%批次。不得复用`auto_trade=false`作为计划模式。

验收：连续至少10个合成交易日覆盖目标保持仍滚动批次、换股、空仓、风险减仓、停牌、涨跌停、部分成交和未成交；开盘进入和收盘退出均经过preflight；draft可刷新，ready/viewed只能新建版本；卖出到账后新建买入版本并重新查看；每个计划可从输入哈希复算；仅用户成交事件改变账本。

### M6：HTTP与人工操作台

新增：

- `server/api/manual_trading.py`
- `web/src/views/ManualTrading.vue`
- `web/src/types/manual.ts`
- `web/src/utils/manual.ts`
- `web/tests/manual.test.mjs`

修改：`server/main.py`、前端router和SideNav。

实现第9、12节接口和页面；不提供真实提交按钮。

验收：桌面及390/320px完成“查看今日计划→标记已读→回填两笔部分成交→对账→查看复盘”；刷新/重试不重复入账；控制台无错误；页面始终显示人工来源边界。

### M7：持久worker、备份与日终复盘

新增：

- `server/services/manual_scheduler.py`
- `server/services/manual_backup.py`
- `scripts/run_manual_daily_worker.py`
- `server/services/manual_review.py`
- `tests/test_manual_scheduler.py`
- `tests/test_manual_review.py`

修改`server/models/schema.py`和`server/models/database.py`，实现8.16的持久任务、交易日调度、lease/heartbeat/recovery、manual表备份恢复、数据健康、执行偏差、因子衰减和Research Revision。

验收：并发worker和故障注入后无重复计划/账本；备份恢复后的账户序号、hash链、现金、持仓和计划逐值一致；数据断源阻断新买；未对账阻断次日进入；研究修订只入研究队列。

### M8：30日前瞻纸面验收

冻结一个真正通过研究与组合门的新策略版本，运行30个交易日。允许使用合成/回放做工程验收，但`manual_pilot`必须使用开始后才到达的真实前瞻数据。

验收：满足10.4全部条件，生成不可变观察报告；没有通过则回到研究循环，不降低门槛补偿。

### M9：首次人工购入与持续循环

用户创建人工执行账户，显式填写资金和风险上限，批准授权并对首批计划逐项确认。系统生成清单，用户在券商端操作并回填成交。

验收：首次买入、费用、T+1可卖、T+2退出、两批现金共享、日终复盘和次日决策均与用户回填事实一致；系统和日志没有任何“已向券商提交”表述。

## 15. 测试矩阵

每阶段完成后统一验证，不要求每个小步骤运行全量测试。最终阶段必须包含：

- 状态转换表每条合法/非法边；
- 所有唯一约束和幂等重放；
- 时区、周末、节假日、长假、月末；
- T+1、两批共享现金、重叠证券和零股清仓；
- 部分成交、撤单、未操作、重复/冲突流水号、费用待补和更正；
- 停牌、涨跌停、退市、公司行动和数据修订；
- 策略/数据/授权hash变化导致旧计划失效；
- API权限、错误结构和无真实提交能力反向测试；
- 账本从零重放与物化视图逐值一致；
- worker并发认领、租约到期和崩溃恢复；
- 前端空态、加载、失败、阻断、移动端和网络重试；
- 现有后端、前端及冻结研究回归无退化。

## 16. 交付纪律

1. 每阶段开始先读 `CONTEXT.md`、本方案、`PROGRESS.md` 和涉及的当前专业文档。
2. 每阶段使用新表、新结果目录和新策略版本；保留现有paper与36组研究证据。
3. 每阶段结束统一运行相称的专项/全量测试，记录实际结果，不预写通过。
4. 更新本方案的阶段状态只允许追加“实施记录”，不得改写原始门禁和首版范围；规则变化创建v2方案。
5. 每次项目修改按 `AGENTS.md` 更新Progress；提交/推送按用户授权执行。
6. 任一研究、组合、观察或对账硬门失败时停止晋级并记录原因；低级模型不得自行放宽阈值。

## 17. 当前起点与下一动作

当前代码已具备受限因子生成、训练/验证门、冻结bundle、原价公司行动组合回测、纸面账户与观察、审计和前端因子工作台。人工执行链尚未创建。

下一次开发从M1开始：先实现有效日期规则和纯计划状态机，不创建数据库表、不修改真实或纸面账户。M1完成并统一验证后进入M2。

## 18. M1实施记录

### 2026-09-09 22:16 CST

- 已新增 `quant_engine/trading/manual_protocol.py`、`quant_engine/factor/manual_daily_label.py`、`quant_engine/trading/effective_rules.py` 和 `server/services/manual_planning.py`，并新增 `tests/test_manual_protocol.py`、`tests/test_manual_planning.py`。
- 已实现 `manual-daily-label-v1` 的交易日历步进、按生效日期解析A股规则、`Decimal`费用估算、类型化策略/决策/批次/计划/计划项、卖出优先、只使用确认现金、A股买入100股整手、全部零股卖出、阻断/对账优先级以及draft刷新和ready/viewed版本修订。
- `paper_market_rules.py` 仅复用独立的历史纸面费用适配器，既有持久纸面仍保持卖出印花税0.001；人工计划按生效日期读取规则，2023-08-28起的默认A股规则使用0.0005。
- 本阶段实际验证：M1专项17项、纸面交易/跨市场回归23项和后端全量717项均通过；compileall、Ruff、`git diff --check`通过。全量测试有83个既有警告，未据此宣称失败或消失。
- M1没有创建数据库表、写入账本、调用网络、访问当前时间、提交计划到券商或生成模拟成交。M2持久模型/影子账本仍未开始；本实施记录不改变后续阶段门禁。

## 19. M2实施记录

### 2026-09-09 22:36 CST

- 已实现M2数据库模型、人工现金/成交事实写入、账户级哈希链账本、A股T+1批次重放、幂等更正和账户对账基础；本节记录当前施工快照，不代表后续阶段完成。
- 人工链使用`manual_*`表，`source`固定为`user_reported`；服务不调用纸面提交服务，不创建或修改`PaperOrder`、`PaperFill`、`PaperLedgerEvent`、`PaperLot`等纸面对象。新经济字段采用SQLAlchemy `Numeric`并在服务入口量化为Decimal，不用Float承载成交、费用或现金。
- 每个账户的`account_sequence`从1递增；事件哈希覆盖账户、序号、引用、交易日、现金/数量变动、代码、载荷及前哈希。人工现金事件和成交事件先形成不可变事实，再追加账本事件；物化批次由账本重放生成，账户现金与检查点哈希同步更新。
- 买入必须传入可验证交易日历，解锁日为下一交易日；卖出按买入时间排序的可用批次消费，支持最后不足整手的全部卖出；费用拆分必须满足`total_fee=commission+stamp_duty+other_fee`。成交更正保留原事实，追加冲正事实和替代成交事实，不更新原记录。
- 可保存用户回报的现金、总资产和持仓快照，比较账本现金/数量并落库`matched`或`different`；差异将账户置为`reconcile`，不自动修正账本。
- 实际验证：`tests/test_manual_ledger.py`与`tests/test_manual_migrations.py`共5项通过；后续提交前须补跑后端全量回归和跨平台静态检查。
- 下一步：M3实现策略发布包、组合批次和持仓归属，仍需保持人工成交链与研究/纸面链隔离。

## 20. M3实施记录

### 2026-09-09 22:44 CST

- 新增独立`ManualDailyFactorBundleV2`，没有修改`FrozenFactorStrategyBundle`字段或v1身份；bundle显式记录收盘信号、T+1开盘进入、T+2收盘退出、双cohort、每批45%、入场过期、退出重试、费用场景和`auto_submit=false`，通过完整证据哈希生成bundle hash。
- 新增原价输入组合回测`run_manual_daily_portfolio`：从注入的归档日历和bars读取，不访问网络/数据库；同日只在收盘捕获signal，后续交易日开盘使用信号，退出使用收盘；共享现金、A股整手买入、全部持仓零股清仓、停牌/缺bar审计、baseline/stress费用和结果hash均为可重放结果。输出明确为研究结果，不产生paper或manual成交。
- 因子实验新增完整`label_spec`身份并保留旧默认open/open标签；SQLite旧库由`init_db()`追加`factor_experiment.label_spec`，旧研究结果不被重解释。
- 实际验证：M3专项与因子/研究回归共71项通过；结果hash重复运行一致。下一阶段需实现M4发布晋级、真实holdout访问证据和人工授权状态门。

## 21. M4实施记录

### 2026-09-09 22:51 CST

- 新增`StrategyRelease`不可变发布包模型和`ManualDailyPromotionPolicyV1`证据评估。manual_ready需要研究哈希/训练/验证、baseline/stress组合指标、重放一致性、holdout访问、30日观察及P0/P1状态全部满足；缺失证据默认失败，不能从旧paper记录推导资格。
- 新增holdout生命周期：创建即`sealed`，读取会追加`ResearchHoldoutAccess`并转`opened`，可完成或失效，不能重新sealed；已完成/失效窗口禁止继续读取或完成。
- 新增账户级`ManualExecutionAuthorization`和服务：限额、有效期、撤销policy必须完整；只能对`manual_ready`发布创建，用户批准写`AuditEvent`，首笔成交后才进入`active`，撤销同样写审计。授权不保存券商凭证，也不提供下单接口。
- 实际验证：M4专项7项通过，Ruff通过；后续阶段需将授权绑定到日决策、计划和人工成交事件。

## 22. M5实施记录

### 2026-09-09 22:59 CST

- 新增`DailyDecision`、`ManualCohort`、`ManualExecutionPlan`和`ManualExecutionItem`持久模型，决策按输入变化递增revision并将旧记录置为`superseded`；cohort固定两个sleeve、由注入交易日历计算T+1进入/T+2退出。
- 新增数据就绪和风险preflight，交易日历不完整、行情缺失、账户对账或授权状态不满足时返回阻断；风险检查覆盖确认现金、单笔金额、单票权重、总敞口和每日项数。
- M1纯计划输出可持久化为人工清单；计划项含参考价、来源、时间、预计费用、顺序和`confirmed_cash`依赖。查看计划只改变计划状态，不写成交事件，不更新人工账本，不触碰Paper表。
- 实际验证：M5专项2项通过；下一阶段实现统一HTTP幂等边界和人工操作台，所有操作继续显示`user_reported`且不提供提交券商按钮。

## 23. M6实施记录

### 2026-09-09 23:10 CST

- 新增`server/api/manual_trading.py`并挂载到`/api/v1/manual-trading`。接口只接受操作员保护下的人工事实：创建账户、现金事件、用户成交/更正、账户对账和计划标记已读；响应显式携带`manual_execution=true`、`broker_connected=false`、`user_reported_fills=true`和`live_order_submission=false`。没有真实委托、券商认证或Paper表写入路由。
- 账户创建使用持久幂等键；现金事件沿用现金幂等键，成交沿用`client_event_id`，对账沿用快照幂等键；状态输出把Decimal转为字符串，前端不依赖浮点金额。交易日历覆盖检查使用跨月安全的日期加法，覆盖失败返回阻断错误。
- 新增`web/src/views/ManualTrading.vue`、`web/src/types/manual.ts`、`web/src/utils/manual.ts`和`web/tests/manual.test.mjs`，页面展示今日计划/台账/对账入口并提供资金和成交回填表单；页面文案与路由反向约束均明确没有“提交订单”动作。前端操作员令牌拦截器覆盖`/manual-trading`，导航支持桌面和移动端。
- 实际验收：人工账本+HTTP专项`5 passed`，前端测试`15 passed`，`npm run build`、Ruff、`compileall`通过；Playwright桌面、390px、320px真实页面检查通过，320px无横向溢出（`scrollWidth=320`），控制台错误为0。
- 本阶段后端代码仍需在M6提交后纳入全量回归；M7持久worker/备份/复盘、M8真实前瞻观察和M9首次人工购入仍按原门禁执行。

## 24. M7实施记录

### 2026-09-09 23:31 CST

- 新增`ManualDailyJob`和`server/services/manual_scheduler.py`，任务以`job_key`去重，按交易日排队；worker使用显式lease owner、过期时间、heartbeat和attempt计数，支持崩溃恢复、临时失败重试和不可执行事项阻断。`scripts/run_manual_daily_worker.py`支持Windows/macOS的Python单次运行或短间隔循环，不引入平台专属守护进程；默认没有人工输入或审查handler时直接阻断。
- 新增`server/services/manual_backup.py`，只使用`pathlib.Path`、UTF-8 JSON、临时文件和`os.replace`完成跨平台原子备份；备份涵盖manual领域表、发布/授权/holdout/决策/计划/账本/复盘/研究修订等证据，包含行数和内容hash。恢复只允许目标数据库为空，并在提交前校验schema和hash。
- 新增`ManualValuation`、`ManualDailyReview`、`ResearchRevision`、`ManualCorporateActionFact`。估值要求现金+持仓市值=总资产，首日不计算虚假收益，后续收益按真实用户报告现金流中和；对账不同或账户处于`reconcile`时复盘为`blocked`。公司行动只保存`user_reported`事实，不自动改写持仓；研究修订只进研究队列，不改变已发布授权。
- 人工HTTP和操作台新增估值、复盘、任务只读、研究修订和公司行动事实入口；新增数据仍不进入Paper链，也没有券商认证或订单提交能力。
- 实际验证：M7专项调度/备份/复盘`6 passed`；连同人工HTTP回归`7 passed`；前端`15 passed`，`npm run build`、Ruff、`compileall`通过。M7不宣称已完成M8/M9的真实前瞻和用户首笔交易门。

## 25. M8实施记录

### 2026-09-09 23:46 CST

- 新增`ManualProspectivePilot`与`ManualPilotObservation`，pilot只接受30个交易日目标；观察日必须是注入日历中的交易日，真实前瞻模式要求`received_at`发生在pilot启动日之后，且`data_as_of`不得晚于观察日。每日日志保留输入hash、信号hash、来源、回填对账状态和到达时间。
- `manual_pilot.py`提供创建、逐日追加和不可变终结报告。终结报告同时检查研究、组合、holdout、重放、风险、数据健康、30日/每日对账、P0/P1、最大回撤和日亏损次数；`synthetic_engineering`永远追加`synthetic_engineering_not_eligible_for_pass`，不能被当作真实前瞻通过。
- 人工API新增pilot生命周期路由；backup服务涵盖pilot及观察记录。没有自动修改`StrategyRelease`状态，观察结果不会绕过原有发布/授权门。
- 实际验证：M8专项`2 passed`；连同M7/M6人工域回归`9 passed`，Ruff和`compileall`通过。30日循环只验证工程状态机，不构成现实市场30日观察证据。

## 26. M9实施记录

### 2026-09-09 23:48 CST

- 新增`ManualExecutionConfirmation`和`server/services/manual_execution_cycle.py`。计划项必须先处于已查看状态并由用户记录确认hash；确认成交必须绑定计划/项目/账户，检查账户状态、授权状态、单笔限额、计划数量和用户报告事实，再调用M2人工账本；任何券商提交动作均不存在。
- 首笔成交在人工成交事实成功落库后把`approved`授权激活为`active`并记录首笔事件；部分成交更新`ManualExecutionItem.confirmed_quantity/status`，全部项目进入终态后计划才完成；未成交原因写入项目并不伪造fill。重放相同`client_event_id`不会重复入账。
- `/manual-trading`新增授权创建/批准、授权查询、计划项逐项确认、确认成交和未成交接口。授权仍只保存限额与审计字段，不保存券商凭证；买入日历不可用或T+1不满足时失败关闭。
- 实际验证：M9工程专项`1 passed`，Ruff和`compileall`通过；该结果只说明代码状态机符合边界，不是用户真实首笔交易验收。M8/M9的真实市场/人工操作门禁仍需后续由用户提供事实证据。
