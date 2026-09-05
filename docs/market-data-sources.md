# 市场行情数据源

## 当前接入

市场页 `/market` 使用一个有明确来源标记的 provider 链：

1. `tencent:qt`：腾讯财经公开快照接口，适合低频自选股刷新，不需要 API key。
2. `akshare:eastmoney` → `akshare:sina`：现有 AKShare 双源回退。

快照接口为 `https://qt.gtimg.cn/q=sz000001,sh600519`，返回 GBK 编码的 `~` 分隔字段；日 K 接口为 `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get`，返回 JSON 的 `qfqday`/`hfqday`/`day` 序列。代码把成交量从手换算成股、成交额从万元换算成人民币元，并保留 `source`、`as_of`、`received_at`、`freshness` 和 `is_fallback`。

当本地 parquet 日线无法覆盖市场页请求的日期窗口时，`/api/v1/market/daily/{code}` 会请求腾讯日 K；如果所有源失败，则返回结构化 `503`，不使用缓存样例或生成值冒充实时数据。

为避免静默截断，日 K 远程回退的请求区间最多 1000 个自然日；更长区间会返回明确错误，调用方应拆分区间或先补齐本地数据。非法日期和倒序日期返回结构化 `422`。本地日线仅部分覆盖请求窗口时也会走远程回退，不会把一小段旧数据伪装成完整区间。

## 选择依据与边界

- 腾讯公开接口不需要用户券商账户，接入成本低，适合当前 Phase 3 的“可见行情”目标。
- 该接口不是交易柜台，也没有本项目可验证的 SLA、鉴权和成交保证；它只能用于行情展示、研究和纸面风控影子计算，不能作为真实下单确认依据。
- 请求频率保持在市场页可见时每 30 秒一次；不得把它当作高频行情或绕过券商官方行情授权。
- `freshness=unknown/stale` 时页面会显示警示；服务端的纸面成交与实盘草案仍拒绝未知/过期报价。

## 可追溯来源

- 腾讯云开发者社区的实时股票接口说明记录了 `qt.gtimg.cn` 的 `sh`/`sz` 代码前缀和批量查询格式：<https://developer.cloud.tencent.com/article/1534790>
- 当前项目原有 AKShare provider 的字段归一化、超时和 Eastmoney/Sina 回退仍保留在 `quant_engine/data/live.py`。
