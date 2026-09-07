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

## Ashare 探查决策

已对 [mpquant/Ashare](https://github.com/mpquant/Ashare) 的当前 `Ashare.py` 做源码与真实端点对比。它复用腾讯/新浪公开接口，不是独立数据源；原始请求没有显式超时，异常回退过宽，结果也不携带本项目要求的来源、接收时间和 freshness。因此当前不接入其源码，继续使用本项目已有的 HTTPS Tencent provider、AKShare 双源回退和结构化错误契约。完整对比记录见 `docs/ashare-evaluation.md`。

## 历史 AKShare 适配器边界

历史数据管道继续使用 `AKShareAdapter`，但已补齐限时调用、单飞保护、严格 A 股代码规范化、OHLCV 值域/日期校验和批次级 `source_meta`。当一批代码只有部分返回时，结果状态为 `partial`，同步日志也记录 `partial`，不会伪装成完整 `success`；全批失败则为 `failed`。

写入前会拒绝 `NaN/inf`、价格关系错误、负成交量、越界日期和重复日期；多个证券通过 `PriceStore.write_batch()` 先在临时文件中生成，再原子替换并在替换失败时回滚，避免形成半批本地数据。

`DataPipeline` 和 `PriceStore` 两层都强制要求 `code/date/open/high/low/close/volume` 完整存在；缺列批次写入 `failed` 日志并拒绝落盘。

AKShare 的指数成分接口只提供当前快照，无法证明指定历史日期的成分。适配器现在对 `fetch_index_components(index_code, dt)` fail-closed 抛出明确的历史成分不可用错误，避免回测把当前成分误用于过去日期。需要历史成分时，必须先建设按日期保存的成分快照，再开放该回测入口。

回测引擎在没有显式 `stock_list` 且没有点时成分快照时也会直接停止，并提示导入历史快照；不再使用固定大盘股列表兜底，以免产生生存者偏差。

快照工作流现在提供：`POST /api/v1/market/index-snapshots` 归档单期带生效日的数据、`POST /api/v1/market/index-snapshots/import` 批量导入多期历史数据（支持 `dry_run=true` 预校验）、`GET /api/v1/market/index-snapshots/{index_code}?as_of=...` 读取不晚于目标日的最近快照、`GET /api/v1/market/index-snapshots/{index_code}/coverage` 查看已归档期间，以及 `POST /api/v1/market/index-snapshots/current?index_code=...` 明确归档“今天的当前成分”。批量导入在全部期间通过代码、交易所、权重、覆盖数量、日期和重复期校验后才写入单一事务；HTTP、DataAPI 与底层 MetaDB 三层都会拒绝未来日期，避免绕过接口写入前视数据。当前抓取入口只标记当天，不能替代历史成分文件。

`GET /api/v1/market/calendar/coverage?start_date=...&end_date=...` 返回交易日历来源、内容哈希、覆盖边界和严格 `complete` 标记；`refresh=true`（默认）会在本地缓存未验证或范围不足时尝试从 AKShare 更新。回退到工作日近似时 `complete=false`，回测调用方必须 fail-closed。

离线文件也可使用 `scripts/import_index_snapshots.py` 导入 CSV、Parquet 或 JSON；脚本会先计算 SHA-256、执行同一套点时校验，并支持 `--dry-run`。CSV/Parquet 至少需要 `index_code,as_of,code` 列，JSON 还支持按 `snapshots[].rows` 分组的格式。

导入器会在解析前后复核文件 SHA-256，Parquet 的日期和接收时间统一规范化；直接写入的 `received_at` 必须是带时区的 ISO-8601 时间。旧版 SQLite 若存在不兼容的 `index_components` 表，会先保留为 `index_components_legacy*`，再创建当前结构，不会静默覆盖旧表。
