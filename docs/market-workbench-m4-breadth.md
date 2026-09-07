# Phase 3A-M M4-B：全市场宽度补强记录

日期：2026-09-05

## 已交付

- `/api/v1/market/breadth` 不再调用“无代码列表”的全量源。默认公开源先读取本地/缓存证券主数据，再按每批 100 只证券请求 Tencent `qt`，并发上限为 6。
- 结果按证券代码去重；请求证券数、实际返回数、来源、证券主数据来源与 freshness 都写入 `meta`。返回不完整时为 `partial`，全量返回才为 `ok`；缺失字段保持 `null`。
- 有效涨跌或成交额覆盖不足时，`meta.status=partial`，页面明确显示 `N/M` 有效样本，不会把缺失数据当作 0。
- `meta.freshness` 采用保守聚合：存在未知时效优先显示未知，其次区分已过期、延迟和新鲜数据；不会把 `delayed` 误报为 `stale`。
- 宽度响应使用 30 秒单进程缓存，并以 provider 身份隔离缓存，避免刷新同时打满公共源。缓存返回深拷贝，调用方不能修改服务端缓存。
- `meta.cache_state=fresh` 表示缓存窗口内读取；已有快照遇到并发刷新时立即返回 `cache_state=stale`，无旧快照时才等待首个刷新完成，避免并发请求串行阻塞。
- 非默认 provider 继续保留无参数快照契约，便于离线合同测试和未来替换有全量能力的正式数据源。

## 运行态证据

- 在授权网络运行的本地 API 上，`GET /api/v1/market/breadth` 返回 `requested_count=5556`、`quoted_count=5556`、`status=ok`、`coverage=security_master_requested`、`source=tencent:qt`。
- 同一进程第二次读取命中 30 秒缓存，响应耗时约 0.324 秒；首次全市场请求耗时约 11.75 秒。耗时取决于公开源和网络，不构成 SLA。
- 当前 `can_submit_live=false`、Kill Switch 保持开启；宽度统计不会改变实盘能力开关。

## 明确边界

- Tencent/AKShare 是公开研究源，没有交易所级 SLA；非交易时段会按各证券源时间标记 stale。
- `limit_up`/`limit_down` 在源没有可靠涨跌停规则字段时继续返回 `null`，不能用涨跌幅阈值推断。
- 多源全部失败返回结构化 503；本地证券主数据不可用时不会伪造股票池。
