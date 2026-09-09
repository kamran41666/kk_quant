# kk_quant

个人量化研究工作台：行情观察、策略管理、研究数据检查、事件驱动回测、绩效比较与纸面交易。

Python / FastAPI / SQLite / Parquet · Vue 3 / TypeScript / ECharts

## 当前能力

- **研究数据中心 `/data`**：本地日线库存、按证券和日期的覆盖预检、缺失及异常定位、JSON 报告导出、已接入数据源与市场能力说明。
- **回测中心 `/backtest`**：A 股日频下一交易日开盘执行、国内基金 NAV 回测、策略参数、成本情景、异步状态、取消任务、失败原因与多实验绩效比较。
- **行情 `/market`**：A 股、国内基金、美股与黄金。行情可见不代表该市场已支持回测。
- **策略 `/strategies`**：策略协议、内置策略目录、实例与参数管理。
- **模拟 `/paper`**：纸面账户、持仓、订单与观察记录。真实券商执行尚未接入。

## 本地运行

从项目根目录执行（Python 3.11+，Node.js 与 npm）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,server]'
cd web
npm ci
```

终端一，在项目根目录启动后端。首次研究建议关闭自动纸面调度，避免检查页面时触发定时观察：

```sh
QUANT_SCHEDULER_ENABLED=false .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

终端二，在 `web/` 启动前端：

```sh
npm run dev -- --host 127.0.0.1
```

打开 [研究数据中心](http://127.0.0.1:5173/data) 或 [API 文档](http://127.0.0.1:8000/docs)。

## 推荐研究顺序

1. 检查本机库存，按目标股票池和区间生成覆盖报告。初始库存和策略实例允许为空。
2. 导入行情、历史成分与所需基本面。库存的最早/最晚日期不能证明中间没有缺口。
3. 登记策略、确认预热期和费用假设，运行回测。默认 A 股股票池需要起始时点的历史沪深300成分快照。
4. 对比参数、成本、样本内外结果，再决定是否进入纸面观察。

导入说明：[A 股数据](docs/a-share-data-acquisition.md)、[数据源与历史成分](docs/market-data-sources.md)、[基本面](docs/phase-4a-fundamentals.md)。

## 验证

```sh
.venv/bin/python -m pytest -q
cd web
npm test
npm run build
```

## 开发入口

[Progress](PROGRESS.md) 统一记录 2026-09-09 之后各次对话的项目修改；[中期状态审查](docs/project-state-audit-2026-09-09.md) 是建立该机制时的全链路事实快照；[文档地图](docs/README.md) 按任务指向专业资料。Agent 接手规范见 [AGENTS.md](AGENTS.md)，策略开发见 [Strategy Protocol v2](docs/strategy-protocol-v2.md)。

数据、数据库和结果目录属于本地资产，不随 Git 分发。代码具备某项能力不等于本机已导入所需数据。

## 已完成的长周期研究

500只历史主板股票，2015-01-05—2026-08-31；三个候选、两种成本情景、时间切分及窗口敏感性，36组正式实验和独立账务审计，12组精确复跑一致。[研究结果与来源](docs/research-runs/ashare-multi-strategy-research.md)、[缺陷修复记录](docs/research-runs/ashare-backtest-defect-log.md)。

回测中心可查看已登记的真实三线图（策略/上证/沪深300）、最大上涨与最大回撤，并导出PNG。当前所有候选仍是带未解决市场事件的研究结果，不获自动观察授权。

研究依赖与命令（从项目根目录）：

```sh
.venv/bin/python -m pip install -e '.[research]'
.venv/bin/python -m scripts.acquire_ashare_research --size 500 --workers 3
.venv/bin/python -m scripts.build_ashare_research_dataset --version normalized-new
.venv/bin/python -m scripts.run_ashare_research --manifest data/research/ashare-inception-2014-v1/normalized-new/manifest.json --output backtest_result/my-new-study --stage discovery --sensitivity --workers 2
.venv/bin/python -m scripts.audit_ashare_results --matrix backtest_result/my-new-study/matrix.json
```

新实验使用新版本和新输出目录。复现既有结果应保留原始响应、normalized-v2和runtime-v2代码副本；重新从网络下载可能得到修订历史，不能保证等于旧数据。时间留出已公开，不能复用它挑参数后再称独立样本外。
