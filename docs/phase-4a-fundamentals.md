# Phase 4A：Point-in-time 基本面数据层

日期：2026-09-05

## 目标

为价值、质量和股息策略提供可审计的财务观察值，同时阻止“用后来发布的财报回看过去”的前视偏差。该阶段只接收用户已取得授权的数据文件或后续符合契约的数据适配器，不把网页抓取结果伪装成可靠财务源。

## 已交付

- SQLite `fundamentals` 表保存 `code/report_date/announce_date/field/value/source/received_at`。
- 存储层校验 A 股代码、报告期、公告日期、字段名、有限数值和重复键；公告日期早于报告期或未来日期会被拒绝。
- `announce_date <= as_of` 的 PIT 选择规则：每个证券/报告期/字段只取查询日可见的最新公告版本。
- `DataAPI.fundamentals(..., as_of=...)`、导入校验/批量原子写入和覆盖率查询。
- `GET /api/v1/market/fundamentals/{code}`：返回 tidy rows、来源、接收时间和 PIT 规则；没有导入数据时返回 `status=empty`。
- `GET /api/v1/market/fundamentals/{code}/coverage`：查看可用报告期和公告版本数。
- `scripts/import_fundamentals.py`：支持 CSV/Parquet/JSON、`--dry-run`、SHA-256 文件一致性校验和 `data_versions` 审计记录。

## 使用示例

```powershell
python scripts/import_fundamentals.py data/fundamentals.csv --dry-run
python scripts/import_fundamentals.py data/fundamentals.csv --source licensed:provider
```

输入至少包含：`code,report_date,announce_date,field,value`。例如 `roe`、`pb`、`pe_ttm`、`dividend_yield` 等字段由数据提供方定义；平台不会猜测单位或把缺失值改成零。

## 明确边界

- 当前没有默认联网财务 provider；公开网页财务数据的公告日、修订版本和授权范围未被验证。
- 只有导入并带公告日期的数据才能进入 PIT 查询；没有数据时价值策略应显示不可用，不应自动使用当前值回测历史。
- 该层不改变 `can_submit_live=false`，也不提供真实账户操作。

## 验收

- PIT 版本选择、未来日期、公告日期顺序、非有限值、重复行和批量事务回滚均有测试。
- HTTP 返回结构化 `empty/ok` 状态和来源元数据。
