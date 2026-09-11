# kk_quant Agent 工作约定

本文件是仓库内所有开发 Agent 的常驻操作规范。自 2026-09-09 起，所有对话产生的项目修改统一追加到 [`PROGRESS.md`](PROGRESS.md)；专题文档保存设计、协议、验收和研究证据，不再各自维护“全项目当前状态”。

## 模型分工（2026-09-11 用户偏好）

- 机械代码、文档、配置写入以及所有测试执行，统一交给 `gpt-5.6-luna` 子代理；主代理负责架构、任务拆解、审查和结果核对，并将明确任务与验收标准交给 Luna。
- `gpt-5.6-luna` 不可用时，必须如实说明机械任务无法执行，不得静默改由更高等级模型代执行。

## 每次接手

1. 运行 `git status --short --branch`、`git log -3 --oneline --decorate`，确认当前分支、HEAD、上游差异和其他对话留下的未提交改动。
2. 完整阅读 [`PROGRESS.md`](PROGRESS.md)，再读取其指向的最新状态审查或专业文档。完成标准：建立记录机制后的所有项目修改和当前任务相关边界均已核对。
3. 按任务分支阅读专业资料：
   - 数据、研究质量、公司行动或回测证据：先看 [`docs/README.md`](docs/README.md) 的“研究与数据”路由。
   - 策略协议或新增策略：看 [`docs/strategy-protocol-v2.md`](docs/strategy-protocol-v2.md) 和 [`docs/strategy-library/README.md`](docs/strategy-library/README.md)。
   - 因子发现、因子评价或自动研究：看 [`docs/research-runs/forum-502342-factor-mining-review.md`](docs/research-runs/forum-502342-factor-mining-review.md) 与 [`docs/research-runs/factor-materials-v1.md`](docs/research-runs/factor-materials-v1.md)。
   - 纸面观察、实盘控制面或券商适配：看 [`docs/phase-4c-validation.md`](docs/phase-4c-validation.md)、[`docs/phase-4d-evidence-gated-observation.md`](docs/phase-4d-evidence-gated-observation.md) 和 [`docs/phase-3-plan.md`](docs/phase-3-plan.md)。
   - A股日频人工执行、策略晋级、成交回填或日终循环：先看 [`CONTEXT.md`](CONTEXT.md) 和 [`docs/manual-daily-trading-implementation-plan.md`](docs/manual-daily-trading-implementation-plan.md)。
   - 不确定文档用途或时效：查 [`docs/README.md`](docs/README.md)，以其中状态分类为准。
4. 在修改前核对实现和测试。事实优先级为：当前代码/运行结果/数据 manifest → `PROGRESS.md` 中后续变更 → 最新状态审查与当前专业规范 → 历史验收和施工记录。历史测试数字只证明当时版本。

## 开发与事实边界

- 使用中文沟通和维护项目文档。代码命名、协议字段和错误码保持现有英文风格。
- 保留用户及其他对话的工作树改动；只编辑当前任务需要的文件。共享工作树中先确认改动归属，再处理重叠文件。
- 数据、数据库、`backtest_result/` 和 `outputs/` 是机器本地资产。任何数量、日期、哈希和运行记录都注明机器与采集时间，不能推断另一台设备具备相同数据。
- 冻结研究目录按不可变证据处理。修改数据、执行语义、策略或引擎后创建新版本和新结果目录，不覆盖旧 manifest、协议和结果。
- 区分“代码具备能力”“本机有数据”“已通过研究门”“可纸面观察”“可真实执行”。只有证据完整且门禁通过，状态才能逐级提升。
- 实盘保持 fail-closed。当前没有已验收的真实券商适配器；不得把本地沙盒、订单草案、手工成交或纸面成交描述为真实委托。
- 回测与因子结论必须绑定股票池、时间切分、数据版本、执行时点、费用、代码版本和已知缺陷。已打开的留出集不得反复用于调参后继续称为独立样本外。
- 对缺陷先建立最小复现，修复后运行能够否证该问题的测试。通过既有测试不等于新发现的问题已经覆盖。

## Progress 同步协议

任何对话只要修改了代码、配置、文档、测试、数据协议或研究结论，都要在结束前更新 [`PROGRESS.md`](PROGRESS.md)。更新必须同时完成以下事项：

1. 在“记录”顶部追加一条，写北京时间、分支、HEAD/工作树、提交和推送状态。
2. 记录最终修改、实际验证、遗留边界和专业文档链接，不回填或重写以前对话的历史。
3. 功能契约、研究协议或证据需要完整说明时更新对应专业文档，Progress 只保存摘要和指针。
4. 专业文档有新增或状态改变时，同步维护 [`docs/README.md`](docs/README.md) 的分类和入口。

完成标准：代码、测试、`PROGRESS.md` 和专业文档相互一致；工作树状态与日志描述一致；阅读者能从 Progress 看到每次修改并通过链接进入所需证据。

## 验证与交付

- Python 改动按影响范围运行专项测试；跨模块、协议、存储或回测语义改动运行 `.venv/bin/python -m pytest -q`。
- 前端改动运行 `cd web && npm test && npm run build`。构建警告应记录为限制，不得描述为失败或忽略为不存在。
- 文档改动至少运行 `git diff --check`，并检查新增/修改的本地 Markdown 链接。
- 提交前检查未跟踪文件、敏感配置和大文件。只有用户已授权提交/推送时才执行 Git 外部写入，并报告提交哈希、分支、推送结果与验证结果。
- 最终答复先说明实际完成内容，再说明测试和仍存在的边界；不得用计划中的能力描述当前实现。
