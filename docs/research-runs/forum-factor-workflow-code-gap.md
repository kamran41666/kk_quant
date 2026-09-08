# 论坛因子工作流对照：项目代码能力盘点

记录日期：2026-09-08（Asia/Shanghai）

## 范围与结论边界

本文件只盘点 `kk_quant` 当前代码和项目内文档。未读取论坛帖子、用户浏览器或网络内容，因此不推断帖子具体实现，也不声称项目与帖子逐项等价。本文可作为后续拿到帖子流程后进行逐项映射的“项目侧事实基线”。

结论先行：项目已经有可靠的面板因子计算、PIT 防未来约束、相关性筛选、Rank IC 统计、回测因子数据接口，以及有证据门禁的日频纸面观察；但当前因子候选是代码中固定登记的 5 个高级候选，不是自动发现。因子脚本是一次性批处理，不包含 LLM 表达式生成、受限表达式解释器、因子实验队列、多轮反馈、失败记忆或增量更新，也不会把通过相关性门的候选自动变成策略和回测。纸面观察可以在 D 日保存目标并在下一有效交易日自动产生模拟订单，但没有“次日人工下单清单 → 用户回填真实成交 → 差异对账”的手工实盘辅助闭环。

真实交易边界必须保持原样：当前没有真实券商适配器，`paper_only=true`、`live_execution=false`、`can_submit_live=false`。项目状态对此有明确说明（[`project-status.md`](../project-status.md#L7-L27)）。

## 代码事实：因子候选、计算与筛选

| 问题 | 当前事实 | 判定 | 代码依据 |
|---|---|---|---|
| 候选是否自动生成 | `DEFAULT_ADVANCED_FACTORS` 从目录里筛选 `category="advanced"`；目录由 Python 装饰器静态注册。运行时实测目录共 21 个因子，默认高级候选正好 5 个：`breakout_dryup_quantile_20_ohlcv`、`liquidity_shock_reversal_decay_20_ohlcv`、`range_compression_volume_release_20_ohlcv`、`tail_asymmetry_rank_20_close`、`volume_price_divergence_corr_10_ohlcv`。 | 固定 5 个，自动计算；不是自动发现/生成。 | [`research.py`](../../quant_engine/factor/research.py#L23-L25)、[`catalog.py`](../../quant_engine/factor/catalog.py#L26-L58)、[`factors.py`](../../quant_engine/factor/factors.py#L319-L429)、[`factor-materials-v1.md`](factor-materials-v1.md#L11-L15) |
| 表达式是什么 | `formula`、`logic`、`operators` 是目录元数据；实际计算由每个 `FactorDefinition.compute` Python 回调完成。`compute_factor` 只检查所需列和返回索引对齐后直接调用回调。 | 有可审计元数据和固定 Python 实现；没有解析或执行公式字符串。 | [`catalog.py`](../../quant_engine/factor/catalog.py#L12-L23)、[`catalog.py`](../../quant_engine/factor/catalog.py#L76-L84) |
| 算子与 PIT 约束 | 算子在 `date × asset` 宽表上逐证券滚动；负 `delay` 明确拒绝。因子输出按当日截面做 1%/99% 缩尾和 Z-Score，避免全样本标准化改写历史值。 | 已具备。 | [`operators.py`](../../quant_engine/factor/operators.py#L19-L65)、[`operators.py`](../../quant_engine/factor/operators.py#L133-L228)、[`operators.py`](../../quant_engine/factor/operators.py#L248-L275) |
| IC 如何计算 | 每个日期对因子值与 forward return 做截面相关；默认先各自排名再做相关，即 Rank IC。少于 3 个共同有效证券的日期跳过。汇总包含 `ic_mean`、`ic_std`、`icir=mean/std`、正 IC 比例、t 值、p 值和观测数。 | 已具备统计计算。 | [`evaluation.py`](../../quant_engine/factor/evaluation.py#L44-L89)、[`evaluation.py`](../../quant_engine/factor/evaluation.py#L92-L136) |
| forward return 标签 | 脚本以信号日为 T，使用 T+1 开盘进入、T+1+持有期的开盘退出；默认持有期 5。标签只传入研究评估层。 | 已具备且与 next-open 协议一致。 | [`run_factor_research.py`](../../scripts/run_factor_research.py#L31-L43)、[`run_factor_research.py`](../../scripts/run_factor_research.py#L84-L96) |
| 因子相关性如何计算 | 每日对所有因子的共同有效证券做截面 Spearman 相关，至少 3 只证券；随后对日期取均值。当前实现使用“所有输入因子共同完整”的样本，不是逐因子对的 pairwise complete 样本。 | 已具备；样本口径需在扩展候选时特别记录。 | [`evaluation.py`](../../quant_engine/factor/evaluation.py#L212-L287) |
| 如何过相关性门 | 候选按调用顺序贪心筛选，与全部 baseline 和已经接纳的候选比较；最大绝对相关性 `> 0.70` 拒绝。没有有限因子值，或存在比较对象但相关性全部不可计算时，返回 `insufficient_evidence`。 | 已具备。 | [`research.py`](../../quant_engine/factor/research.py#L54-L70)、[`research.py`](../../quant_engine/factor/research.py#L83-L121) |
| IC 是否决定入选 | `accepted` 只由有限值和相关性门决定。IC 在有标签时计算并报告，但模块明确没有设置最小 IC。 | 缺少 IC/ICIR 晋级门。 | [`research.py`](../../quant_engine/factor/research.py#L63-L69)、[`research.py`](../../quant_engine/factor/research.py#L94-L115) |
| 基线因子 | CLI 加入 20 日滞后动量、20 日实现波动、20 日平均换手，以及当前字段可计算的旧内置因子。 | 已具备。 | [`run_factor_research.py`](../../scripts/run_factor_research.py#L17-L21)、[`run_factor_research.py`](../../scripts/run_factor_research.py#L46-L62) |
| 研究产物 | 成功运行后写 `candidate_factors.parquet`、`correlations.csv` 和 `report.json`；报告保存输入哈希、样本范围、标签定义、门槛、入选结果和 IC 摘要。 | 已具备一次运行的文件证据。 | [`run_factor_research.py`](../../scripts/run_factor_research.py#L98-L152) |
| 因子接入回测 | Strategy Protocol 可声明 `DataRequirement.factors`，解析输入列和预热窗口；`StrategyContext.get_factor` 返回当日 PIT 截面。 | 已接通计算接口。 | [`factor-materials-v1.md`](factor-materials-v1.md#L44-L65)、[`test_factor_pipeline.py`](../../tests/test_factor_pipeline.py#L152-L187) |
| 候选组合、分层和正式回测 | 库内有 `quantile_analysis`、多因子合成及中性化函数，但 `run_factor_research.py` 没有调用它们，也没有创建 Strategy 或启动事件驱动回测。 | 基础函数部分具备；候选到策略的流水线缺失。 | [`evaluation.py`](../../quant_engine/factor/evaluation.py#L143-L205)、[`synthesis.py`](../../quant_engine/factor/synthesis.py#L42-L115)、[`run_factor_research.py`](../../scripts/run_factor_research.py#L65-L153) |

当前 13 只本地证券的结果只能证明链路可运行。项目文档记录 5 个候选都过 0.70 相关性门，但 IC 很弱，并明确指出当前股票池不是历史动态成分，存在代表性和生存者偏差，不能晋级策略（[`factor-materials-v1.md`](factor-materials-v1.md#L21-L42)、[`project-status.md`](../project-status.md#L39-L47)）。

另一个会影响自动扩展的细节是候选顺序。目录列表按名称排序，贪心门又把“先入选候选”加入后续比较，因此候选集合增大后，名称顺序可能影响最终集合。自动流水线需要冻结候选顺序或改成顺序无关的聚类/图选择，并把规则写入实验协议。

## 自动研究能力差距

以下结论针对本次检查的因子目录、因子研究脚本及其直接测试，不把其他模块中的通用 `Run` 或纸面调仓恢复能力误算成因子研究能力。

| 能力 | 当前状态 | 事实说明 |
|---|---|---|
| LLM 生成因子表达式 | 缺失 | 因子候选是静态 Python 函数；因子路径中没有 LLM client、prompt、候选生成调用或模型输出 schema。 |
| 表达式 DSL / AST 校验 | 缺失 | 有 allowlist 风格的算子函数，但没有表达式 parser、AST、复杂度限制、字段/窗口静态检查或表达式指纹。`formula` 只是字符串元数据。 |
| 生成代码的隔离执行 | 缺失 | 固定受信任 Python 回调在进程内执行；没有给生成代码使用的 sandbox、资源限额或 import 禁止规则。现状不需要执行生成代码，因为根本没有生成代码入口。 |
| 多轮反馈优化 | 缺失 | `run_factor_research` 对给定候选列表做一次计算和一次贪心筛选，结果不会形成下一轮候选或自动修改表达式。 |
| 持久实验队列 | 缺失 | 因子 CLI 不创建数据库 `Run`，没有 queued/running/failed 状态、worker claim、lease 或 retry；只在成功路径向指定目录写文件。通用回测 `Run` 存在，但未被该脚本使用（[`schema.py`](../../server/models/schema.py#L33-L59)）。 |
| 失败记忆 | 部分且不足 | 候选级 `insufficient_evidence` / `correlation_limit_exceeded` 会进入成功报告；进程异常没有结构化失败记录，历史失败也不会参与候选去重或下一轮约束。 |
| 自动增量 | 缺失 | 每次从所选日期范围重新取数、全量计算并写目标目录；没有数据 watermark、按新增日期续算、数据修订失效或断点恢复。复用同一输出目录还会覆盖同名产物。 |
| 自动样本外验证 | 缺失 | CLI 只有一个 start/end 窗口，没有 Train/Validation/Sealed OOS 编排。项目文档把宽截面、PIT 基本面和独立 OOS 列为下一研究门。 |
| 自动回测与晋级 | 缺失 | 相关性门通过不会生成策略版本、运行成本/换手/容量压力回测，或把结果标为可观察。 |

## 代码事实：从日频信号到订单与成交

| 环节 | 当前已有 | 对“次日手工实盘”的限制 | 代码依据 |
|---|---|---|---|
| 观察准入 | 观察绑定已完成回测；带证据的运行会检查市场、策略指纹、数据 manifest、`eligible_for_observation` 和研究门。 | 这是可复用的策略发布门。 | [`observation.py`](../../server/services/observation.py#L659-L726)、[`observation.py`](../../server/services/observation.py#L767-L844) |
| D 日目标 | live checkpoint 把目标权重持久化到 `pending_signals` / `pending_signal_date`；下一有效日才消费。 | 已有“今天信号、次日执行”的时序基础。 | [`schema.py`](../../server/models/schema.py#L360-L369)、[`observation.py`](../../server/services/observation.py#L1697-L1707)、[`observation.py`](../../server/services/observation.py#L1737-L1800) |
| 订单提案 | 下一有效日使用新报价把目标权重换算为股数，A 股按 100 股取整；先做整篮现金和仓位预检，卖出项排在买入项前，计划和明细持久化。 | 提案生成逻辑可复用，但现有计划属于 paper 自动执行。 | [`observation.py`](../../server/services/observation.py#L301-L385)、[`schema.py`](../../server/models/schema.py#L389-L437) |
| 自动模拟执行 | `auto_trade=true` 时计划创建后马上逐项调用 `paper_trading.submit_order`；幂等键、租约、崩溃恢复、blocked 状态和显式 retry 已有。 | 没有人工审批停点；不是供用户拿去券商 App 下单的清单。 | [`observation.py`](../../server/services/observation.py#L425-L537)、[`observation.py`](../../server/services/observation.py#L1749-L1786) |
| `auto_trade=false` | 当前 tick 会计算并校验 signals，但不持久化待执行目标，也不返回权重明细；响应只有 `signals_count`。 | 不能充当手工订单建议模式。 | [`observation.py`](../../server/services/observation.py#L1787-L1803)、[`observation.py`](../../server/services/observation.py#L1583-L1594) |
| 调仓计划查询 | 观察列表只附上最新计划 ID 和状态；公开 API 有 blocked 计划 retry，但没有计划明细 GET、确认/拒绝或导出接口。前端也只显示状态和重试按钮。 | 用户看不到可操作的逐笔次日清单。 | [`observation.py`](../../server/services/observation.py#L943-L982)、[`observations.py`](../../server/api/observations.py#L145-L161)、[`PaperTrading.vue`](../../web/src/views/PaperTrading.vue#L73-L93) |
| 手工纸面订单 | API 接受用户输入的证券、方向、数量、价格和价格证据；服务进行交易日、T+1、现金和仓位风控。 | 它是一笔即时模拟下单，不是从策略清单逐项确认真实券商成交。 | [`paper.py`](../../server/api/paper.py#L48-L61)、[`paper.py`](../../server/api/paper.py#L86-L109)、[`paper_trading.py`](../../server/services/paper_trading.py#L531-L627) |
| 成交记录 | 纸面网关成交后立即写 `PaperOrder`、`PaperFill`、lot、账户现金/持仓和账本事件。 | 这些是项目内部模拟成交；没有用户回填的真实成交价/数量/费用/时间，也没有真实券商成交回报。 | [`paper_trading.py`](../../server/services/paper_trading.py#L628-L692)、[`schema.py`](../../server/models/schema.py#L154-L199) |
| 日频调度 | 收盘调度器运行观察 tick，再估值并生成日报；所有响应明确 `paper_only`。 | 可复用调度时点，但不能解释成真实交易自动化。 | [`paper_scheduler.py`](../../server/services/paper_scheduler.py#L387-L429)、[`paper_scheduler.py`](../../server/services/paper_scheduler.py#L459-L539) |

## 建议的最小自动因子流水线

目标应是先把现有可靠计算内核变成可恢复、可复现、可审计的有限状态流水线，再考虑是否让 LLM参与候选提出。最小版本不需要执行任意 Python 代码。

1. **定义受限候选协议。** 新增 `FactorExpressionSpec`，只允许当前 `operators.py` 中明确开放的算子、已知输入字段、正整数窗口和有限常数。保存规范化 AST、表达式哈希、金融假设、预期方向、父候选和生成来源。由解释器计算表达式，禁止 `eval`、`exec` 和任意 import。现有 5 个候选先转成该协议，作为回归基线。
2. **增加持久实验记录和 worker。** 新表至少保存 `experiment_id`、候选哈希、父实验、数据集/输入哈希、代码版本、股票池定义、日期分段、标签定义、成本假设、状态、lease、尝试次数、错误码和产物目录。状态建议限定为 `queued → validating → computing → screened → backtesting → completed/failed/rejected`。产物目录按 experiment ID 内容寻址，不能覆盖旧实验。
3. **分层门禁。** 第一层做 AST、字段、窗口、PIT 和数值稳定性检查；第二层只在训练段计算覆盖率、相关性和 IC；第三层在冻结验证/OOS 上计算 IC 稳定性、分层单调性、换手、成本、容量和行业/规模暴露。相关性门和 IC 门分开记录，不能继续把 `accepted` 理解成可交易。
4. **修正候选去重和顺序依赖。** 对规范化 AST 做精确去重；对值序列做相似度聚类，再用预注册规则在簇内选择代表，而不是依赖候选名称顺序。相关性统计应明确使用 pairwise 还是全体 complete-case，并保存每对有效日期/证券数。
5. **形成可控反馈。** 将结构化失败分为 `invalid_expression`、`lookahead_risk`、`missing_input`、`insufficient_coverage`、`high_correlation`、`weak_ic`、`unstable_oos`、`cost_failure` 等。下一轮只能读取这些摘要和已评估表达式哈希；Sealed OOS 结果不能回流生成或调参。
6. **做可审计增量。** 记录数据 watermark 和从表达式依赖递归推导的有效预热长度；嵌套滚动/滞后不能只取各窗口的最大值。新增交易日按有效预热范围重算；若算子依赖无限历史状态，还需保存可验证的状态检查点或完整重算。历史数据内容哈希变化时，标记依赖实验 stale 并重建，而不是静默拼接。
7. **明确晋级动作。** 只有冻结 OOS、成本/换手/容量和数据证据全部通过，才生成版本化 Strategy Protocol 配置并进入现有事件驱动回测。仍需经过现有 observation research gate，禁止由“相关性通过”直接开启纸面观察。

如果后续引入 LLM，建议把它限制为“输出 `FactorExpressionSpec` 候选和假设”，所有校验、计算、筛选和晋级由确定性代码完成。这样可以获得多轮候选探索，又不需要执行模型生成的 Python。

## 建议的次日手工实盘辅助闭环

该闭环只做决策与记账辅助，不连接真实券商，也不把用户回填当成已验证的券商回报。

1. **新增独立的 manual-assist 计划。** 复用 observation 的 D 日目标、次日交易日判定、整篮预检和卖出优先 sizing，但写入独立 `manual_execution_plan/item`，避免把真实手工成交与 `PaperOrder/PaperFill` 混为一谈。状态至少包含 `draft`、`ready_for_review`、`partially_confirmed`、`completed`、`cancelled`、`expired`。
2. **生成可查看、可导出的次日清单。** 每项展示执行日期、市场、证券、方向、计划数量、参考价及来源时间、价格容忍区间、预估金额/费用、T+1 可卖数量、计划后现金和仓位、信号/策略版本、数据哈希及阻断原因。提供 GET 明细和 CSV 导出；生成清单本身绝不调用 `submit_order`。
3. **设置明确的人为停点。** 用户在券商端自行下单。项目只能将条目标为“用户已查看/跳过/取消”，不能出现“已向券商提交”状态。
4. **回填成交确认。** 为每项允许多笔回填：实际成交数量、价格、费用、成交时间，以及可选的用户输入券商流水号。支持部分成交、未成交和拒单；每次修改追加审计事件，不覆盖历史。
5. **建立影子组合与差异对账。** 用已回填成交更新单独的 manual shadow ledger，计算计划数量与实际数量、计划均价与成交均价、费用、现金和持仓差异。次日信号 sizing 应以最近一次用户确认的实际持仓为准；未确认项默认未知并阻断自动推断。
6. **保留证据边界。** UI 和导出固定显示“手工录入、未经券商 API 验证”。未来只有接入用户选择的官方券商沙盒/接口并完成订单回报与对账验收后，才可增加 verified broker receipt 状态。

最小交付顺序建议是：先做持久因子实验记录与确定性表达式协议；再把候选接入冻结窗口回测和晋级门；最后基于已经验证的策略增加 manual-assist 计划、清单与成交回填。现有 `PaperRebalancePlan` 的 sizing、预检、幂等和恢复逻辑可以抽成纯提案服务复用，但现有 paper 表和 paper fill 语义应保持不变。

## 验证记录

本次执行了以下只读/测试检查，未修改业务代码：

```bash
rg --files -g 'docs/project-status.md' -g 'factor-materials-v1.md' \
  -g 'quant_engine/factor/*.py' -g 'scripts/run_factor_research.py' \
  -g '*paper*' -g '*observation*'

rg -n -i 'llm|prompt|candidate|queue|retry|failure|feedback|incremental' \
  quant_engine/factor scripts/run_factor_research.py tests/test_factor_pipeline.py

.venv/bin/python - <<'PY'
from quant_engine.factor import list_factor_definitions
from quant_engine.factor.research import DEFAULT_ADVANCED_FACTORS
print(len(list_factor_definitions()), len(DEFAULT_ADVANCED_FACTORS))
print(*DEFAULT_ADVANCED_FACTORS, sep='\n')
PY

.venv/bin/python -m pytest -q \
  tests/test_factor_pipeline.py tests/test_observations.py tests/test_paper_trading.py
```

运行时目录检查得到 `21` 个面板因子和 `5` 个默认高级候选。相关测试结果为 `78 passed in 8.62s`。测试覆盖本次结论所依赖的因子 PIT/相关性门、观察两阶段执行/恢复以及纸面订单账本；它不证明任何候选具有可交易预测能力，也不证明存在真实券商连接。

## 附录：主模型已读论坛片段的最小复现

以下两个论坛片段由主模型从其已读帖子中提供。本附录只验证给定代码/公式的局部行为，不推断作者完整实现，不评价帖子整体收益真伪。

### A. `pct_change` 后做全局 `shift(1)` 会串股票，且结果依赖行排序

主模型提供的 #151 片段先按 `ticker` 计算成交量变化率，再直接对返回的整列做 `shift(1)`。问题在于后一个 `shift` 已经脱离分组，会沿 DataFrame 的物理行顺序移动。

使用 A、B 两只股票和三个日期，成交量分别为 A=`100, 110, 132`、B=`200, 260, 390`，各股票变化率分别为 A=`NaN, 10%, 20%`、B=`NaN, 30%, 50%`。

当索引按 `(date, ticker)` 日序交错时，实际结果是：

| date | ticker | 当期 pct_change | 全局 shift | 组内 shift |
|---|---|---:|---:|---:|
| 2024-01-02 | A | NaN | NaN | NaN |
| 2024-01-02 | B | NaN | NaN | NaN |
| 2024-01-03 | A | 10% | NaN | NaN |
| 2024-01-03 | B | 30% | **10%（来自同日 A）** | NaN |
| 2024-01-04 | A | 20% | **30%（来自前日 B）** | 10% |
| 2024-01-04 | B | 50% | **20%（来自同日 A）** | 30% |

这里已经出现跨股污染；B 在 1 月 3 日和 1 月 4 日还读到了同日另一只股票的当期变化，而不是自己的上一期变化。

当相同数据按 `(ticker, date)` 排序时，股票分界处会出现明确的未来日期污染：

| ticker | date | 当期 pct_change | 全局 shift | 组内 shift |
|---|---|---:|---:|---:|
| A | 2024-01-02 | NaN | NaN | NaN |
| A | 2024-01-03 | 10% | NaN | NaN |
| A | 2024-01-04 | 20% | 10% | 10% |
| B | 2024-01-02 | NaN | **20%（来自 A 的 2024-01-04）** | NaN |
| B | 2024-01-03 | 30% | NaN | NaN |
| B | 2024-01-04 | 50% | 30% | 30% |

所以该写法既不是逐股票滞后，也不具备索引排序不变性。局部修正是让第二次位移仍在股票组内，例如：

```python
pct = df["volume"].groupby(level="ticker", sort=False).pct_change()
vol1 = pct.groupby(level="ticker", sort=False).shift(1)
```

更稳妥的验证应同时检查：交换 ticker 顺序不改变按键对齐后的结果、追加未来日期不改写历史因子。对于逐股时序算子，还应检查任一 ticker 的输入变化不影响其他 ticker；这一隔离要求不适用于截面排名、中性化等本来就需要跨证券计算的步骤。

### B. 两项相乘时，同正与同负都会得到正值

主模型提供的 #136 解释将原始分数概括为 `z_shadow × price_dev`，并声称两个负数相乘会得到较小值。只看这个乘法，四个简单数值为：

| z_shadow | price_dev | 乘积 |
|---:|---:|---:|
| +2 | +0.1 | +0.2 |
| -2 | -0.1 | **+0.2** |
| +2 | -0.1 | -0.2 |
| -2 | +0.1 | -0.2 |

因此，在绝对值相同的例子里，“同正”和“同负”完全同分，只有异号组合为负。若设计意图是专门压低“负 z + 负偏离”，单纯乘法无法表达这个方向信息，需要增加方向项、分段规则或明确下游排序方向。这个结论只针对给定局部公式；若完整实现还有未提供的负号、排序反转或条件分支，需对完整代码另行验证。

### C. 当前 `neutralize` 的截距与非法市值问题

项目当前实现使用 `pd.get_dummies(ind, drop_first=True)`，再把行业 dummy 与 `log(market_cap)` 拼成设计矩阵，但没有常数列（[`synthesis.py`](../../quant_engine/factor/synthesis.py#L169-L190)）。`drop_first=True` 通常要与截距配套；在这里，被省略的基准行业没有自己的常数项，其整体水平只能被迫通过 `log(market_cap)` 拟合。

用 10 只股票做确切复现：S01—S05 属于行业 A，S06—S10 属于行业 B；`log(market_cap)=1,2,...,10`；所有股票的 alpha 都恒为 10。恒定 alpha 在包含截距的中性化回归中应被完整移除。

当前 `neutralize` 返回的残差为：

```text
[7.692308, 5.384615, 3.076923, 0.769231, -1.538462,
 4.615385, 2.307692, 0.000000, -2.307692, -4.615385]
```

最大绝对残差为 `7.692308`；在同一个设计矩阵加入截距后，最大绝对残差为 `5.33e-15`，即浮点误差范围内的零。这个例子证明当前实现会把本应由截距吸收的公共水平残留在“中性化”结果中。可选修正是“截距 + `drop_first=True`”，或者“不加截距 + 保留全部行业 dummy”；前者更符合常见截面回归写法。

市值清洗还有独立问题。当前代码把 `±inf` 变为 NaN，却把零和负市值通过 `clip(lower=1e-10)` 变成正数，因此它们会以 `log(1e-10)=-23.025851` 进入回归，而不是作为非法观测剔除（[`synthesis.py`](../../quant_engine/factor/synthesis.py#L173-L182)）。

第二个 10 股例子将前 9 只有效股票设为 `log(market_cap)=1,...,9`、`alpha=2×log(market_cap)`，第 10 只设 `market_cap=0`、`alpha=100`。使用与当前实现相同的无截距回归：

```text
零市值 clip 后的 log 值       -23.025851
包含该非法观测时的回归系数     -2.125376
剔除该非法观测时的回归系数      2.000000
当前实现前 9 只有效股最大残差   37.128387
当前实现零市值行残差            51.061402
```

前 9 只在剔除非法行时本可得到零残差，但零市值行被映射到极端有限值后反转了系数并污染全部有效股票。建议先以 `market_cap > 0` 构造 valid mask，再取对数；零、负数和无穷值均保留为 NaN 并从当日回归剔除，同时在实验报告记录被剔除数量。截距修复和非法市值修复应分别测试，避免一个改动掩盖另一个问题。

本附录复现命令使用项目 `.venv`、pandas/numpy 和当前 `quant_engine.factor.synthesis.neutralize`，没有修改业务代码。两个面板例子各 6 行，两个中性化例子各 10 只股票；针对跨股值、未来日期值、乘法符号、截距残差和非法市值残差的数值断言已通过。
