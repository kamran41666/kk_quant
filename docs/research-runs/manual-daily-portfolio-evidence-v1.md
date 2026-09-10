# 日频组合证据合同 v1

> 类型：active-research-contract
> 适用阶段：H2b / `research_passed → portfolio_passed`
> 当前实现状态：合同已冻结；v3循环、精确Arrow证据、目录验证、内部账务与基础源执行重放已完成；公司行动经济重算深化和artifact resolver尚未完成

## 1. 目标

`portfolio_passed`只能由服务端读取并重算一对baseline/stress组合产物后产生。客户端只提交两份`ResearchEvidenceArtifact.id`，不能提交收益、Sharpe、回撤、换手、成交率、`replay_exact`或`passed`副本。

现有`manual-daily-portfolio-v2`属于工程模拟产物，在满足本合同全部文件和验证器前固定登记为`legacy_unsupported`，不能晋级。

## 2. 配对身份

同一pair必须共享：

- `strategy_core_hash`：排除`cost_scenario`，包含表达式、direction/role、label、执行时点、cohort、TopN、排序、仓位和代码hash；
- training/validation artifact ID及evidence hash；
- 数据、日历、信号、股票池、公司行动、基准manifest hash；
- 开始/结束交易日、初始资本和期末未平仓policy。

baseline/stress各自保留不同`bundle_hash`和`cost_policy_hash`。两者只允许费用率、滑点、参与率和分红税等预注册成本字段不同。

## 3. 必需输入manifest

| 输入 | 必需证据 |
|---|---|
| 原始行情 | `date/code/open/high/low/close/preclose/volume/amount/is_suspended/is_st`及内容hash |
| 历史资格 | `code/ipo_date/out_date/board`、每日资格规则和内容hash |
| 交易日历 | 覆盖首日前一交易日、全区间及退出重试尾部的交易日序列、来源和hash |
| 因子信号 | 每日全截面分数、表达式hash、生成时点、可见数据截止和文件hash |
| 公司行动 | action ID、登记/除权/派息/红股上市日、现金/送转比例、资格来源和hash |
| 基准 | 预注册基准ID、交易日价格/净值序列、复权口径和内容hash |
| 成本 | 佣金、最低佣金、过户费、印花税有效日期、滑点、参与率、分红税和取整规则 |
| 代码 | 生成器、执行账本、公司行动处理和独立重放器的代码hash |

任一关键输入缺失、hash不匹配、路径越出受控根目录、包含未解释公司行动或无法确定历史资格时，运行可以保存为研究反例，但`eligible_for_promotion=false`。

## 4. 时点和容量

- T日收盘信号只使用T日及以前可见数据。
- T+1开盘进入，T+2收盘退出；日期由冻结交易日历步进。
- 开盘容量只使用T日前一完整交易日成交量。禁止读取T日最终成交量决定T日开盘成交。
- baseline参与率为1%，stress为0.5%，与`ResearchLedger.RESEARCH_COSTS`一致；变化必须产生新cost policy。
- 同证券同交易日的所有cohort和买卖共享容量池，不得逐cohort重新获得完整容量。
- 买入按100股整手；退出只有在卖出该cohort全部剩余可卖股时允许零股清仓。

目标成交率使用冻结参考价：

```text
denominator = Σ requested_quantity × frozen_reference_price
numerator   = Σ min(unique_filled_quantity, requested_quantity)
                × frozen_reference_price
fill_rate   = numerator / denominator
```

拒单、停牌、涨跌停、容量不足、现金不足、未操作和重试失败都保留在分母；同一intent重试不能重复增加分母。

## 5. 公司行动和cohort会计

- 登记日按cohort冻结权益股数；登记后卖出不取消已取得的应收。
- 现金分红在除权日进入应收、派息日进入现金，不重复计入权益。
- 红股先按账户总权益取整，再按各cohort的精确份额和稳定余数规则分配；各cohort之和必须等于账户新增股数。
- 红股在上市日前不可卖；未知上市日、权益比例或持有人分配进入错误审计。
- 配股、重整、差异化分红和无法确认的退市回收默认使运行不具备晋级资格。
- 退出和公司行动始终按cohort记账；同代码另一未到期cohort不得被消费。

## 6. 必需输出

一次scenario必须生成固定schema文件，即使为空：

1. `signals.parquet`
2. `order_intents.parquet`
3. `order_attempts.parquet`
4. `trades.parquet`
5. `corporate_actions.parquet`
6. `positions.parquet`
7. `daily_portfolio.parquet`
8. `benchmark.parquet`
9. `audit.json`
10. `replay.json`
11. `summary.json`
12. `manifest.json`

`manifest.json`保存全部输入/输出hash、strategy core、scenario bundle、成本policy、执行协议、代码hash、生成时间和限制。输出目录不可覆盖；attempt使用新目录。

## 7. 指标

- `equity = cash + receivable_cash + market_value`，逐交易日保存。
- 日收益由相邻权益复算；Sharpe使用252日、无风险利率0并记录定义。
- 最大回撤保存0—1非负幅度。
- 基准累计收益使用同一首尾有效交易日；净超额为策略净累计收益减基准累计收益。
- 年化双边换手：`Σ abs(fill_notional) / mean(daily_equity) / elapsed_calendar_years`。
- baseline与stress分别计算净收益、超额、Sharpe、回撤、换手和目标成交率。
- summary只用于展示；resolver必须从Parquet、JSON和manifest重算。

## 8. 独立重放

重放器不得调用生成器的成交或记账函数。它必须验证：

- 文件、父输入和代码hash；
- fill唯一绑定intent/attempt/cohort且满足时点、现金、T+1、涨跌停、停牌和共享容量；
- 费用、现金、应收、股份、红股锁定和公司行动阶段；
- 每日账户股数等于各cohort之和；
- 逐日权益、复利收益、基准、换手和成交率；
- 所有未完成intent有明确终态或结转状态。

股数、日期、ID和状态逐值相等；金额最大容差0.01元，收益最大容差`1e-10`。`replay.json`保存逐门结果、最大差异和重放器代码hash。

## 9. Artifact登记与resolver

`register_manual_portfolio_pair(manifest_path, allowed_root)`必须：

1. 重算全部文件hash和指标；
2. 验证baseline/stress共享`strategy_core_hash`及所有非成本输入；
3. 验证scenario和cost policy没有互换；
4. 验证独立重放全部通过；
5. 分别登记`manual_portfolio_baseline`和`manual_portfolio_stress` artifact，并保存同一pair ID/hash。

`portfolio_passed` resolver只接受：

```json
{
  "baseline_artifact_id": "...",
  "stress_artifact_id": "..."
}
```

随后按`ManualDailyPromotionPolicyV1`重新检查baseline/stress净收益及净超额为正、stress Sharpe至少0.8、最大回撤不超过20%、换手不超过冻结上限、成交率至少95%、独立重放通过且没有未解决P0/P1。失败追加blocked evaluation，release保持`research_passed`。

每条`StrategyPromotionEvaluation`保存`previous_evaluation_id/hash`和自身`evaluation_hash`。portfolio判定必须引用同release最近一条通过的research判定；后续holdout和paper判定继续形成同一追加式哈希链。

## 10. 当前施工顺序

1. 已完成：前一交易日容量、1%/0.5%成本口径、`strategy_core_hash`和追加式晋级评价哈希链。
2. 已完成底层模块：提取cohort-aware原子成交和共享容量池；尚未接入正式v3组合循环。
3. 已完成：历史资格进入原子买入门；公司行动cohort子账覆盖登记、应收、派息、红股分配和上市锁定，并已接入正式v3循环。
4. 已完成内部证据层：v3保存signals/cohorts/intents/attempts/trades/actions/positions/daily/benchmark，并以固定Arrow schema的12文件、物理/schema/逻辑三层hash和不可覆盖原子目录写出；cohort终态合并到signals证据。
5. 已完成基础源重放：受控输入重新从磁盘核验并加载，独立检查日历、benchmark、signals、原始执行价格/滑点、昨量容量、历史资格、T+1和action定义；下一步深化登记权益、现金/红股分配与锁仓源重算。
6. 接入artifact登记和`portfolio_passed` resolver。
