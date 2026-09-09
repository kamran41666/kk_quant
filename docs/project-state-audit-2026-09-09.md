# kk_quant 中期状态审查

> 类型：dated-audit
> 状态：2026-09-09 事实快照，后续项目修改以根目录 [`PROGRESS.md`](../PROGRESS.md) 为增量记录
> 审查范围：当前代码、测试、SQLite、冻结研究 manifest/结果和仓库文档；未重新在线认证公共数据源

## 总体判断

项目已形成“数据检查 → 策略协议 → 事件驱动回测 → 绩效比较 → 证据门禁 → 纸面观察”的主要链路。A 股和国内基金日频研究可以运行；Web 工作台覆盖行情、数据、策略、回测、模拟和实盘准备。独立的 500 股长周期研究完成了 36 组正式实验和账务复核，但公司行动证据仍不足，因此 36 组均保持 `validated=false`。

系统当前最大的结构性断点有四个：普通数据/因子链与独立 500 股研究链尚未统一；因子候选仍是静态集合，没有自动生成—反馈—持久队列；中性化存在已复现数值缺陷；人工实盘辅助缺少订单清单、分批持仓和实际成交回填。真实券商适配器尚未接入，所有实盘能力继续关闭。

## 链路能力

| 链路 | 已具备 | 边界或缺口 | 深入阅读 |
|---|---|---|---|
| 数据采集与存储 | Parquet 日线、SQLite 元数据、交易日历、事件复权；A 股公开源、国内基金 NAV、美股和黄金 provider；下载/导入、失败状态、覆盖报告 | 公开源无生产 SLA；本机 PIT 基本面和历史指数成分均为空；本地资产不随 Git 跨设备同步 | [数据源](market-data-sources.md)、[A股采集](a-share-data-acquisition.md)、[基本面](phase-4a-fundamentals.md) |
| 市场工作台 | A 股、基金、美股、黄金总览；搜索、分页、K线、指标、市场宽度、来源/freshness | 低频公开行情研究界面；指数不可直接交易；断源时降级或阻断 | [M4验收](market-workbench-m4.md)、[宽度](market-workbench-m4-breadth.md) |
| 数据质量 | `/data` 库存、逐证券覆盖预检、内容哈希、历史资格、异常字段报告 | 普通库存范围小；文件存在不等于逐证券全期完整 | [研究路线](research-platform-roadmap.md) |
| 策略协议 | Protocol v2 自动发现、类型化参数、数据需求/预热、目标权重、诊断输出、版本和研究文档绑定 | 协议声明不保证每个引擎支持该数据；历史实例迁移仍可加强 | [v2协议](strategy-protocol-v2.md)、[策略库](strategy-library/README.md) |
| 策略目录 | 代码发现 6 个：COPA、基金 NAV 动量、小盘价值、低波、动量、短期反转 | 研究候选不等于通过研究门；数据库只有 3 个策略实例 | [策略库](strategy-library/README.md)、[多策略研究](research-runs/ashare-multi-strategy-research.md) |
| 通用 A 股回测 | 事件驱动、次日开盘、T+1、费用/滑点、涨跌停/停牌、覆盖与日历门、异步任务/取消、结果 manifest | 普通股票池/数据路径不能替代动态历史全市场和冻结研究证据 | [研究质量](phase-4e-research-quality-plan.md) |
| 国内基金回测 | 不可变 NAV 数据集、下一有效 NAV 执行、交易日历、费用敏感性、生命周期 | 本机基金 NAV 数据集为 0；费率是研究假设 | [跨市场验收](phase-4c-validation.md) |
| 独立长周期回测 | 原价成交/调整信号分离、公司行动、应收与红股、昨量参与率、费用/容量压力、逐日账本重放、冻结代码 | 固定 500 股而非动态全 A；隔离引擎拒绝因子需求；市场事件证据仍不完整 | [协议](research-runs/ashare-multi-strategy-protocol.md)、[结果](research-runs/ashare-multi-strategy-research.md)、[缺陷](research-runs/ashare-backtest-defect-log.md) |
| 因子研究 | legacy 类因子 12 个；面板目录 21 个；PIT 时序算子、截面处理、IC/ICIR、分层、相关性、Fama–MacBeth、静态候选筛选 | 默认高级候选固定 5 个；无表达式 DSL、自动生成、持久任务、反馈记忆或策略自动晋级；当前因子样本仅 13 股 | [因子材料](research-runs/factor-materials-v1.md)、[自动化路线](research-runs/forum-502342-factor-mining-review.md) |
| 绩效与报告 | 风险收益、Alpha/Beta、交易统计和多实验比较；策略/上证/沪深300三线、上涨/回撤区间、PNG/HTML/CSV/Excel | 图表不能替代数据、成本和样本外证据；指数含息口径需明确 | [研究结果](research-runs/ashare-multi-strategy-research.md)、[前端记录](frontend-iteration-log.md) |
| 纸面交易 | 持久账户、订单、成交、lot、现金/持仓/估值/账本、T+1、幂等、市场规则、风险检查、日报和对账 | 本机账户为 0；legacy `SimulationAccount` 重启锁仓恢复存在缺陷 | [跨市场计划](phase-4c-plan.md)、[验收](phase-4c-validation.md) |
| 策略观察 | A 股/基金回测证据绑定，7/30天任务、生命周期、两阶段信号/执行、持久计划、租约和恢复 | 美股策略观察阻断；`auto_trade=false` 不产生可操作人工清单；无真实成交回填 | [观察](phase-4b-observations.md)、[证据门](phase-4d-evidence-gated-observation.md) |
| 实盘准备 | kill switch、连接元数据、凭证引用、订单草案/确认、审计导出、备份校验；确定性本地沙盒支持部分成交/撤单/恢复 | 无真实券商认证、报单、回报或资金对账；`can_submit_live=false` | [阶段记录](phase-3-plan.md)、[沙盒](phase-3b-sandbox.md)、[运维](phase-3c-ops.md) |
| 前端与部署 | Vue 3/TypeScript/Vite/ECharts，7个主要页面；FastAPI、WebSocket、Docker Compose | 单用户本地边界；无通用用户会话认证和生产 SLA；图表核心包约 558KB | [README](../README.md)、[前端记录](frontend-iteration-log.md) |

## 本机事实

- Git：`phase4-factor-develop` @ `55e5156`，审查开始时与上游差异 `0/0`。
- 普通日线库存：13 只证券、455 个分区、27,278 行，合计日期边界 2018-01-02—2026-08-31；这不证明每只证券全区间完整。
- `data/meta.db`：`stock_info=5558`、`fundamentals=0`、`index_components=0`、`data_versions=1`、`data_update_log=2`。
- `data/server.db`：`run=48`、`strategy=3`、`live_control=1`；纸面账户、观察、基金 NAV 数据集、券商连接与订单表均为 0。
- `normalized-v2`：500 只证券、1,625,850 条日线、4,645 条公司行动、3,317 个日历交易日，4 个 Parquet SHA-256 与 manifest 一致；保留 37 只后来退市证券。
- 正式研究区间 2015-01-05—2026-08-31，共 2,834 个交易日；36 个实验全部 completed、全部 `validated=false`，4 个阶段账务审计合计 36/36 通过。
- 低波全段基线 CAGR 8.66%、最大回撤 44.06%；动量与短期反转的时间留出均为负。以上是冻结假设下的历史结果，不是收益承诺。

## 已核实的高优先级问题

1. `quant_engine/factor/synthesis.py` 使用 `drop_first=True` 却没有回归截距，并把非正市值裁成极小正数进入 `log`。精确线性小样本本应得到零残差，当前最大绝对残差为 1.109785；零市值仍得到有限残差。现有测试只检查形状，未覆盖经济语义。
2. 500 股研究仍有 50 条参考价调整未完全解释。账本审计证明内部现金/股数/净值一致，不证明配股、重整权益、差异化分配和退市回收值来自完整真实证据。
3. 因子相关性门按候选顺序贪心筛选，`accepted` 没有最低 IC 门；相关矩阵使用全部因子共同非缺失样本，重叠标签的普通 t 统计也未处理序列相关与多重搜索。
4. `ResearchBacktestEngine` 对因子请求 fail-closed 是正确门禁，但 500 股归档尚未成为 `DataAPI`/面板因子脚本的正式数据源。
5. legacy `server/services/paper_engine.py` 没有可靠持久化分批锁仓；恢复后可能把当日买入变为可卖。当前持久纸面交易链不应与它混称。
6. 作者帖子所述半仓日频方式是 T 日信号、T+1 开盘买、T+2 收盘卖；项目当前因子标签默认是 T+1 开盘到更后开盘，不能只改 rebalance frequency 就声称复现。

服务端与前端还有三个产品缺口：异步回测任务和取消集合是进程内状态，没有持久 worker；“最新信号”前端状态没有对应服务端事件生产者；纸面页尚未展示已有的订单/成交/账本查询结果。账户接入页也未暴露已存在的实盘草案 API，这符合当前收敛界面的选择，但意味着控制面后端能力与可见 UI 不完全对齐。

## 文档时效审查

审查发现旧状态源重复：`docs/project-status.md`、`CLAUDE.md`、`research-platform-roadmap.md` 和专题 `ashare-multi-strategy-progress.md` 都曾包含“当前/接手”信息。主要过时点如下：

- `docs/project-status.md` 的旧分支和“未提交/未推送”已经失效。
- `CLAUDE.md` 的 12 个因子描述只覆盖 legacy 系统，容易误读为全部因子；其 Git 工作流与当前通用 Agent 规则重复。
- `architecture-v1.md` 前半的“当前现状”称无调度、无广播、无持久账户和无图表，已被后续实现推翻；后半又追加新实现，文档内部跨时点。
- `research-platform-roadmap.md` 中“没有参与率模型、数据库回测为空、未做批量采集”等是 2026-09-08 早期快照，已不适用于隔离研究和当前数据库。
- `phase-3-plan.md`、`phase-4e-research-quality-plan.md`、`phase-4c-validation.md` 和 M1—M4 文档中的分支与测试数字是历史验收，不是当前基线。
- `phase-4b-observations.md` 的“基金观察未开放”是 Phase 4B 历史边界；当前基金观察已按独立证据开放。
- `phase-4c-plan.md` 仍称 blocked 计划 retry 未完成，但对应 API 和测试已经存在；剩余缺口是计划明细/审批与人工成交回填。
- `ashare-multi-strategy-progress.md` 中“补充行动正在建设、图表待联调”与同文最终交付矛盾。
- `factor-materials-v1.md` 的“回测协议接入”只适用于支持 factor portal 的通用链路；隔离研究引擎仍明确拒绝。
- `strategy-protocol-v1.md` 已被 v2 取代；`strategy-protocol-v2-checkpoint.md` 的“尚未实现”章节是当时状态，后文已交付大部分内容。

本次清理采用低风险方式：建立文档地图和状态标签，保留历史验证数字与冻结证据，不大规模移动路径；旧链接继续有效。全项目后续修改统一写入根 `PROGRESS.md`。

## 审查验证

- 子链路定向测试：数据/研究相关 125 项通过；服务/前端审计复核了上次全量 678 项和前端 10 项基线。
- 上一提交的全量基线：Python 678 项通过（1 个预期 warning）；Web 10 项、类型检查和生产构建通过。
- 本审查未重跑 36 组长周期实验，也未重新联网验证第三方提供商。
