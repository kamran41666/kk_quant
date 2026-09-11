# kk_quant 文档地图

本页回答“某类任务该读什么”。2026-09-09 之后的跨对话修改只看 [`../PROGRESS.md`](../PROGRESS.md)，建立该机制时的全项目状态看 [`project-state-audit-2026-09-09.md`](project-state-audit-2026-09-09.md)，Agent 操作规则看 [`../AGENTS.md`](../AGENTS.md)。历史文档里的分支名、测试数量、本地数据量和“当前”措辞均是当时证据。

## 当前规范与活动路线

| 主题 | 阅读入口 | 用途 |
|---|---|---|
| 后续修改记录 | [`PROGRESS.md`](../PROGRESS.md) | 2026-09-09 起每次对话的项目修改、验证和交接 |
| 中期状态基线 | [`project-state-audit-2026-09-09.md`](project-state-audit-2026-09-09.md) | 建立 Progress 机制时对全部链路、数据、风险和文档时效的审查快照 |
| Agent 工作流 | [`AGENTS.md`](../AGENTS.md) | 接手、事实核对、验证、Progress 同步和交付标准 |
| 系统改进路线 | [`research-platform-roadmap.md`](research-platform-roadmap.md) | 研究平台的能力差距和阶段验收方向；其中 2026-09-08 状态数据按历史快照阅读 |
| 策略开发 | [`strategy-protocol-v2.md`](strategy-protocol-v2.md) | 当前策略声明、生命周期、输入输出和复现要求 |
| 策略研究文档 | [`strategy-library/README.md`](strategy-library/README.md) | 策略来源、协议、实现和验证状态索引 |
| 研究质量 | [`phase-4e-research-quality-plan.md`](phase-4e-research-quality-plan.md) | 数据证据、样本外、成本和观察门；完成状态以 Progress 为准 |
| 自动因子路线 | [`research-runs/forum-502342-factor-mining-review.md`](research-runs/forum-502342-factor-mining-review.md) | 自动候选、回测反馈、审查缺陷和人工日频执行设计 |
| 因子研究工作流 | [`factor-research-workflow.md`](factor-research-workflow.md) | 受限表达式、持久候选/实验、worker、接口和当前评价边界 |
| 日频人工执行 | [`manual-daily-trading-implementation-plan.md`](manual-daily-trading-implementation-plan.md) | 用户人工买卖方向下的研究晋级、两批日频计划、成交回填、影子账本和实施阶段 |
| 日频建设审查 | [`research-runs/manual-daily-implementation-audit-2026-09-10.md`](research-runs/manual-daily-implementation-audit-2026-09-10.md) | 阅读远端M1—M9实现的已修缺陷、当前真实完成度和H2—H6收口顺序 |
| 日频组合证据合同 | [`research-runs/manual-daily-portfolio-evidence-v1.md`](research-runs/manual-daily-portfolio-evidence-v1.md) | 实现baseline/stress组合产物、公司行动、容量、重放和portfolio晋级前的冻结合同 |
| 日频组合证据读回补充规范 | [`research-runs/manual-daily-portfolio-evidence-v2.md`](research-runs/manual-daily-portfolio-evidence-v2.md) | 读取v2固定12文件、源receipt、独立replay及baseline/stress pair的补充规则 |
| 日频组合数据库登记与晋级 | [`manual-portfolio-promotion.md`](manual-portfolio-promotion.md) | 数据库 pair 登记、上游血缘、portfolio resolver、评价链和 API 读回边界 |
| H2c holdout预注册与访问 | [`manual-holdout-preregistration.md`](manual-holdout-preregistration.md) | 冻结窗口、策略绑定、访问事实、历史读取隔离、backup-v3元数据边界与尚未开放的经济评估 |
| 人工执行语言 | [`CONTEXT.md`](../CONTEXT.md) | 修改人工执行领域对象、状态或接口前统一术语 |

## 数据与研究

| 文档 | 状态 | 何时阅读 |
|---|---|---|
| [`market-data-sources.md`](market-data-sources.md) | 当前参考 | 修改 provider、来源标记、freshness 或历史/实时边界 |
| [`a-share-data-acquisition.md`](a-share-data-acquisition.md) | 专项操作记录 | 维护五年批量下载和原始响应归档 |
| [`phase-4a-fundamentals.md`](phase-4a-fundamentals.md) | 当前接口说明 | 导入或查询 PIT 基本面；建立机制时的本机数据量看中期审查，后续变化看 Progress |
| [`research-runs/ashare-multi-strategy-protocol.md`](research-runs/ashare-multi-strategy-protocol.md) | 冻结协议 | 解释 500 股 36 组实验如何预注册和切分 |
| [`research-runs/ashare-multi-strategy-research.md`](research-runs/ashare-multi-strategy-research.md) | 冻结结果解释 | 引用策略结果、来源、失败和证据边界 |
| [`research-runs/ashare-multi-strategy-results.json`](research-runs/ashare-multi-strategy-results.json) | 机器可读证据 | 读取正式实验索引与数值 |
| [`research-runs/ashare-backtest-defect-log.md`](research-runs/ashare-backtest-defect-log.md) | 累积缺陷记录 | 修改隔离研究引擎、数据标准化或公司行动前 |
| [`research-runs/ashare-multi-strategy-progress.md`](research-runs/ashare-multi-strategy-progress.md) | 历史施工记录 | 追溯 2026-09-08 实验过程；不作为全局进度入口 |
| [`research-runs/copa-price-only-v1-protocol.md`](research-runs/copa-price-only-v1-protocol.md) | 冻结协议 | 复现 COPA v1 固定矩阵 |
| [`research-runs/copa-price-only-v1-results.md`](research-runs/copa-price-only-v1-results.md) | 冻结结果 | 核对 COPA 未通过研究门的原因 |
| [`research-runs/factor-materials-v1.md`](research-runs/factor-materials-v1.md) | 当前因子基线 | 修改 21 个面板因子、相关性门或因子脚本 |
| [`research-runs/forum-factor-workflow-code-gap.md`](research-runs/forum-factor-workflow-code-gap.md) | 当前代码审查 | 实现自动因子、人工成交回填或修复中性化前 |
| [`research-runs/factor-portfolio-validation-v1.md`](research-runs/factor-portfolio-validation-v1.md) | 组合验证结果 | 查看自动因子进入原价、公司行动、费用和容量回测后的反例 |

研究 JSON 证据按原实验保存，不手工改写；新数据或执行语义使用新版本。

## 纸面交易、观察与实盘准备

| 文档 | 状态 | 何时阅读 |
|---|---|---|
| [`phase-4b-observations.md`](phase-4b-observations.md) | Phase 4B 历史说明 | 追溯最初观察生命周期；现行证据门和后续变化同时查 Phase 4D 与 Progress |
| [`phase-4c-plan.md`](phase-4c-plan.md) | 阶段范围 | 修改跨市场纸面账户和策略观察 |
| [`phase-4c-validation.md`](phase-4c-validation.md) | 历史验收快照 | 核对 2026-09-06 已验证行为；测试数字不代表当前基线 |
| [`phase-4d-evidence-gated-observation.md`](phase-4d-evidence-gated-observation.md) | 当前门禁说明 | 修改回测证据、策略指纹或观察准入 |
| [`engineering-validation.md`](engineering-validation.md) | 当前专项说明 | 验证调度、幂等和恢复但不评价策略收益 |
| [`phase-3-plan.md`](phase-3-plan.md) | 历史阶段总记录 | 修改实盘控制面前了解已交付和未交付边界 |
| [`phase-3b-sandbox.md`](phase-3b-sandbox.md) | 当前沙盒说明 | 修改确定性本地 broker 合同与恢复 |
| [`phase-3c-ops.md`](phase-3c-ops.md) | 当前运维说明 | 修改审计导出、备份或操作员令牌 |
| [`khquant-adoption.md`](khquant-adoption.md) | 技术评估快照 | 评估 MiniQMT/xtquant 数据或券商适配 |

## 产品与前端历史

- [`market-workbench-m1.md`](market-workbench-m1.md)、[`market-workbench-m2.md`](market-workbench-m2.md)、[`market-workbench-m3.md`](market-workbench-m3.md)、[`market-workbench-m4.md`](market-workbench-m4.md) 和 [`market-workbench-m4-breadth.md`](market-workbench-m4-breadth.md) 是行情工作台各阶段的交付/验收证据。
- [`frontend-iteration-log.md`](frontend-iteration-log.md) 是前端改动的历史流水；新的跨项目进度统一追加到 Progress，专项视觉证据仍可留在本文件。
- [`architecture-v1.md`](architecture-v1.md) 是 2026-09-03 的架构决策与历史现状基线。原则仍可参考，旧测试数、分支树和缺口清单不代表当前状态。
- [`phase-1-retrospective.md`](phase-1-retrospective.md) 与 [`superpowers/`](superpowers/) 是阶段一/二历史计划和回顾，不用于判断当前完成度。

## 兼容和已取代入口

- [`project-status.md`](project-status.md) 已由中期状态审查和根 [`PROGRESS.md`](../PROGRESS.md) 取代，仅保留旧链接兼容。
- [`strategy-protocol-v1.md`](strategy-protocol-v1.md) 是旧协议参考；新增或修改策略使用 v2。
- [`strategy-protocol-v2-checkpoint.md`](strategy-protocol-v2-checkpoint.md) 是 v2 建设过程和验收快照；最终接口以 [`strategy-protocol-v2.md`](strategy-protocol-v2.md) 与代码为准。
- [`knowledge-base.md`](knowledge-base.md) 是 2026-07-08 的研究资料库，内容和外部链接可能随时间变化；采用其中结论前重新核对原始来源，并将实际项目决定同步到 Progress 或研究协议。

## 维护规则

新增文档时只选择一种职责：当前规范、活动路线、冻结协议、冻结结果、历史验收或技术评估。若文档改变了项目能力、优先级或边界，必须同步 `PROGRESS.md`；若只保存专题细节，Progress 只写摘要和指针。阶段结束时将“当前”措辞改成明确日期的历史状态，避免形成第二份全局进度。
