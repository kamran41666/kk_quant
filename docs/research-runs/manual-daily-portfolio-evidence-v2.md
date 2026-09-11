# 日频组合证据读回补充规范 v2

> 类型：active-research-supplement
> 适用阶段：H2b 组合证据读回与 pair 审计
> 当前状态：实现与验收结果见 [`PROGRESS.md`](../../PROGRESS.md)，本文定义 v2 补充规范

## 1. 作用域与固定目录

本规范补充 [`日频组合证据合同 v1`](manual-daily-portfolio-evidence-v1.md)，只定义已落盘组合证据的无数据库副作用读回和 baseline/stress 配对检查。一次 artifact 必须保持固定 12 个文件：

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

v2 读回函数本身不登记数据库 artifact、不推进 release 状态，也不产生 `portfolio_passed`。其上层数据库登记与 resolver 见 [`日频组合数据库登记与晋级专题`](../manual-portfolio-promotion.md)；两者必须分开审查。

## 2. manifest v2 运行封套

`manifest.json` 的 `protocol_version` 为 `manual-daily-portfolio-evidence-v2`，并新增必填 `run_envelope`。封套必须保存：

- 完整 `bundle`，包括策略核心、场景和输入身份；
- `start_date`、`end_date`；
- 已经处于金额分币精度的 `initial_capital`；若输入不是分币精度必须拒绝，不能静默四舍五入；
- 期末规则 `terminal_policy=require_closed_cohorts`；
- 成本正文 `cost_policy`，由实际 scenario 成本规则生成；
- 八个模块的 `code_hashes`，覆盖生成、账本、输入、证据、源、读回、bundle 和有效规则模块。

manifest 顶层另存 UTC 时间戳 `generated_at`。读回将 v1 缺少 `run_envelope` 视为旧目录并拒绝继续语义验收。封套中的代码 hash、日期、初始资本、期末规则、成本正文和 bundle 必须能从结果与固定文件独立重建。

## 3. 源定位与输入重读

manifest 新增 `source_locator`，只保存两个相对 receipt：`dataset_manifest` 与 `benchmark_receipt`。每个 receipt 必须包含相对 `path`、文件 `size` 和 SHA-256；同时保存 `signal_value_column`。六类 source 仍完整保留在 `input_manifest.source_files`：daily、actions、securities、calendar、benchmark、signals。

读回先在受控根目录内重新核验两个 receipt，再以六类 source 重载输入并独立 replay。`source_locator` 与 `input_manifest.source_files` 中的路径必须相对，且不得路径逃逸或经过符号链接；`directory`、`allowed_root`、`source_root` 这些 API 参数可以是绝对路径。对旧 normalized manifest 中保留的历史绝对路径，允许按其原 basename 在 receipt 同目录迁移定位并核验原 SHA，不能改写冻结 manifest。receipt、source 文件、输入 manifest、bundle 与结果的身份必须逐项一致。

## 4. writer 与结果身份

writer 以实际 Arrow 落盘数据重建结果，再生成 metrics、replay、summary 和 manifest；展示字段不能反向充当事实来源。`result_hash` 表示持久化规范结果的 hash，必须由读回的固定文件重建并核对。`generator_result_hash` 只用于记录生成器内存结果的追溯，不能替代 `result_hash` 或成为晋级依据。

金额、价格、收益和指标按既定 Decimal/Arrow 规则处理；score、nav、benchmark_return 在比较前统一量化到 `decimal18`。金额与股份的既有精度、整手买入、零股清仓和费用规则不因 v2 读回而放宽。

## 5. `read_portfolio_artifact` 语义

`read_portfolio_artifact(directory, *, allowed_root, source_root)` 重新读取源 receipt、六类 source 和固定 12 文件，并独立重建结果、指标与 replay。它必须拒绝：

- 缺失或伪造 `run_envelope`，包括 v1 目录；
- 任一模块 `code_hashes` 不匹配；
- manifest 中绝对或越界的 source 路径，或任一 source 路径组件为符号链接；
- 必填字段为空、类型不符或主键重复；
- 结果、summary、replay、manifest 或输入身份之间的摘要不一致；
- 伪造摘要、伪造指标、伪造 replay 或未闭合 cohort；
- 源 receipt、六类 source 或落盘文件在读回期间发生变化。

通过只表示该 artifact 的目录、输入和独立 replay 通过语义检查，不表示已登记或可执行。

## 6. `audit_portfolio_pair` 语义

`audit_portfolio_pair(baseline_dir, stress_dir, *, allowed_root, source_root)` 只接受一份 baseline 和一份 stress。两份 artifact 必须共享全部非成本身份：`strategy_core_hash`、输入 manifest hash、起止日期、分币初始资本、`require_closed_cohorts`、八模块代码 hash 和 `source_locator`。只允许预注册成本场景差异。

`pair_hash` 绑定 pair 身份以及两份 `manifest.json` 的文件 SHA-256。任一 manifest 被替换、两场景身份不一致、场景不是 baseline/stress 或成本之外的字段变化，均拒绝配对。该检查仍是只读内存结果，不写数据库；数据库登记由独立服务在重新审计后完成。

## 7. 当前边界与入口

本文只补充 v2 读回协议；实现与验收状态以 [`PROGRESS.md`](../../PROGRESS.md) 及其最新指针为准。读回通过不能单独描述为晋级或真实委托资格；数据库登记、release 绑定和 `portfolio_passed` resolver 已在其上层专题实现，边界见 [`日频组合数据库登记与晋级专题`](../manual-portfolio-promotion.md)。

代码入口仅有以下两个函数：

- `quant_engine/backtest/manual_portfolio_artifacts.py:read_portfolio_artifact`
- `quant_engine/backtest/manual_portfolio_artifacts.py:audit_portfolio_pair`
