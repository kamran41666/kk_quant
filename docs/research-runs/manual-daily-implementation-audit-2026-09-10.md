# A股日频人工执行建设审查（2026-09-10）

> 类型：current-code-audit
> 审查基线：`5c225a4...fb92672`
> 审查范围：仅 `manual-daily-v1` 的 M1—M9 新增建设
> 审查机器与时间：当前 macOS 工作区，2026-09-10（Asia/Shanghai）

## 结论

远端提交已经建立M1—M9的大部分对象、服务、API和页面骨架，但同步时还不能称为可运行的日频人工闭环。审查实际复现了会误卖批次、绕过授权、破坏事务一致性、伪造前瞻观察和生成未对账复盘的缺陷。本轮先修复会改变真实影子账本或绕过门禁的路径；任何策略继续保持无人工执行资格。

当前准确状态：M1—M7具备分散的工程组件，但还缺统一编排与完整证据绑定；M8只有前瞻状态机，尚无真实30个交易日；M9只有人工计划成交回填机制，尚无用户真实成交与日终循环验收。

## 已复现并修复

| 严重度 | 原缺陷 | 本轮处理与否证测试 |
|---|---|---|
| P0 | 每个日决策同时创建两个45%批次 | 改为每个信号日只创建一个交替sleeve；旧sleeve未关闭时阻断复用 |
| P0 | 同代码任一批次到期会卖掉全部批次 | 计划按`cohort_id`逐持仓生成退出；账本卖出也按cohort过滤lot |
| P0 | `blocked/reconcile`使到期退出计划无法执行 | 仅阻断新增进入；到期退出仍生成可执行草案 |
| P0 | 计划成交、账本、授权激活和计划状态分三次提交 | 下层服务支持`flush`，顶层执行周期单事务提交；注入激活异常后事件、现金和计划全部回滚 |
| P0 | 计划成交丢失cohort，成交更正后item/plan仍显示旧数量 | 成交绑定计划项cohort；更正后按追加事件重算item、plan和cohort；同一原成交禁止二次更正 |
| P0 | 通用HTTP成交接口可无发布/授权/计划直接建仓 | 通用入口拒绝`fill/partial_fill`；正常经济成交只能走已查看、已确认的计划项接口 |
| P0 | 同一成交键重试在计划完成后失败，冲突payload也可能被吞掉 | 先执行完整payload幂等比对；同payload返回原事件，不同payload拒绝 |
| P0 | 30日前瞻可在一天内提交未来日期并立即通过 | release fingerprint必须一致；未来时间、未来观察日和跨交易日补录均拒绝；生产API不暴露测试时钟 |
| P0 | 从未对账或估值现金与账本冲突仍生成`ready`复盘 | 仅同日`matched/resolved`快照、相同ledger checkpoint和相同确认现金可生成`ready` |
| P1 | 标准日频label的`exit_phase=close`在worker中静默回落为open | worker兼容并优先读取phase字段；manual label强制horizon=1及T/T+1/T+2协议，阶段前置证据要求相同label spec |
| P1 | 回测退出重试写入错误日期类型；缺持仓收盘价按零估值 | 退出日期统一ISO并按到期日重试；持仓缺收盘价直接终止研究，不再产生虚假跌涨 |
| P1 | 回测忽略ST、停牌、零成交量、涨停与容量 | 进入前显式审查并记录，baseline/stress分别应用参与率；仍缺公司行动完整会计，见遗留项 |
| P1 | 晋级接受负的最大回撤并可替换冻结证据 | 回撤固定为0—1非负幅度；非有限/非法指标拒绝；`manual_ready`只接受与release冻结内容同hash证据且必须顺序经过paper门 |
| P1 | 两个pending授权可以分别获批 | 批准时再次检查账户有效授权，并检查当前有效期 |
| P1 | 已打开历史区间可换名字重新sealed | 固定拒绝2015、2019—2022、2023—2026.08区间；数据库中任何重叠窗口也不能重新登记 |
| P1 | `account_id`备份参数声称局部备份却导出所有账户 | 首版明确只支持全人工域备份；传账户scope直接失败关闭 |
| P2 | bundle公开JSON无法被CLI读回 | 新增严格`from_dict()`、协议和hash往返校验 |
| P2 | API测试依赖未声明 | dev依赖新增`httpx2>=2.0.0`，当前机器API测试可收集运行 |

## 仍未闭合的硬边界

1. **研究组合回测还不能用于晋级。** 新组合引擎仍没有接入与`ResearchLedger`同等级的公司行动、历史资格、退市回收和数据manifest核验；必须新建结果版本并重新跑长区间、广股票池baseline/stress，不能复用旧结果。
2. **晋级证据仍缺数据库对象链。** 本轮阻止了release创建后的证据替换，但release创建、holdout完成和pilot终结仍有调用方提供的结构化证据。下一阶段要让服务从实验、结果manifest、holdout access、pilot observation、review和reconciliation表读取ID及hash。
3. **前瞻日记录仍未绑定真实日决策/计划/复盘。** 时间防伪已经收紧，但`reconciled`和门禁布尔值仍是输入字段；在完成数据库引用链前，pilot结果不得使release晋级。
4. **worker没有生产handler。** CLI会把无handler任务置为blocked；尚未自动生成收盘决策、开/收盘计划或复盘，也没有多进程数据库CAS和长任务续租。
5. **HTTP覆盖不完整。** 仍缺release评价、decision生成、plan创建/刷新/修订、备份恢复和任务重试等闭环API；所有写请求的统一`Idempotency-Key`记录表也未实现。
6. **执行风险还缺日损失和累计回撤输入。** 单笔、现金、单票、总敞口和计划价格已检查；`max_daily_loss/max_drawdown`仍未从可信估值序列接入每次决策和成交前置门。
7. **账本多进程串行仍需加强。** 当前账户锁只覆盖单进程；SQLite应使用写事务锁，其他数据库应使用账户行锁并对sequence竞争分类重试。

## 后续施工顺序

1. **H2 证据闭合：** 建立release evidence resolver；用数据库ID/hash连接实验、组合结果、holdout、pilot、decision、review和reconciliation；移除客户端自报通过布尔值。
2. **H3 每日编排：** 增加收盘决策、开盘进入、收盘退出、估值等待、对账和复盘的持久handler及依赖图；实现数据库级租约认领和续租。
3. **H4 HTTP与操作台补全：** 接通release→decision→plan→view→confirm→fill→reconcile→review全链，完成统一幂等记录和失败恢复。
4. **H5 研究重跑：** 在公司行动、容量、历史资格、成本和manifest均绑定后，重新运行日频因子训练/验证、长区间广股票池组合回测；通过后才能创建新的sealed holdout。
5. **H6 真实前瞻与人工试运行：** 等待真实到达的连续30个交易日，逐日对账；通过后由用户显式设置资金限额并批准首次人工计划。

## H2a 实施状态（2026-09-10）

已新增`ResearchEvidenceArtifact`与`StrategyPromotionEvaluation`：前者保存可信worker产物的身份、逐文件路径/大小/SHA-256和总证据hash，后者追加保存release状态转换的证据引用、服务端解析检查、解析器版本/代码hash和决定。日频FactorExperiment完成后会自动登记训练/验证产物；release创建与晋级HTTP只接受artifact ID，不接受复制的收益或`passed`布尔值。

`draft → research_passed`解析器已经实现并重新核验候选表达式、实验状态、manual daily label、数据hash、评价policy、训练/验证时间隔离、数据库报告与`report.json`语义一致以及全部Parquet/JSON字节hash。旧`promote_release(..., evidence={...})`不能再提升状态；组合、holdout、paper和manual_ready解析器未具备足够底层证据时会追加blocked evaluation并保持当前release状态。

H2尚未整体完成。下一步H2b需要新增日频组合产物登记与重放证明，再将holdout结果和真实前瞻daily checkpoint接入相同resolver；当前任何release最多只能由新解析器到达`research_passed`。

本审查不修改冻结研究结果，也不评价远端提交中与日频闭环无关的新增内容。
