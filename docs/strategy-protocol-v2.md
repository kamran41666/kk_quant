# kk_quant 统一策略协议 v2

## 目标与边界

策略只负责读取已校验参数、通过公开的 Point-in-Time 上下文取数，并输出目标权重和已声明诊断。交易日、调仓调度、撮合、费用、组合估值、数据覆盖和真实交易安全门均由平台负责。

协议当前支持 A 股与国内基金回测；美股回测和真实订单提交仍关闭。

## 最小接入流程

1. 复制 `strategies/_template.py`，同时修改文件名、类名和稳定 `StrategySpec.id`；类名不能以下划线开头。
2. 在 `StrategySpec` 声明版本、市场、参数、数据需求、建议调仓频率和诊断字段。
3. `initialize()` 只能初始化策略自身状态；初始化阶段的数据门户尚未定位到交易日，取数结果为空。
4. `generate_signals(dt)` 使用 `self.ctx.universe`、`history()` 和 `current()`，返回 `StrategyOutput`。不得访问网络、文件系统或券商。
5. 打开策略管理页；注册表会自动发现模板，表单由参数声明生成，不需要手填 Python 类路径或 JSON。

## 参数与输出

- 参数类型为 `integer`、`number`、`boolean` 或 `string`；未知参数、越界值和类型错误会在创建策略或提交回测时拒绝。
- `required=True, default=None` 表示没有默认值的必填参数。
- 目标权重必须是有限数；做多策略不得为负，总敞口不得超过 Spec 声明。
- 新策略的诊断键必须在 `analysis_outputs` 声明，类型必须一致且值必须可 JSON 序列化。
- `ctx.record()` 与 `StrategyOutput.diagnostics` 使用同一校验路径。

## 调仓与执行语义

- 未显式覆盖时，回测使用 `StrategySpec.rebalance_frequency`；覆盖值与来源会写入 manifest。
- 回测从用户开始日前加载满足 `DataRequirement` 的预热窗口；绩效和组合记录仍严格从用户指定开始日计算。
- A 股和基金统一采用每日、周末（默认周五）或月末信号日。
- A 股信号在下一交易日开盘执行；基金信号在下一有效 NAV 执行。
- 换仓先卖后买。日历、历史数据或股票池证据不足时 fail closed。

## 可复现证据

策略指纹覆盖本地类路径、源码哈希、Spec ID/版本/协议版本和补齐默认值后的规范化参数。回测提交时会把实现路径与最终参数快照写入 manifest，后台执行不读取可能已被修改的数据库参数，并在执行前拒绝代码或 Spec 已变化的任务。

## 跨平台开发

实现不得依赖 Windows 或 macOS 专属路径。文件路径使用 `pathlib`，包引用使用 `strategies.module.ClassName` 点路径，文本使用 UTF-8。验证命令在 macOS/Linux 可用 `python -m ...`，Windows 可用 `py -m ...`；项目代码不依赖命令解释器差异。
