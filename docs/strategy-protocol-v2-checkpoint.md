# Strategy Protocol v2 改造检查点

首次记录：2026-09-07；最近更新：2026-09-08（Asia/Shanghai）

基础分支：`codex/phase-3-live-readiness`
改造状态：**P0 闭环已完成，P1 体验改造进行中；不代表 Phase 4 完成**

## 1. 本轮目标

把当前平台从“数据库手填策略类路径 + 原始 JSON 参数 + 引擎分别解释策略输出”逐步改造成：

1. 策略实现只维护自身逻辑和声明；
2. 平台自动发现策略类；
3. 参数、数据需求、执行约束和分析输出具有机器可读的统一定义；
4. A 股与基金回测使用同一输出校验和公共数据访问接口；
5. API 与前端最终可依据声明自动生成策略表单，不再为每个策略硬编码。

## 2. 已确认的现状问题

当前 v1 协议只约束 `initialize()`、`generate_signals()` 和 `dict[str, float]`，但下列信息仍分散：

- 策略名称、市场、说明和参数存放在数据库记录中，策略代码本身无法完整自描述；
- 参数是未经结构校验的 JSON 对象，前端只能显示原始 JSON 文本；
- A 股策略通过 `context._data_handler` 私有属性取数，基金策略通过引擎注入 `_fund_history`；
- A 股与基金引擎各自实现一次目标权重校验；
- 调仓频率由每次回测请求指定，策略的建议频率无法被系统自动识别；
- 新策略必须先写代码，再手工填写模块类路径和数据库记录；
- 策略特有的诊断结果没有统一持久化格式。

现有撮合、费用、组合、交易日、数据覆盖校验、结果统计和 paper-only 安全边界属于引擎职责，应保留在策略之外。

## 3. 调研形成的架构决定

不建立可执行 JSON/YAML DSL。协议采用“Python 策略类 + 类型化 `StrategySpec` + 稳定运行时接口”：

- QuantConnect LEAN 把 Universe、Alpha、Portfolio Construction、Risk、Execution 通过稳定对象连接，说明执行与风控不应进入策略描述文件：<https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview>
- NautilusTrader 将策略实现与类型化 `StrategyConfig` 分离，并在回测和实时环境复用同一策略源码：<https://nautilustrader.io/docs/latest/concepts/strategies/>
- Backtrader 使用类级参数默认值和统一生命周期：<https://www.backtrader.com/docu/strategy/>、<https://www.backtrader.com/docu/quickstart/quickstart/>
- vn.py 从约定的 `strategies` 目录发现策略，并由 `parameters` / `variables` 驱动配置和监控界面：<https://www.vnpy.com/docs/cn/community/app/cta_strategy.html>
- vectorbt 的声明式输入、参数和输出命名适合参数扫描，但本项目仍需要事件驱动执行边界，因此只吸收其结构化参数/输出思想，不替换现有引擎：<https://vectorbt.dev/api/signals/factory/>

职责划分已经确定：

| 层 | 负责 | 不负责 |
|---|---|---|
| `StrategySpec` | 身份、版本、市场、参数模式、数据需求、默认调仓频率、输出能力、分析字段 | 可执行买卖逻辑、密钥、数据源实现、撮合规则 |
| `Strategy` 实现 | 读取已校验参数、通过公开上下文取数、生成目标仓位和诊断 | 下载数据、费用计算、交易日判断、成交模拟 |
| 回测引擎 | 数据供给、时间推进、输出校验、撮合、费用、组合、审计与结果 | 识别某个具体策略类型或硬编码其参数 |
| API / 前端 | 从注册表读取 Spec、创建策略实例、自动生成参数表单 | 解析策略算法或维护策略专用页面分支 |

## 4. 当前已实现代码

### 4.1 类型化协议核心

新增 `quant_engine/backtest/protocol.py`：

- `StrategySpec`：稳定策略身份、语义版本、市场、参数、数据、执行和分析声明；
- `ParameterSpec`：整数、数值、布尔、字符串类型，以及默认值、范围、候选值校验；
- `DataRequirement`：数据集、字段、回看长度、频率、复权和可选性；
- `AnalysisOutputSpec`：策略特有诊断字段定义；
- `StrategyOutput`：统一的目标权重和诊断输出；
- `normalize_strategy_output()`：兼容旧 `dict`，集中校验代码、有限权重、做多边界、总敞口和诊断字段。

`extensions` 只允许 JSON 可序列化的命名空间扩展，用于未来复杂策略增加非核心声明，避免频繁修改主协议。

### 4.2 自动发现基础

新增 `quant_engine/backtest/registry.py`：

- 扫描 `strategies` 包；
- 发现具体 `Strategy` 子类；
- 要求新策略声明有效 `StrategySpec`；
- 拒绝重复稳定 ID；
- 支持按安全的本地类路径加载；
- 保留无 Spec 旧策略的显式兼容加载模式。

> 历史检查点：此处记录的是 2026-09-07 状态；注册表已在后续续建中接入 API。

### 4.3 公共策略数据接口

`StrategyContext` 已增加：

- `universe`：当前引擎提供的证券集合；
- `history()`：统一的 Point-in-Time 历史数据读取；
- `current()`：当前时点字段读取；
- `record()`：策略诊断记录；
- `DataHandlerPortal` 和 `FundNavPortal`：分别适配 A 股日线与基金 NAV。

A 股和基金内置策略已迁移到公共接口；基金引擎暂时仍保留 `_fund_history` 兼容注入，避免破坏旧策略。

### 4.4 内置策略声明

以下策略已经增加 v2 Spec：

- `SmallCapValueStrategy`
- `FundNavMomentumStrategy`

参数不再从任意 `_strategy_kwargs` 直接解释，而由协议先校验，再通过只读 `self.params` 使用。

### 4.5 两套回测引擎的第一步统一

- A 股与基金引擎都调用 `normalize_strategy_output()`；
- 继续接受旧策略返回的 `dict[str, float]`；
- 新策略可以返回 `StrategyOutput`；
- 运行摘要开始记录 `strategy_spec` 和最终参数；
- 策略诊断写入 `strategy_outputs.parquet`。

## 5. 当前验证结果

本检查点创建前执行：

- `python -m compileall -q quant_engine strategies server`：通过；
- `pytest -q tests/test_fund_engine.py tests/test_server_security.py`：首次为 24 通过、1 失败；失败原因是旧测试使用 `lookback=1`，而新声明最小值误设为 2；兼容下限改为 1 后复验为 **25 通过**；
- `StrategyRegistry.discover()` 冒烟检查：成功发现 `fund-nav-momentum` 与 `small-cap-value` 两个内置策略；
- `git diff --check`：未发现空白错误，仅有 Git 的 LF/CRLF 工作区提示。

> 历史检查点：此处是 2026-09-07 的验证结论；最新全量结果见第 9 节。

## 6. 2026-09-07 时尚未实现

按优先级排列：

### P0：形成可实际使用的闭环

1. 将 `StrategyRegistry` 接入策略 API：增加自动目录接口，CRUD 按 Spec 校验类、市场和参数；
2. 修复策略创建接口当前向 ORM 传入未映射 `protocol_version` 字段的问题；
3. 回测 API 使用注册表加载策略，合并并校验运行时参数覆盖；
4. 调仓频率默认读取 Spec，同时允许回测请求显式覆盖并写入审计证据；
5. 策略指纹纳入 Spec 版本和规范化参数，确保策略声明变化可追踪；
6. 为协议、注册、参数校验、输出校验、两个引擎兼容路径补齐单元测试；
7. 运行后端全量测试，修复所有回归。

### P1：完成面向初学者和 Luna 的接入体验

1. 策略管理页从自动目录选择模板，不再要求用户手填 Python 类路径；
2. 根据 `ParameterSpec` 自动生成输入控件、范围、默认值和帮助文本；
3. 回测页根据所选策略显示参数控件，支持本次运行覆盖而不修改策略实例；
4. 根据 `DataRequirement` 提示缺失数据，服务端必须在执行前做强校验；
5. 读取 `strategy_outputs.parquet` 的通用 API 和前端展示；
6. 迁移数据库中已有策略实例，并显示真实协议兼容状态；
7. 更新策略页面文案，移除仍指向 v1 和原始 JSON 的说明。

### P1：新策略开发规范与模板

1. 正式文档 `docs/strategy-protocol-v2.md`；
2. 可复制的 `strategies/_template.py`；
3. 策略研究说明 Markdown 模板，明确数学表达、假设、数据偏差和适用边界；
4. 面向 Luna 的逐项机械检查表；
5. 示例策略及自动发现/回测验收测试。

### P2：后续增强

1. 参数网格/随机搜索与结果对比；
2. 数据需求的自动预热和覆盖报告；
3. 多数据集、多频率和基准数据适配器；
4. 更复杂输出类型的插件式扩展（保持核心协议不变）；
5. paper observation 使用同一注册表、参数验证和数据上下文；
6. 将两个回测循环中仍重复的生命周期编排抽成小型公共 runner；
7. 独立审查协议稳定性、前视偏差、兼容性和 UI 完整性。

## 7. 明确未改变的安全边界

- 本次只改策略接入与回测内部契约；
- `paper_only=true`、`can_submit_live=false`、`live_execution=false` 的边界不变；
- 未新增真实券商委托、资金转移或账户密钥处理；
- 交易日历和历史数据覆盖不足仍必须 fail closed；
- 现有费用、成交和结果统计逻辑没有迁入策略层。

## 8. 恢复施工顺序

1. 先复验本轮基金与安全测试；
2. 补协议/注册表单元测试；
3. 接入策略 API 和回测 API；
4. 实现前端自动参数表单；
5. 增加模板与正式接入规范；
6. 执行后端全量测试、前端测试与构建；
7. 独立审查后再判断 Strategy Protocol v2 是否完成。

## 9. 2026-09-08 续建结果

本轮已按恢复顺序完成 P0 闭环：

- 策略 API 已接入自动注册表，新增机器可读目录；创建和更新会校验实现、市场与规范化参数，并移除向 ORM 传入不存在字段的问题；
- 回测 API 通过同一注册表加载策略，支持本次运行参数覆盖；未显式指定调仓频率时读取 Spec，显式覆盖来源写入 manifest；
- 后台任务使用提交时固化的实现路径与参数快照，并在执行前校验指纹，不再受排队期间数据库策略记录修改影响；
- 策略指纹和 manifest 已覆盖 Spec ID/版本/协议版本及补齐默认值后的参数；
- 协议现在支持无默认值必填参数，严格校验诊断声明、类型和 JSON 可序列化边界；`StrategyOutput.diagnostics` 与 `ctx.record()` 共用校验；
- 参数化 lookback 已解析为运行时数据需求；A 股和基金会在执行前强制检查 dataset、频率、复权口径、必需字段和有效条数，不满足即 fail closed；
- 基金公共数据门户在初始化阶段 fail closed，运行期只暴露交易日 NAV；基金与 A 股统一使用周末/月末调仓语义；
- 恢复 v1 `ctx.stock_list` 兼容别名，并保证 A 股与基金策略在异常路径调用 `teardown()`；
- A 股和基金默认纸面观察已绑定同一公开数据上下文并接受 `StrategyOutput`，v2 内置策略可复用同一信号代码；
- 存量策略会按真实参数/市场显示兼容状态；JSON 诊断以稳定标量编码写入 Parquet，API 全量分页读取；
- 新增正式 v2 文档和可复制模板；策略管理页从自动目录生成参数控件，回测页支持本次参数覆盖与 Spec 默认频率；新增通用策略诊断读取和展示。

跨平台约束：新增文件路径通过 `pathlib` 组合，策略使用 Python 点路径，文档明确 UTF-8 与 Windows/macOS 命令差异；未引入平台专属运行时代码。

本轮验证结果：

- `python -m compileall -q quant_engine strategies server`：通过；
- 后端全量：**493 passed**，仅保留 1 个既有 matcher 截断告警；
- 前端：**6 passed**，`vue-tsc` 与 Vite 生产构建通过（684 modules）；
- `git diff --check`：通过。

P1 尚未完成的部分：历史数据库实例迁移、更完整的策略研究说明模板，以及面向低成本模型的机械检查表。自动预热仍属于 P2；当前实现是在现有回测输入上执行强覆盖门，不会隐式下载或补造历史数据。
