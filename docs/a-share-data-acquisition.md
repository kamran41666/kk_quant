# A 股五年行情数据集

## 当前任务定义

- 时间范围：`2021-09-05` 至 `2026-09-05`，按交易日返回；截至下载时上游最后可用交易日可能早于结束日期。
- 市场范围：东方财富公开列表接口返回的沪、深、北交所 A 股股票。当前股票清单和上市日期写入 `data/market_a_share_5y/universe.csv`。
- 复权口径：`none`（不复权）、`qfq`（前复权）、`hfq`（后复权），分别对应东方财富接口 `fqt=0/1/2`。
- 频率：日线；字段为日期、开高低收、成交量、成交额、振幅、涨跌幅、涨跌额、换手率。

## 运行方式

下载器保留每只股票、每个口径的原始 JSON，已有文件会跳过，因此可以中断后重跑：

```powershell
pwsh -NoProfile -File scripts/download_a_share_5y.ps1 -ThrottleLimit 8
```

下载完成后再压缩为三份 Parquet：

```powershell
& "C:\Users\17235\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts/compact_a_share_5y.py
```

产物为：

- `data/market_a_share_5y/universe.csv`
- `data/market_a_share_5y/manifest.json`
- `data/market_a_share_5y/raw/{none,qfq,hfq}/*.json`
- `data/market_a_share_5y/parquet/{none,qfq,hfq}/daily.parquet`

## 数据工程判断

行情主表之外，严肃回测还需要交易日历、证券主数据（上市/暂停/终止上市）、停牌状态、涨跌停价格、公司行为/复权因子、财务报表及公告日期、指数历史成分和行业分类。财务数据必须按公告日期建立 point-in-time 视图；不能把事后修订值直接当作历史可见值。

本次下载器先完成可复现的核心日线层。当前列表接口是“当前股票清单”，因此仍可能存在退市股票覆盖不足；退市股票需要从交易所历史退市清单或有授权的历史证券主数据补齐，不能用当前清单冒充无幸存者偏差的全历史股票池。公开免费接口也不应被宣称为交易所授权的完整历史数据产品。

## 来源与限制

主源是 AKShare 文档所说明的东方财富历史行情通道；AKShare 文档明确列出不复权、前复权、后复权三种参数，并说明成交量等字段单位。交易所官方页面同时说明行情历史数据存在产品和授权服务，项目如需对外分发或商用应另行核对授权范围。
