# 日频研究闭环工作台首版

状态：2026-09-11 首版端到端交付。用户优先级是先打通端到端功能，再细化视觉与协议；本页记录当前可操作范围和事实边界。

## 使用入口

- 前端：`http://127.0.0.1:5173/daily-workflow`
- API：`/api/v1/daily-workflow/overview`、`/runs`、`/runs/{id}`、`/runs/{id}/advance`
- 主导航新增“日频闭环”，人工执行页顶部提供返回闭环入口。

进入页面后可看到真实数据库中的候选因子、实验、release、pilot、人工账户、计划和复盘统计。页面会从服务端恢复最近保存的工程演示 run；也可以创建新的 seed=7 演示运行。所有推进都携带 revision 和幂等键，按钮忙碌时禁止重复点击。

## 可操作流程

“一键体验全流程”按服务端状态顺序推进：`research → observe → plan → confirm → fill → review`。完成 review 后可推进 `next_day`，再继续下一日。成交步骤支持全部成交、部分成交和未成交；页面展示成交事实、现金、持仓、执行计划和日终复盘。刷新页面会通过 localStorage 保存的 run id 读回服务端状态。

研究步骤来自 `quant_engine/trading/manual_daily_runtime.py` 的实际计算，页面展示候选因子、基线/压力指标、策略净值、沪深300和上证对比，以及最大回撤和观察进度。图表可切换研究回测轨迹与执行演练净值；执行演练轨迹需要至少形成相应复盘状态。

## 模式隔离和边界

当前 run 明确标记为“合成数据·工程演示”，响应固定包含 `mode=engineering_demo`、`live_authorized=false`、`broker_connected=false`。编排服务仅写入 `WorkflowRun`/`WorkflowAction`；runtime 保持纯计算，不修改真实 release、pilot、人工账户、Paper 账户或券商链路。真实项目区只读展示数据库状态并提供因子研究、回测和人工执行入口。

本首版提供工作流状态机和可读的执行演练，不生成 trusted daily checkpoint，也不生成真实 pilot report 晋级证据。真实 30 日观察、生产 handlers、用户真实人工成交与日终循环验收、人工执行授权和 `manual_ready` 仍保持关闭；工程演示结果不能作为真实策略资格、paper observation 或交易证据。

## 验收记录

本机 macOS `npm test` 为 16 passed，`npm run build` 通过（保留既有 charts-core 大 chunk 提示）。独立临时 Chrome profile 通过 Playwright 实际点击一键流程、次日 partial 成交、刷新恢复和 390px 无横向溢出检查；截图仅保存在本机未跟踪的 `outputs/daily-workflow-desktop.png` 与 `outputs/daily-workflow-mobile.png`，不随 Git 交付；可复核代码见 [`DailyWorkflow.vue`](../web/src/views/DailyWorkflow.vue) 与 [`workflow.test.mjs`](../web/tests/workflow.test.mjs)。后端最终全量结果由本轮 backend 交付记录为 892 passed、2 warnings。UI 真实浏览器插件连接不可用，因此使用独立临时 profile 的系统 Chrome 后备验收，未读取现有 Chrome 用户 profile/cookies。
