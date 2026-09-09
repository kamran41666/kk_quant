# 量化交易平台 — 阶段一回顾文档

> 文档状态：历史回顾。架构图、因子数量、测试数和阶段展望只反映 2026-07-07；不得用来判断当前实现。

> 日期: 2026-07-07
> 状态: 阶段一完成
> GitHub: https://github.com/kamran41666/kk_quant

---

## 1. 项目概述

面向 A 股市场的个人量化交易研究平台。以事件驱动的回测引擎为核心，提供从数据采集、因子研究、策略回测到绩效分析的完整研究链路。

- **市场**: A 股（沪深两市）
- **频率**: 低频/中频（日线级别，周频调仓）
- **约束**: T+1、涨跌停、停牌、印花税 + 佣金
- **技术路线**: Python 3.11+ 为主语言，C++ 预留加速接口

---

## 2. 提交记录

| # | 提交哈希 | 内容 | 文件数 | 行数 |
|---|---------|------|--------|------|
| 1 | `7aabd57` | 📄 阶段一设计文档 | 2 | +1145 |
| 2 | `cb5c76e` | 📋 实施计划 | 1 | +2080 |
| 3 | `8df984c` | 🏗️ 数据层基础设施 | 9 | +420 |
| 4 | `7d16c67` | 🔧 复权+成本+DataAPI | 12 | +732 |
| 5 | `c691fcc` | ⚙️ 回测引擎 (10模块) | 18 | +2363 |
| 6 | `68c56d4` | 📊 因子研究+绩效分析 | 18 | +2299 |

---

## 3. 技术架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     阶段一：研究平台                               │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐    │
│  │  数据层   │→ │ 回测引擎  │→ │ 因子研究  │→ │   绩效分析    │    │
│  │ 7 modules │  │10 modules│  │ 7 modules│  │  3 modules   │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘    │
│       │             │              │               │             │
│  交易日历        事件驱动循环    FactorMeta元类    Sharpe Ratio  │
│  事件驱动复权     Point-in-Time  12个内置因子     CAPM α/β      │
│  Parquet+SQLite  周频调仓        IC分层回测      Fama-French    │
│  DataAPI单例     T+1涨跌停       Fama-MacBeth    Max Drawdown   │
│  AKShare适配     撮合+滑点       行业/市值中性化  Sortino/Calmar │
│                  VWAP成交价                      信息比率        │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 数据层

| 模块 | 文件 | 核心功能 |
|------|------|---------|
| **交易日历** | `data/calendar.py` | A 股交易日历，AKShare 获取，本地 Parquet 缓存，支持 next/prev/nth 查询 |
| **复权处理器** | `data/adjust.py` | 事件驱动复权，存储原始价格+事件表，Point-in-Time 安全 |
| **存储层** | `data/store.py` | PriceStore（年/季分区 Parquet）+ AdjustStore + MetaDB（SQLite） |
| **数据采集器** | `data/fetcher/` | DataSource(ABC) + AKShareAdapter + DataPipeline（Fetch→Clean→Validate→Write） |
| **统一 API** | `data/api.py` | DataAPI 单例，隔离物理存储细节 |

### 3.2 回测引擎

| 模块 | 文件 | 核心功能 |
|------|------|---------|
| **数据类型** | `backtest/types.py` | Order/Trade/Position/AccountState dataclass + T+1 锁仓 |
| **成本模型** | `backtest/cost_model.py` | 佣金万2.5（最低5元）、印花税千1（仅卖出）、滑点千1 |
| **策略基类** | `backtest/strategy.py` | Strategy(ABC)：initialize/generate_signals/on_rebalance/teardown |
| **策略上下文** | `backtest/context.py` | StrategyContext + PortfolioLike Protocol |
| **调仓调度** | `backtest/scheduler.py` | RebalanceScheduler：daily/weekly/monthly |
| **数据供给** | `backtest/data_handler.py` | Point-in-Time 预加载 + push_day 逐日释放 |
| **订单管理** | `backtest/order_manager.py` | 订单生命周期 + A 股 100 股/手约束 |
| **撮合引擎** | `backtest/matcher.py` | VWAP 撮合 + 涨跌停/停牌约束（滑点已去重修复） |
| **组合估值** | `backtest/portfolio.py` | 持仓管理 + T+1 锁仓 + 买卖成本核算 |
| **结果记录** | `backtest/recorder.py` | Parquet 输出（持仓/组合/成交/委托/信号）+ summary.json |
| **主循环** | `backtest/engine.py` | 事件驱动主循环，串联所有模块 |

### 3.3 因子研究

| 模块 | 文件 | 核心功能 |
|------|------|---------|
| **基类+元类** | `factor/base.py` | FactorMeta 自动注册 + Factor(ABC) compute 抽象方法 |
| **注册表** | `factor/registry.py` | FactorRegistry 单例 |
| **内置因子** | `factor/builtin/` | 12 个因子：动量(3)、波动率(2)、估值/规模(2)、技术(3)、质量(2) |
| **因子评估** | `factor/evaluation.py` | IC 分析（Rank/pearson ICIR）+ 分层回测（Q1-Q5）+ Fama-MacBeth |
| **因子合成** | `factor/synthesis.py` | 等权/ICIR 加权合成 + 行业/市值中性化（截面回归取残差） |

**内置因子清单**：

| 类别 | 因子 | 计算引擎 |
|------|------|---------|
| 动量 | momentum_1m, momentum_3m, momentum_12m_1m | Python |
| 波动率 | volatility_1m, downside_vol_1m | Python |
| 规模 | log_market_cap | Python |
| 流动性 | turnover_1m | Python |
| 技术 | rsi_14, macd, bb_position | Python |
| 质量 | sharpe_1m, maxdrawdown_1m | Python |

### 3.4 绩效分析

| 模块 | 文件 | 核心功能 |
|------|------|---------|
| **收益指标** | `analytics/metrics.py` | 年化收益、超额收益、月度热力图 |
| **风险指标** | `analytics/metrics.py` | 波动率、下行波动率、MaxDD（含恢复天数）、VaR、CVaR |
| **风险调整** | `analytics/metrics.py` | Sharpe、Sortino、Calmar、Information Ratio、Omega Ratio |
| **α/β 归因** | `analytics/alpha_beta.py` | CAPM α+β、Fama-French 三因子 α、Jensen's Alpha、Treynor Ratio |
| **交易分析** | `analytics/metrics.py` | 胜率、盈亏比、年化换手率 |

---

## 4. 关键设计决策

### 4.1 Point-in-Time（时间点原则）

回测的最大陷阱是前视偏差（Look-ahead Bias）。系统在两个关键点上杜绝了这种偏差：

1. **事件驱动复权**：存储原始价格 + 独立的分红送转事件表。回测时仅应用 ex_date ≤ 当前交易日的事件，策略在每个时间点看到的就是"当时市场真正看到的价格"。

2. **DataHandler 预加载 + 逐日释放**：整个回测区间的数据在初始化时加载，但通过 `push_day()` 逐日推进，策略只能访问当前日及之前的数据。

### 4.2 API 隔离

上层代码不直接访问文件系统，通过 `DataAPI` 单例交互。这允许未来无缝切换底层存储（Parquet → 数据库 → 云端）而不影响业务逻辑。

### 4.3 不可变原始数据

`data/raw/` 目录下的数据写入后不可修改。数据修正通过追加事件实现（事件溯源模式），保证了回测结果的可复现性。

### 4.4 FactorMeta 自动注册

用户定义 Factor 子类时，元类自动将其注册到全局 FactorRegistry。策略通过 `strategy.use_factor("momentum_20")` 声明依赖，框架自动查找并计算。

### 4.5 模拟账户与实盘的统一接口

设计文档中明确了阶段二（模拟交易）和阶段三（实盘）共享相同的信号生成、订单管理、组合估值代码。唯一的差异在"订单执行"环节——模拟使用本地撮合，实盘使用券商 API。这保证了从模拟到实盘的无缝切换。

---

## 5. 开发流程总结

### 5.1 工作方式

- **多 Agent 并行开发**：每批次 3-4 个 Agent 同时开发独立模块
- **TDD 先行**：先写测试，再写实现，测试全部通过才能提交
- **Code Review 双检**：每批次都启动专门的 Review Agent 审查解耦性、代码优雅性、接口设计
- **分批次提交**：每个阶段完成后再统一 review → 批准 → 提交

### 5.2 代码质量

| 维度 | 状态 |
|------|------|
| 解耦性 | ✅ data → backtest → factor/analytics 单向依赖，无循环 |
| 测试覆盖 | ✅ ~200 个测试，覆盖数据层/回测引擎/因子/绩效 |
| 类型注解 | ✅ 所有公共方法有完整类型注解 |
| 错误处理 | ✅ 静默错误加 warning，关键路径有明确的异常 |
| Git 规范 | ✅ 6 次提交，每次 message 描述清晰 |

### 5.3 发现并修复的问题

| 问题 | 严重度 | 修复 |
|------|--------|------|
| AKShare 失败静默回退到工作日历（无节假日排除） | 🔴 | 加 warnings.warn 提示用户 |
| Order.fill() 超额成交静默截断 | 🔴 | 加 warnings.warn 警告 |
| PriceStore.read_range 空结果不返回 MultiIndex | 🔴 | 修复返回类型 |
| 滑点双重计费（matcher + portfolio） | 🔴 | 去重：滑点仅通过成交价体现 |
| StrategyContext 类型不匹配 | 🔴 | PortfolioLike Protocol |
| Fama-French 系数基于位置索引 | 🔴 | 改用列名映射 |
| MetaDB row→StockInfo 重复代码 | 🟡 | 提取 _row_to_stockinfo() |
| trading_days 命名混淆 | 🟡 | 重命名为 count_trading_days() |
| .gitignore 误伤 quant_engine/data/ | 🟡 | 改为 /data/ 只忽略根目录 |

---

## 6. 阶段二展望

### 需要构建的模块

```
阶段二：模拟交易平台
├── Web 前端 (Vue/React)
│   ├── 策略管理面板
│   ├── 实时仪表盘
│   ├── 回测结果可视化
│   └── 模拟账户状态
│
├── Web 后端 (FastAPI)
│   ├── REST API
│   ├── WebSocket 实时推送
│   ├── 任务调度（定时数据更新）
│   └── 用户认证
│
├── 模拟账户 (Paper Trading)
│   ├── SimulationAccount（阶段一已设计接口）
│   ├── 每日自动运行
│   ├── 模拟 vs 回测偏差追踪
│   └── 周度/月度报告
│
└── C++ 加速层 (pybind11)
    ├── 横截面计算 (rank/zscore/winsorize)
    ├── 滚动窗口批量计算
    └── 技术指标加速
```

### 技术栈（阶段二）

| 层级 | 技术 |
|------|------|
| 前端 | Vue 3 + TypeScript + ECharts/D3 |
| 后端 | FastAPI + SQLAlchemy + Redis |
| 实时 | WebSocket (FastAPI 内置) |
| 任务调度 | APScheduler / Celery |
| C++ 加速 | C++17 + pybind11 + CMake |
| 部署 | Docker + docker-compose |

---

## 7. 参考资料

- Fama, E. F., & French, K. R. (1993). Common risk factors in the returns on stocks and bonds. *JFE*.
- Fama, E. F., & MacBeth, J. D. (1973). Risk, return, and equilibrium: Empirical tests. *JPE*.
- Grinold, R. C., & Kahn, R. N. (2000). *Active Portfolio Management*. McGraw-Hill.
- **Qlib**: https://github.com/microsoft/qlib
- **Zipline**: https://github.com/quantopian/zipline
- **vnpy**: https://github.com/vnpy/vnpy

---

> **项目仓库**: https://github.com/kamran41666/kk_quant
> **当前版本**: v0.1.0 (阶段一完成)
