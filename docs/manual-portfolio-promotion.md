# 日频组合数据库登记与晋级专题

> 类型：active implementation specification
> 适用范围：H2b 的 `manual_portfolio_*` 数据库登记、读回和 `research_passed → portfolio_passed` 评价
> 状态：实现验收见 [`PROGRESS.md`](../PROGRESS.md)；本文不声明任何具体策略已通过组合门

本文承接 [`日频组合证据合同 v1`](research-runs/manual-daily-portfolio-evidence-v1.md) 与 [`v2 读回补充规范`](research-runs/manual-daily-portfolio-evidence-v2.md)，定义落盘证据进入数据库后的边界。v2 reader 和 pair audit 本身仍是无数据库副作用的纯读回；本专题描述其上层登记服务、评价 resolver 和 API。

## 1. 登记输入与受控根

`register_manual_portfolio_pair(db, baseline_dir, stress_dir)` 只接收两份已落盘的 v2 目录。服务从默认配置读取 allowed root 与 source root，并重新审计固定 12 文件、receipt、源文件、输入 manifest、bundle、执行协议和 pair hash。HTTP `POST /api/v1/manual-trading/portfolio-pairs` 只接受 `baseline_path`、`stress_path` 两个非空相对目录路径；客户端不能覆写任何 root，也不能提交 metrics、passed 或 source 身份字段。

组合证据必须引用真实上游 `factor_training` 与 `factor_validation` artifact。两场景都必须绑定相同的 training/validation ID、证据 hash、数据身份和 label；验证信号必须覆盖完整验证窗口，方向、候选表达式、`rank` 角色、因子血缘、交易日历和退出尾部均重新核对。缺失、篡改、路径逃逸、符号链接、窗口不完整或身份不一致时登记失败。

## 2. 数据库写入与幂等

一次有效 pair 只登记两个 `ResearchEvidenceArtifact`：`manual_portfolio_baseline` 和 `manual_portfolio_stress`。两行写入位于同一 savepoint/事务边界内；任一场景失败不能留下半个 pair。`producer_entity_id` 使用稳定 `pair_hash`，相同 pair 内容再次请求返回原有两个 artifact ID，不新增行；已存在但身份、manifest 或 evidence hash 不一致时拒绝。失效或不完整的旧行不能被复用为新的 verified pair。

pair 内容幂等不等于统一 HTTP 幂等协议。当前接口不宣称已实现统一 `Idempotency-Key`；该能力属于 H4，客户端不得把自然 pair 幂等解释为通用 HTTP 幂等。

## 3. portfolio_passed resolver

portfolio resolver 只接受 artifact ID 引用，并从数据库和受控目录重读 baseline/stress，不信任调用方复制的收益、Sharpe、回撤、换手、成交率、replay 或 passed 字段。它必须验证：

- release 的 baseline bundle hash、策略 core fingerprint、市场、execution policy 与 pair 绑定，且 `auto_submit=false`；
- release 的 training/validation 引用与 pair 上游证据一致；唯一的 `research_passed` 评价存在且其 `evaluation_hash`、上一评价引用和上一评价 hash 链可重验；
- baseline 与 stress 的总收益和超额收益均为正；stress Sharpe 不低于 `0.8`，stress 最大回撤不高于 `0.2`；
- 两个场景的年化双边换手均在 policy 上限内，成交率均满足 policy 下限；
- 独立账务/source replay 通过，并且冻结执行协议能够从受控输入重现目标执行结果。

任一条件失败都追加 blocked evaluation，release 状态不前进。通过时评价保存 pair 引用、完整 checks、resolver/version/code hash、`previous_evaluation_id`、`previous_evaluation_hash` 和自身 `evaluation_hash`，形成追加式审查链。该状态仍只说明研究证据门通过，不代表券商连接、真实委托或自动执行已开启。

## 4. API 读回

`GET /api/v1/manual-trading/releases/{id}/evidence` 只读取该 release 的 `research_evidence` 和同 release 的 `StrategyPromotionEvaluation.evidence_refs_json`。它可返回 `training_artifact_id`、`validation_artifact_id`、`baseline_artifact_id`、`stress_artifact_id` 对应的 artifact 元数据，不回传任意路径或其他 release 的评价引用。每条评价同时返回上一评价 ID/hash 和自身 `evaluation_hash`，用于人工审查追加链。

登记接口响应的 `evidence_status` 为 `registered_not_promoted`。登记不会改变 release、manual account、manual ledger、`Paper*` 表或订单状态；晋级必须经过独立 promotion resolver，系统没有新增下单或自动执行入口。

## 5. 证据边界

本专题的实现状态、实际 fixture、测试和本机数据边界以 [`PROGRESS.md`](../PROGRESS.md) 的最新记录为准。v2 reader 通过只证明目录可读回和内部重放一致；数据库登记、上游血缘、release 绑定和 resolver 完成并通过后，才可记录 `portfolio_passed` 评价。一个内容不变且仍为 `verified` 的目录可以参加新 pair；已登记目录只要内容改版或 artifact 已 `invalidated`，就不能原目录复用，必须生成并重新登记新的证据版本。

本轮统一验收：后端全量 `813 passed, 2 warnings`；新增/相关 API、登记与晋级专项通过。`compileall quant_engine server tests`、本轮 8 个 Python 文件的关键 Ruff 规则、`git diff --check` 和本专题相关 Markdown 链接检查均通过。仍未闭合的是 holdout、pilot、manual_ready 和真实人工执行门；本轮 fixture 只验证数据库/API 证据链，不证明真实数据策略收益通过。
