# H2c 人工 holdout 预注册与访问边界

本专题定义 H2c 的工程身份登记边界。它只把一个已经通过研究和组合门的 release、组合评价链与一个尚未使用的数据窗口绑定起来，并记录窗口的冻结、打开和失效事实；本轮不产生 `holdout_passed`，也不执行经济评估。

## 预注册

`POST /api/v1/manual-trading/holdouts` 只接受 `release_id`、`dataset_id`、64 位 `data_content_hash`、`start_date`、`end_date` 和 `actor`，并强制使用 `Idempotency-Key`。服务端从数据库解析 release 的不可变 release hash、strategy core hash、唯一 `portfolio_passed` 评价、执行协议及其 hash，客户端不能传入 policy hash、策略 hash、经济结果或完成状态。窗口创建时为 `sealed`，日期重叠、历史已打开区间和父资格不足均拒绝。

绑定哈希覆盖 release、策略核心、组合评价、协议和窗口身份。相同幂等请求可读回同一绑定；相同 key 对不同请求或同一窗口重复绑定返回冲突。窗口一旦使用过不能重新封存或替换为另一个策略绑定。

## 打开与访问

`POST /holdouts/{binding_id}/access` 只接受非空 `actor` 与 `purpose`。服务端固定写入 `result_exposed=true`，并追加 `ResearchHoldoutAccess` 的 `binding_hash` 和 `payload_hash`。响应只包含 access 时间、绑定哈希和访问事实哈希，`evidence_status` 为 `access_recorded_only`；接口不读取、返回或评价持出集经济内容。元数据 GET 不计经济访问，也不会改变窗口状态。

## 失效与后续边界

`POST /holdouts/{binding_id}/invalidate` 要求非空原因。失效后访问被拒绝；本轮没有 complete 路由，也没有任何改变 release 到 `holdout_passed` 的路径。代码在这一步只处理冻结身份和审计事实，不读取持出集经济内容。

现有封存机制提供应用级不可变审计，不能替代磁盘加密。新的数据集评估身份必须独立绑定原策略 release，不改写原 bundle。实际 economic executor/resolver、持出集指标、晋级门和数据资产恢复属于后续专题。

因子研究队列和 worker 会在读取冻结 manifest 元数据后，以实际历史读取前缀检查已绑定的 `sealed/opened` 窗口；冲突在排队、重试或 worker 读行情前失败并持久化，普通研究只要不重叠仍可运行。这个应用层范围隔离不等同于磁盘加密，也无法追踪副本或外部脚本对数据的读取。
