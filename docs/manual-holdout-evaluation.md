# H2c 经济 holdout 评估

本专题定义 `portfolio_passed` 之后的未见样本经济评估。旧的 preregistration bundle 保持不可变；一次评估只允许替换数据集 manifest、benchmark receipt 及其经验证的文件内容，不能重新封存整个输入范围，也不能从客户端接收 signal、策略参数、资本、成本或 passed 字段。

冻结请求由 operator、相对路径的 dataset manifest 和相对路径的 benchmark receipt 组成。路径相对受控根解析，输入 manifest、策略 bundle、执行协议、窗口日期和 release 身份由 holdout binding 固定。读取经济文件前必须先持久化 binding 对应的 access 事实；access 记录不足、binding/hash 不符或 window/evaluation 未完成时，resolver 关闭。

评估使用固定尾部 `20 + exit_offset` 的窗口语义。phase heartbeat 会在当前同步执行期间更新 lease，但不提供后台自动持续续租；租约失效时旧 token 不能提交结果。技术失败会将 evaluation 置为 `failed`，输入不能变更、失败不能重新封存；技术性重试只能复用同一冻结输入并形成新的 attempt。只有对未通过经济门调用 promotion 时才追加 blocked evaluation。完成并写出 `holdout_result` artifact 只表示结果已验证，不表示已经获得晋级资格。

`holdout_passed` 重新验证 artifact 的真实数据库 SHA 锚点、冻结 binding、access 链和完整 portfolio 父评价链，再依据 binding 冻结的 `ManualDailyPromotionPolicyV1` 检查 baseline/stress 净收益与超额收益、stress Sharpe、回撤、换手、成交率、样本数、独立 replay、quality errors 和最小 access 数。任何经济门失败都保持 `portfolio_passed` 并追加 blocked evaluation；通过时才原子追加 holdout 评价并引用既有 portfolio 评价。

同一 release 的完整预注册集合也是门禁输入。若存在多个 binding，所有窗口都必须各自有 completed、可验证的 evaluation/artifact，并通过同一冻结 policy；pending、running、failed、invalidated、负收益或其他失败窗口都会阻断。HTTP 仍以一个 `holdout_artifact_id` 作为入口，但 resolver 的结果 hash 会绑定集合内每个 binding、evaluation 和 artifact 的身份，不能挑选一份漂亮结果覆盖其他窗口。

每次 evaluation 的外部锚点固定为 25 项：顶层 holdout manifest、baseline/stress 各 12 项目录文件。冻结输入保存完整 read scope、source manifest、benchmark receipt、父 portfolio 身份和计算代码 hash；运行时先加载受控 source，再由因子表达式重算 signals，分别执行 baseline/stress，最后对 v3 归一化目录做独立 replay。任何文件、锚点、策略 core 或计算代码变化都会使 readback 失败。

resolver 在经济核验前捕获完整 snapshot，并在最终 release 状态 CAS 前取得 registry 写锁重新读取并逐值比较。snapshot 覆盖 release 全行、晋级链、全部 binding/window/evaluation/access、holdout 结果和上游 training/validation/baseline/stress artifact 正文与 hash；新增窗口、访问篡改、artifact 失效或缺少 snapshot 均拒绝提交。无需在提交锁内重新运行经济引擎。

`manual-backup-v4` 只备份人工域数据库元数据及其 hash 链，包含 evaluation、binding/access、release/artifact 谱系，不包含原始行情、Parquet、holdout 输出目录或其他源/结果字节。旧 v3 备份不能由当前 v4 恢复入口直接恢复，当前未提供 v3→v4 迁移器；旧文件保留且不可改写。历史 promotion evaluation 的已存 resolver hash 可单独按其历史版本校验，不能被新 H2c 结果重写或重新解释。

API 的 `/holdout-evaluations/{id}` 只返回状态和身份元数据；`/evidence` 只返回 `verify_completed_holdout` 产生的 metrics、hash 和安全 meta 白名单，不返回绝对路径。该入口与 Python service 都是同步计算入口，可供 CLI 复用；当前没有独立 CLI 命令，也不代表已接入自动 scheduler。
