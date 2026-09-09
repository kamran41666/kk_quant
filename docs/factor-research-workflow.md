# 受限因子研究工作流

> 类型：current-contract
> 状态：P2 自动模板候选、训练/验证、冻结bundle和组合回测已实现；模型生成和策略晋级仍未实现
> 首次实现：2026-09-09

## 目标与边界

本模块把人工、规则程序或后续模型提出的因子统一转换为 `FactorExpressionSpec`，再通过确定性解释器计算。表达式是 JSON 数据，不是 Python 源码；执行路径不使用 `eval`、`exec` 或模型提供的 import。

一次实验只完成表达式计算和截面训练/验证门，结果为对应阶段的 passed/rejected。passed只允许进入下一研究阶段；它不会创建策略、授权纸面观察或触发任何真实订单。早期v1工程实验保留 `evaluated_not_promoted` 兼容状态。

候选生成通过 `FactorCandidateGenerator` 接口隔离。当前 `template-v1` Adapter无需模型或密钥，生成4个可审查基线；后续模型Adapter也只能返回相同的 `FactorExpressionSpec`，不能改变校验、计算或门禁代码。

## 表达式协议

顶层字段包括：

| 字段 | 约束 |
|---|---|
| `name` | 小写下划线名称，3—80字符 |
| `hypothesis` | 必填金融假设，最多1000字符 |
| `direction` | `1` 或 `-1` |
| `role` | `rank`、`filter` 或 `risk` |
| `source` | `human`、模型名或素材来源标识，最多120字符 |
| `parent_hash` | 可选父候选SHA-256 |
| `expression` | 受限AST |

AST节点只有三类：`{"field":"close"}`、`{"constant":1.0}`、`{"op":"...","args":[],"params":{}}`。允许的市场字段为 `open/high/low/close/volume/amount/turnover_rate`；算子来自现有PIT面板算子，包括算术、rank/demean/scale、delay/delta、滚动统计、corr/cov、decay/slope、分位数和截面去极值标准化。

协议限制深度8、节点64、滚动/滞后窗口504、有限常数绝对值不超过1,000,000。delay必须为正；嵌套表达式的预热长度递归累加。规范化AST生成表达式哈希，同一表达式不会因为名称或JSON键顺序不同而重复登记；方向或角色冲突会拒绝，避免静默复用错误语义。

示例：

```json
{
  "name": "lagged_momentum_20",
  "hypothesis": "上一完整交易月的动量可能延续。",
  "direction": 1,
  "role": "rank",
  "source": "human",
  "expression": {
    "op": "winsorize_zscore",
    "args": [{
      "op": "sub",
      "args": [{
        "op": "div",
        "args": [
          {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 1}},
          {"op": "delay", "args": [{"field": "close"}], "params": {"periods": 21}}
        ]
      }, {"constant": 1.0}]
    }]
  }
}
```

## 持久状态与执行

`factor_candidate` 以表达式哈希去重并保存完整规范；`factor_experiment` 绑定候选、冻结数据集ID/内容哈希、日期和forward horizon。实验状态为：

```text
queued → running → completed
                 ↘ failed → queued（显式 retry）
```

每个实验还固定 `training/validation/holdout` 阶段。validation必须先存在同候选的`training_passed`，holdout必须先存在`validation_passed`；非训练实验不会进入候选生成memory。日期切分仍由研究协议负责，阶段字段不能把已经查看过的数据重新变成封存样本。

worker认领时写入owner、到期时间和attempt；过期租约可以被其他worker接管。认领使用条件更新减少并发重复执行。失败保存异常类型与摘要；重试保留attempt历史，并写入新的attempt产物目录。

HTTP入口：

- `POST/GET /api/v1/factor-research/candidates`
- `POST /api/v1/factor-research/candidates/generate`
- `GET /api/v1/factor-research/candidates/{candidate_id}`
- `POST/GET /api/v1/factor-research/experiments`
- `GET /api/v1/factor-research/experiments/{experiment_id}`
- `POST /api/v1/factor-research/experiments/{experiment_id}/retry`
- `GET /api/v1/factor-research/memory`

执行入口：

```sh
.venv/bin/python -m scripts.run_factor_worker --worker-id local-factor-worker --max-jobs 1
```

worker只扫描 `data/research/**/manifest.json` 中登记的数据集，重新校验daily/securities文件SHA-256，拒绝任意路径和内容变化。产物写入 `backtest_result/factor-experiments/<experiment-id>[-attemptN]/`，包括 `factor_values.parquet`、`ic_series.parquet`、`quantile_returns.parquet` 和 `report.json`，不会覆盖既有目录。

## 当前评价口径

- 信号使用冻结研究集的调整后OHLC，成交量、成交额和换手保留原始口径。
- 上市未满180天、退市后、ST和停牌行退出当日研究截面。
- 标签为T日信号、T+1调整开盘进入、T+1+h调整开盘退出。
- 输出包括覆盖率、逐日Rank IC、前后半段方向稳定性、三类基线相关性和五分组收益；零方差截面直接跳过。
- 当前t值/p值没有处理重叠标签、自相关和多重尝试，只能作为诊断。

默认训练门是显式研究政策，并非通用金融定律：覆盖率≥70%、有效IC截面≥60、方向调整后IC≥0.02、与三类基线最大绝对相关性≤0.70、前后半段方向IC均为正、方向调整后的Q5−Q1均值≥0。调用方可以在创建实验时提交并冻结其他阈值；同一实验身份若阈值不同会拒绝静默复用。

`factor-memory-v1` 只返回表达式哈希、训练区间、持有期、决策、失败门和核心指标，不返回大段代码或最终留出详情。旧v1实验标记为 `legacy_evaluated`，不会被误报为执行失败。

## 2026-09-09 工程基线

通过API登记 `engineering_lagged_momentum_20`，表达式哈希 `5da9f1a8…`；实验 `ceeaf8ad8630` 绑定 `ashare-inception-2014-v1/normalized-v2`，覆盖2015-01-05—2015-06-30、500股、5日标签。

实际输出59,381行、48,892个有限值，覆盖率82.34%，113个有效IC截面，IC均值−0.09233。该候选的声明方向为正，但短区间IC为负，因此仍是工程链验证对象，不进入策略晋级。完整结果位于本机 `backtest_result/factor-experiments/ceeaf8ad8630/`，不随Git同步。

训练门基线实验 `3e396094a009` 将区间扩展到2015全年：输出121,756行、96,191个有限值，覆盖率79.00%，238个有效IC截面。覆盖率和样本数通过；方向IC−0.09516、与现有20日动量相关性1.0、前后半段方向均为负、方向Q5−Q1均值−1.597%，因此四项门禁失败，结果为 `training_rejected`。该结果验证系统能准确识别重复且方向不符的候选，不用于宣称反向策略有效。

### template-v1 首轮搜索

生成器自动登记4个候选，并在2015全年500股训练；训练通过者继续进入2019—2022验证：

| 候选 | 训练结果 | 验证结果 | 验证方向IC | 验证最大基线相关性 | 验证Q5−Q1 |
|---|---|---|---:|---:|---:|
| 5日反转 | passed | passed | 0.03264 | 0.41050 | 0.28723% |
| 量价背离10日 | passed | passed | 0.03438 | 0.26703 | 0.16828% |
| 振幅压缩20日 | passed | passed | 0.03073 | 0.24684 | 0.17186% |
| 低波20日 | rejected | 未进入验证 | — | 训练相关性1.0 | — |

这些仍是重叠5日标签的截面研究结果。2019—2022验证通过只允许进入下一步组合回测，不代表扣除换手、费用和容量后仍有效。项目中的2023—2026.08留出此前已经打开，本轮没有把它重新标作holdout。

## 冻结组合回测

只有role=rank且显式指定一条`training_passed`和一条`validation_passed`证据的候选可以创建 `FrozenFactorStrategyBundle`。冻结过程复核候选归属、阶段、决定、数据ID/哈希、forward horizon、门政策、时间先后、DB/report一致性和四类实验产物哈希。bundle hash还覆盖方向、角色、TopN、仓位、调仓频率及执行代码哈希；策略运行时只接收bundle和表达式，不访问数据库。

动态 `ResearchDataPortal` 在每个历史窗口日期重新应用上市180天、退市、ST、停牌和有效行情掩码，再使用调整信号OHLC计算表达式。第一版独立组合只支持rank角色；filter/risk缺少阈值和基础组合语义时fail-closed。

首轮固定周频Top50、90%仓位，在2019—2022分别跑baseline/stress。5日反转baseline累计−16.53%，stress−57.46%；量价背离baseline累计8.97%，stress−36.82%。年化换手约56—74倍，两者均不满足成本稳健性，不晋级。完整口径与数据见 [`research-runs/factor-portfolio-validation-v1.md`](research-runs/factor-portfolio-validation-v1.md)。

## 下一阶段

1. 在训练范围内预注册换手缓冲、月频或双rank组合的新版本；2019—2022已查看结果不得继续用作独立验证。
2. 增加模型候选生成Adapter；模型只能输出本协议，确定性模块负责校验和执行。
3. 为新的、从未打开的数据版本设计真正的holdout并保存访问状态。
4. 将组合回测结果接入因子工作台；当前页面已支持候选、实验、门禁和memory审查。
