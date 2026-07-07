# 量化交易平台 - 阶段一设计文档

> 创建日期: 2026-07-07
> 状态: 已确认
> 版本: 1.1

### 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-07-07 | 初始版本 |
| 1.1 | 2026-07-07 | 调仓频率从月频改为**周频**；回测引擎增加 RebalanceScheduler；策略接口增加 on_rebalance 钩子和调仓日参数；因子评估适配周频节奏；新增 **4.6 模拟账户（Paper Trading）**章节；阶段二重新定义为"模拟交易"

---

## 目录

1. [项目概述](#1-项目概述)
2. [整体架构](#2-整体架构)
3. [数据层](#3-数据层)
4. [回测引擎](#4-回测引擎)
5. [因子研究模块](#5-因子研究模块)
6. [绩效分析模块](#6-绩效分析模块)
7. [C++ 加速层](#7-c-加速层)
8. [技术栈总览](#8-技术栈总览)
9. [开发路线图](#9-开发路线图)

---

## 1. 项目概述

### 1.1 项目定位

面向 A 股市场的个人量化交易研究平台。以事件驱动的回测引擎为核心，提供从数据采集、因子研究、策略回测到绩效分析的完整研究链路。

### 1.2 目标用户

个人量化投资者（现阶段为开发者本人），具备基础股票交易和金融知识。

### 1.3 三阶段规划

| 阶段 | 名称 | 核心产出 |
|------|------|---------|
| 阶段一 | 研究平台 | 数据采集、回测引擎、因子研究、绩效分析（CLI/Jupyter 环境） |
| 阶段二 | 模拟交易 | Web 前端、策略管理、**模拟账户（Paper Trading）**、可视化仪表盘 |
| 阶段三 | 实盘生产 | 实盘接口、风控系统、监控告警、Linux 服务器部署 |

> **模拟交易的定位**: 任何策略在实盘之前，必须先经过至少 1-3 个月的模拟账户验证。模拟账户使用真实行情但虚拟资金，完全模拟真实交易流程（信号生成→订单提交→撮合→持仓管理），仅最后的"下单"环节不走券商接口。这段时间用于验证策略在真实市场环境下的表现、发现回测中未暴露的问题（如数据延迟、信号不稳定、执行偏差等），积累足够的信心后再上线实盘。

本文档覆盖**阶段一**。

### 1.4 策略边界

- **市场**: A 股（沪深两市）
- **频率**: 低频/中频（日线级别，周频调仓）
- **品种**: 股票、ETF、可转债（后续扩展）
- **约束**: T+1、涨跌停、停牌、印花税 + 佣金

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                       阶段一：研究平台                            │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  数据采集 │  │  数据存储 │  │  回测引擎 │  │   绩效分析    │   │
│  │  Fetcher  │→│  Storage  │→│  Engine   │→│  Analytics    │   │
│  └──────────┘  └──────────┘  └─────┬────┘  └──────────────┘   │
│                                     │                           │
│                            ┌────────▼───────┐                   │
│                            │   因子研究       │                   │
│                            │   Factor Lab    │                   │
│                            └────────────────┘                   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              C++ 加速层 (pybind11)                        │  │
│  │   • 因子向量化计算 (截面rank, zscore, winsorize)          │  │
│  │   • 批量信号计算 (动量、波动率等窗口函数)                   │  │
│  │   • 组合优化求解器 (均值-方差 QP)                          │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 设计原则

1. **Point-in-Time（时间点）**: 任何数据查询只能返回"当时已知"的信息，杜绝前视偏差（Look-ahead Bias）
2. **不可变数据**: 原始数据（raw）一旦写入不再修改，修正通过事件追加实现，保证回测可复现
3. **API 隔离**: 上层业务代码不直接访问文件系统，通过统一 Data API 层交互，存储后端可替换
4. **可重建性**: 所有衍生数据（derived）均可从原始数据重新生成，不承载不可替代信息

---

## 3. 数据层

### 3.1 设计参考

本数据层方案参考了以下业界标杆的设计理念：

| 参考对象 | 借鉴的设计 |
|---------|-----------|
| **Qlib** (微软) | 列式存储 + 表达式引擎思想，按列分区减少 I/O |
| **Zipline** (Quantopian) | 事件驱动复权（原始价格 + Split/Dividend 事件表），TradingCalendar 驱动 |
| **Two Sigma** (公开分享) | 不可变数据层 + Point-in-Time 查询语义 |
| **米筐** | 数据管道分层 (raw → cleaned → adjusted) |

### 3.2 物理存储结构

```
data/
├── raw/                          # 原始数据层 —— 写入后不可修改
│   ├── calendar/                  # 交易日历
│   │   └── trading_dates.parquet  # 列: date, is_trading_day, exchange
│   │
│   ├── daily/                     # 日线行情 (Parquet, 按年份+季度分区)
│   │   └── year=2026/
│   │       └── quarter=3/
│   │           └── 000001.SZ.parquet  # 单只股票全历史日线
│   │           # 列: date, open, high, low, close, volume, amount,
│   │           #      pre_close, up_limit, down_limit, turnover_rate,
│   │           #      is_suspended
│   │
│   ├── minute/                    # 分钟线 (阶段二扩展)
│   │
│   ├── adjust/                    # 复权数据
│   │   ├── adjust_factor.parquet  # 列: code, date, factor (累积复权因子)
│   │   ├── splits.parquet         # 送转事件: code, ex_date, split_ratio
│   │   └── dividends.parquet      # 分红事件: code, ex_date, dividend_per_share
│   │
│   ├── fundamental/               # 基本面数据
│   │   ├── balance_sheet/         # 资产负债表 (按报告期分区)
│   │   ├── income_statement/      # 利润表
│   │   ├── cash_flow/             # 现金流量表
│   │   └── valuation/             # 估值指标 (PE/PB/PS/PCF)
│   │
│   ├── index/                     # 指数数据
│   │   ├── index_daily.parquet    # 指数日线 (沪深300/中证500/中证1000等)
│   │   └── index_components.parquet # 指数成分股 (含纳入/剔除日期)
│   │
│   ├── industry/                  # 行业分类
│   │   └── sw_industry.parquet    # 申万行业分类: code, date, level1, level2
│   │
│   └── reference/                 # 参考数据
│       └── stock_list.parquet     # 全市场股票列表: code, name, listed_date,
│                                   #   delisted_date, exchange, board
│
├── derived/                       # 衍生数据层 —— 可从 raw 重建
│   ├── factors/                   # 预计算因子缓存
│   │   └── factor_name/
│   │       └── daily.parquet      # 列: code, date, value
│   │
│   └── signals/                   # 信号缓存
│       └── signal_name/
│           └── daily.parquet
│
└── meta.db                        # SQLite 元数据库
    ├── data_versions              # 数据版本快照: version_id, created_at, description
    ├── data_update_log            # 更新日志: source, last_update, record_count, status
    ├── stock_info                 # 股票基础信息缓存 (冗余, 方便快速查询)
    └── calendar_cache             # 交易日历快速查询
```

### 3.3 复权方案

采用 **事件驱动复权**（借鉴 Zipline），避免前视偏差。

```
原理:
  - 存储原始价格（不复权）
  - 独立存储 split（送转股）和 dividend（现金分红）事件
  - 回测过程中，仅应用 ex_date ≤ 当前交易日的事件
  - 策略在每个时间点看到的，就是当时市场参与者"真正看到的价格"

对比传统方案:
  传统: 直接使用"后复权价格" → 2019年的价格已包含2022年的送转信息 → 前视偏差
  我们的方案: 2019年看到的就是2019年的价格 → Point-in-Time 正确

实现:
  class AdjustHandler:
      def get_adjusted_price(code, date, field='close'):
          """返回截至 date 的复权价格"""
          raw = read_raw_price(code, date, field)
          factors = read_adjust_factors_up_to(code, date)
          return raw * factors.cumprod()
```

### 3.4 数据采集

#### 数据源选型

| 数据类别 | 首选源 | 备选源 | 更新频率 |
|---------|--------|--------|---------|
| 日线行情 | AKShare (免费) | Tushare Pro (需积分) | 每日盘后 |
| 财务数据 | AKShare | Tushare / 东方财富爬取 | 财报发布后 |
| 指数成分 | 中证指数官网 | Tushare | 每半年 |
| 行业分类 | 申万研究所 | 东方财富 | 按需 |
| 复权因子 | 计算生成 | Tushare | 每日盘后 |

#### 采集器架构

```
┌──────────────────────┐
│   DataSourceManager  │  ← 统一管理所有数据源
├──────────────────────┤
│   AKShareAdapter     │  ← 每个数据源是一个 Adapter
│   TushareAdapter     │  ← 实现相同的 DataSource 接口
│   CrawlerAdapter     │  ← 爬虫适配器（备选方案）
├──────────────────────┤
│   DataPipeline       │  ← 数据管道
│   Fetcher → Cleaner  │
│   → Validator →      │
│   Writer             │
└──────────────────────┘
```

### 3.5 Data API

统一数据访问接口，隔离物理存储细节。

```python
# 高层 API —— 上层代码只使用这个
from quant_engine.data import DataAPI

api = DataAPI()

# 获取日线数据
df = api.daily(
    codes=["000001.SZ", "000002.SZ"],
    start="2020-01-01",
    end="2024-12-31",
    fields=["open", "high", "low", "close", "volume"],
    adjust="event_driven"  # 事件驱动复权
)

# 获取交易日历
calendar = api.get_trading_dates(start="2020-01-01", end="2024-12-31")

# 获取指数成分
hs300 = api.index_components("000300.SH", date="2024-01-02")

# 获取行业分类
industry = api.industry("000001.SZ", date="2024-06-30")

# 获取财务数据
fundamentals = api.fundamentals(
    codes=["000001.SZ"],
    report_dates=["2024Q3"],
    fields=["roe", "total_assets", "net_profit"]
)
```

---

## 4. 回测引擎

### 4.1 核心架构：事件驱动循环

策略以**周频调仓**为核心节奏。引擎每日迭代（用于组合估值和风控监控），但信号生成和调仓只在指定的调仓日触发。

```
initialize()
    │
    ▼
┌──────────────────────────────────────────────────┐
│                                                  │
│   for each trading_day in calendar:              │
│                                                  │
│     ┌──────────────────────────────┐             │
│     │ 1. DataHandler              │             │
│     │    push_day(trading_day)     │             │
│     │    向引擎供给"当天可见"的数据  │             │
│     └─────────────┬────────────────┘             │
│                   │                              │
│     ┌─────────────▼────────────────┐             │
│     │ 2. 调仓日判断                │             │
│     │    is_rebalance_day(date)    │             │
│     │    (默认每周五/月末最后一个交易日)│            │
│     └─────────────┬────────────────┘             │
│                   │                              │
│           ┌───────┴───────┐                      │
│           │ 是调仓日?      │                      │
│           ├───────┬───────┤                      │
│           │ YES   │ NO    │                      │
│           ▼       ▼                               │
│   SignalGenerator   跳过信号生成                   │
│   策略计算信号      直接跳到估值                    │
│   → 目标权重                                     │
│           │                                      │
│     ┌─────▼──────────────────┐                   │
│     │ 3. OrderManager        │                   │
│     │    signals → orders → fills                 │
│     │    信号→订单→撮合成交     │                   │
│     │    约束: T+1, 涨跌停, 停牌│                  │
│     └─────────────┬──────────┘                   │
│                   │                              │
│     ┌─────────────▼────────────────┐             │
│     │ 4. Portfolio (每日执行)      │             │
│     │    update(fills + 行情)      │             │
│     │    更新持仓、现金、权益        │             │
│     └─────────────┬────────────────┘             │
│                   │                              │
│     ┌─────────────▼────────────────┐             │
│     │ 5. Recorder (每日执行)       │             │
│     │    snapshot()                │             │
│     │    日终快照: 持仓/信号/权益    │             │
│     └──────────────────────────────┘             │
│                                                  │
│  end for                                         │
│     │                                            │
│     ▼                                            │
│  teardown()  → 输出 backtest_result/              │
└──────────────────────────────────────────────────┘
```

### 4.1.1 调仓日调度

```python
# 调仓日历配置
rebalance_schedule:
    frequency: "weekly"           # weekly | monthly | daily
    weekday: 5                    # 1=周一 ... 5=周五 (周频默认周五收盘)
    monthly_rule: "last"          # last=月末最后一个交易日 | 1~31=固定日期
    skip_holiday: true            # 假日顺延到下一个交易日
```

周频调仓的逻辑：以周五收盘后的数据计算信号，下周一开盘执行交易。这样策略信号基于完整的一周数据，执行有充足的时间缓冲。

### 4.2 撮合引擎

对于日线策略，采用 **VWAP 基准 + 滑点模型** 撮合：

```
撮合逻辑:
  1. 信号 → 目标持仓量 (基于当日收盘后信号)
  2. 目标 vs 实际 → 订单 (买入/卖出)
  3. 订单 → 撮合:
     - 成交价 = 次日 VWAP (近似: (open+high+low+2*close)/5)
     - 滑点 = 成交价 × slippage_rate (默认 0.1%, 可配置)
     - 最终成交 = 成交价 + 滑点方向修正

A股约束实现:
  - T+1: 今日买入仓位最早明日可卖
    → Position 对象维护 unlock_date 字段
  - 涨跌停: 涨停价挂买 = 无法成交
    → 检查 limit_up_price >= fill_price >= limit_down_price
  - 停牌: 停牌日该标的跳过
    → DataHandler 返回 is_suspended=True
  - 最小交易单位: 100 股 (1手)
    → OrderManager.round_lot(shares)
```

### 4.3 交易成本模型

```python
class CostModel:
    commission_rate: float = 0.00025      # 万2.5 佣金
    stamp_duty_rate: float = 0.001        # 千1 印花税 (仅卖出)
    min_commission: float = 5.0            # 最低佣金 5 元
    slippage_rate: float = 0.001          # 滑点 千1 (可配置)

    def calc_cost(self, trade):
        commission = max(trade.amount * self.commission_rate,
                         self.min_commission)
        stamp_duty = trade.amount * self.stamp_duty_rate \
                     if trade.side == SELL else 0
        slippage = trade.amount * self.slippage_rate
        return commission + stamp_duty + slippage
```

### 4.4 策略编写接口

```python
from quant_engine import Strategy, context

class MyAlphaStrategy(Strategy):
    """用户策略: 继承 Strategy, 重写钩子方法

    典型周频节奏:
      - 周五收盘: generate_signals() 计算信号
      - 下周一开盘: 执行交易 (信号→订单→撮合)
      - 周一至周五: 每日 update() 估值 (不产信号)
    """

    def initialize(self):
        """初始化 —— 设置参数、注册因子"""
        self.lookback = 60                    # 因子回溯窗口 (自然日)
        self.top_n = 50                        # 持仓股票数
        self.rebalance_freq = "weekly"         # 调仓频率: weekly
        self.rebalance_day = 5                 # 周五收盘后计算信号

        # 注册需要使用的因子
        self.use_factor("momentum_20")
        self.use_factor("roe_ttm")
        self.use_factor("market_cap")

    def before_trading(self):
        """盘前 —— 预处理、加载因子数据 (每个交易日调用)"""
        pass

    def generate_signals(self, date):
        """信号生成 —— 仅在调仓日调用

        Args:
            date: 当前交易日 (通常是周五)

        Returns:
            signals: 目标权重字典 {code: weight} 或 Signal 对象列表
        """
        # 加载最新因子值 (Point-in-Time: 只看 date 之前的数据)
        momentum = self.get_factor("momentum_20", date)
        roe = self.get_factor("roe_ttm", date)
        market_cap = self.get_factor("market_cap", date)

        # 多因子合成评分
        score = (momentum.zscore() * 0.4 +
                 roe.zscore() * 0.4 +
                 market_cap.zscore() * (-0.2))  # 负号=偏好小市值

        # 行业中性化 (剔除行业 β)
        score = score.industry_neutralize(date)

        # 市值中性化 (剔除市值 β)
        score = score.market_cap_neutralize(date)

        # 选前 N 只等权配置
        signals = score.top_n(self.top_n, weight='equal')
        return signals

    def on_rebalance(self, date, old_weights, new_weights):
        """调仓回调 —— 输出旧持仓→新持仓的变化"""
        self.log(f"调仓日: {date}")
        self.log(f"  换出: {len(old_weights) - len(set(old_weights) & set(new_weights))} 只")
        self.log(f"  换入: {len(new_weights) - len(set(old_weights) & set(new_weights))} 只")
        self.log(f"  换手率: {self._calc_turnover(old_weights, new_weights):.1%}")

    def on_order_filled(self, trade):
        """成交回调 —— 记录/监控"""
        self.log(f"  {trade.date} {trade.code} {trade.side} "
                 f"{trade.shares}@{trade.price:.2f}")

    def teardown(self):
        """结束回调 —— 最终统计"""
        self.log(f"最终收益: {self.portfolio.total_return:.2%}")
        self.log(f"年化收益: {self.portfolio.annual_return:.2%}")
        self.log(f"最大回撤: {self.portfolio.max_drawdown:.2%}")
```

### 4.5 回测结果输出格式

```
backtest_result/
├── daily_positions.parquet    # 每日持仓明细
│   # code, date, shares, market_value, weight, pnl_daily
│
├── daily_portfolio.parquet    # 每日组合总览
│   # date, total_value, cash, equity, market_value,
│   # daily_return, cumulative_return, benchmark_return,
│   # turnover, n_positions
│
├── trades.parquet             # 每笔成交记录
│   # trade_id, code, date, side, shares, price,
│   # amount, commission, stamp_duty, slippage
│
├── orders.parquet             # 每笔委托记录
│   # order_id, code, date, side, shares, price_limit,
│   # status (filled/partial/cancelled), fill_shares, reject_reason
│
├── signals.parquet            # 每日信号快照
│   # date, code, raw_score, final_score, target_weight
│
└── summary.json               # 回测元信息
    # strategy_name, start_date, end_date, benchmark,
    # initial_capital, parameters, version
```

### 4.6 模拟账户（Paper Trading）

模拟账户是连接回测研究和实盘交易之间的**必经桥梁**。它与回测引擎共享相同的信号生成和订单管理逻辑，但运行在实时行情上、使用虚拟资金。

#### 4.6.1 为什么必须做模拟交易

```
回测的问题                    模拟交易如何暴露

数据完美性偏差              实时数据存在延迟、缺失、修正
  - 回测使用的是历史修正后的    - 模拟交易使用实时推送的数据
    "完美数据"                 - 暴露数据质量对你的策略的实际影响

信号稳定性                   连续运行验证信号一致性
  - 回测的一次性计算没问题      - 每天/每周重新计算信号
  - 每次重新计算可能产生        - 验证信号是否在实时环境中
    细微差异                     保持稳定

执行偏差                    真实流程验证
  - 回测中忽略了很多现实延迟    - 模拟完整的：数据采集→信号计算
  - 数据采集时间、计算耗时        →订单生成→模拟执行的完整链路
    T+1挂单时间等
                              - 记录每个环节的耗时

心理因素                    积累信心
  - 回测看到的是"事后"曲线      - 经历真实市场波动中的浮亏
  - 真实持仓的浮亏会让人        - 在压力下坚持策略纪律
    动摇策略信念
```

#### 4.6.2 模拟账户每日流程

```
每个交易日:
┌─────────────────────────────────────────────────────┐
│  盘前 (08:00-09:15)                                  │
│  ├── 数据采集器拉取昨日收盘数据                         │
│  ├── 更新本地数据仓库                                  │
│  ├── 重新计算所有因子                                  │
│  └── 如果是调仓日: 生成信号 → 订单列表                  │
│                                                      │
│  盘中 (09:30-15:00)                                  │
│  ├── 如果是调仓日:                                    │
│  │   ├── 09:25 获取集合竞价价格                        │
│  │   ├── 09:30 按开盘价模拟执行订单                     │
│  │   └── 记录成交结果                                  │
│  ├── 持续拉取分钟线 (可选)                             │
│  └── 更新组合实时估值                                  │
│                                                      │
│  盘后 (15:00-17:00)                                  │
│  ├── 计算当日组合收益率                                │
│  ├── 更新绩效指标 (Sharpe, MaxDD, ...)                │
│  ├── 生成日度模拟报告                                  │
│  └── 写入模拟账户持仓快照                              │
└─────────────────────────────────────────────────────┘

每周五:
├── 计算信号 (基于当周最新数据)
├── 生成下周一开盘执行的订单
└── 记录信号快照 (用于后续复盘: 信号是否被正确执行)
```

#### 4.6.3 模拟账户数据结构

```python
class SimulationAccount:
    """模拟交易账户"""

    # 账户状态
    account_id: str
    initial_capital: float = 1_000_000  # 初始资金 100万
    cash: float                         # 当前现金
    positions: dict[str, Position]      # 当前持仓
    total_value: float                  # 总权益 (现金 + 持仓市值)

    # 模拟专属字段
    start_date: date                    # 模拟开始日期
    days_running: int                   # 已运行天数
    rebalance_count: int                # 已完成调仓次数

    # 信号执行记录
    signals_log: list[SignalSnapshot]   # 历史信号快照
    executions: list[ExecutionRecord]   # 模拟执行记录
    skipped_orders: list[SkippedOrder]  # 因涨跌停/停牌未成交的订单

class SimulationReport:
    """每周/每月模拟报告"""
    # 与回测相同的绩效指标
    # 额外对比: 模拟 vs 回测的偏差分析
```

#### 4.6.4 模拟 vs 回测的偏差追踪

模拟交易自动追踪与回测预期之间的偏差：

```python
class SimulationDriftTracker:
    """追踪模拟交易与回测预期的偏差"""

    def track(self):
        return {
            # 信号偏差: 同一策略, 实时算出的信号 vs 回测时算出的信号
            "signal_drift": compare_signals(live_signal, backtest_signal),
            # 成交偏差: 模拟成交价 vs 回测假设成交价
            "execution_slippage": actual_price - expected_price,
            # 持仓偏差: 模拟持仓 vs 回测持仓 (因为部分成交等原因)
            "position_drift": live_positions - backtest_positions,
            # 收益偏差: 模拟收益 vs 回测收益 (最关键的偏差指标)
            "return_drift": live_return - backtest_return,
        }
```

#### 4.6.5 模拟账户与阶段二的关系

```
阶段一 (当前):
  回测引擎 → 策略在历史数据上验证

阶段二 (下一步):
  模拟账户 = 回测引擎 + 实时数据源 + 完整执行链路
  → 策略在实时行情上运行, 虚拟资金, 无需券商接口
  → 这是阶段二的核心产出

阶段三:
  实盘账户 = 模拟账户 + 券商接口 + 风控 + 监控
  → 阶段二的模拟账户设计不必修改, 只需替换"模拟撮合"
    为"券商下单"
```

> **设计原则**: 模拟账户和实盘账户共享完全相同的信号生成、订单管理、组合估值代码。唯一的差异在"订单执行"环节——模拟账户使用本地撮合引擎，实盘账户使用券商 API 下单。这保证了从模拟到实盘的无缝切换。

---

## 5. 因子研究模块

### 5.1 因子体系

系统内置以下因子类别，用户可以注册自定义因子：

| 类别 | 示例因子 | 计算引擎 | 说明 |
|------|---------|---------|------|
| **估值** | pe_ttm, pb, ps_ttm, fcf_yield | Python | 财报发布日对齐，避免前视偏差 |
| **动量** | ret_1m, ret_3m, ret_6m, ret_12m_1m | C++加速 | 跳过最近1月，避免短期反转效应 |
| **波动** | volatility_1m, downside_vol_1m | C++加速 | 下行波动率（只计入负收益的波动） |
| **质量** | roe_ttm, gross_margin, net_margin, debt_ratio | Python | 用最新可得财报，正确对齐报告期 |
| **规模** | log_market_cap, float_market_cap | Python | 对数市值 = ln(总市值)，降低偏度 |
| **流动性** | turnover_1m, amihud_illiq | C++加速 | Amihud非流动性指标 |
| **技术** | rsi_14, macd, bb_position, ma_gap | C++加速 | 多个窗口并行计算 |
| **情绪** | north_flow_ratio, margin_ratio | Python | 北向资金、融资余额占比 |

### 5.2 因子定义接口

```python
from quant_engine.factor import Factor, FactorMeta

class Momentum20(Factor, metaclass=FactorMeta):
    """20日动量因子"""
    name = "momentum_20"
    category = "momentum"
    inputs = ["close"]           # 需要的原始字段
    window = 20                   # 计算窗口
    engine = "cpp"               # 使用 C++ 加速

    def compute(self, data: pd.DataFrame) -> pd.Series:
        """Python 备选实现（当 C++ 引擎不可用时）"""
        return data["close"].pct_change(self.window)
```

### 5.3 因子评估 —— Alpha 分析体系

#### 5.3.1 IC 分析

```
IC (Information Coefficient):
  - Pearson IC: 因子值与下期收益的线性相关
  - Rank IC: 因子排名与下期收益排名的 Spearman 相关 (更稳健)
  - 每个交易日计算一次交叉截面 IC → 得到 IC 时间序列

IC 统计:
  - IC Mean: IC 均值 (> 0.03 有效, > 0.05 优秀)
  - IC Std: IC 标准差
  - ICIR (Information Coefficient IR):
    ICIR = IC_mean / IC_std  (> 0.5 优秀)
    衡量因子预测能力的稳定性
  - IC > 0 比例: IC 为正的天数占比 (> 55% 有效)
  - IC 衰减: 滞后 1/2/3/5/10/20 期的 IC，衡量因子预测力的持续期
```

#### 5.3.2 分层回测（Quantile Analysis）

```
方法:
  1. 每个交易日按因子值从小到大将股票分为 5 组 (或 10 组)
  2. 每组等权构建组合，持有一个调仓周期（周频=5个交易日）
  3. 绘制每组累计收益曲线

判断标准:
  - 单调性: Q1 → Q5 收益应单调递增/递减
  - Top-Bottom 对冲收益: 多 Q5 空 Q1 的收益曲线 (纯因子 α)
  - 对冲组合的 Sharpe: > 1.0 优秀
  - 对冲组合的最大回撤: 回撤越小因子越稳定

调仓频率适配:
  - 周频调仓时，IC 和分层回测的下期收益统一取 5 个交易日后
  - IC 衰减也按周步进: 1周/2周/3周/4周/8周/12周
```

#### 5.3.3 因子分布分析

```
每期横截面统计:
  - mean, std, skewness, kurtosis
  - 分位数: 1%, 5%, 25%, 50%, 75%, 95%, 99%
  - 覆盖率 (coverage): 有因子值的股票占比

时间序列统计:
  - 因子自相关 (1日/5日/20日滞后)
  - 行业集中度 (Herfindahl 指数)
  - 市值相关性 (因子值与对数值的截面相关性)
```

#### 5.3.4 因子相关性矩阵

```
- 对所有因子做两两 Spearman 截面相关
- 取时间序列均值 → 因子相关性矩阵
- 聚类热力图 → 识别因子家族
- 同族因子取 ICIR 最优者, 避免共线性
```

#### 5.3.5 Fama-MacBeth 回归

```
方法:
  Stage 1 (时间序列): 对每只股票, 用因子值回归其收益,
                       得到因子暴露 β
  Stage 2 (横截面): 对每个交易日, 用收益回归 β,
                       得到因子风险溢价 λ

输出:
  - 每个因子的 λ (风险溢价) 和 t 统计量
  - 显著不为零的因子 → 有独立预测力
  - 这就是学术意义上的 α: 不能被其他因子解释的超额收益
```

### 5.4 因子合成

```
单因子 → 合成 Alpha 信号:

方法1: 等权合成
  alpha = mean(zscore(f1), zscore(f2), ...)

方法2: ICIR 加权合成 (推荐)
  alpha = Σ( ICIR_i × zscore(f_i) ) / Σ(ICIR_i)

方法3: 机器学习合成 (阶段二引入)
  alpha = XGBoost(factors, forward_returns)

后处理 (必须):
  1. 行业中性化: alpha_res = alpha ~ industry_dummies → 取残差
  2. 市值中性化: alpha_final = alpha_res ~ log_mcap → 取残差
  → 得到纯 α, 剥离了行业 β 和市值 β
```

---

## 6. 绩效分析模块

### 6.1 收益指标

| 指标 | 计算 | 说明 |
|------|------|------|
| 累计收益率 | `(final_value / initial_capital) - 1` | 总收益 |
| 年化收益率 | `(1 + total_return)^(252/n_days) - 1` | 年化 |
| 超额收益 | `strategy_return - benchmark_return` | vs 基准 |
| 月度收益热力图 | 每月收益矩阵 | 季节性分析 |

### 6.2 风险指标

| 指标 | 计算 | 说明 |
|------|------|------|
| 年化波动率 | `daily_returns.std() * sqrt(252)` | 总风险 |
| 下行波动率 | `daily_returns[daily_returns < 0].std() * sqrt(252)` | 下行风险 |
| 最大回撤 (MaxDD) | `max(peak - trough) / peak` | 含起止日期和恢复天数 |
| VaR (95%) | 日收益分布的 5% 分位数 | 日度在险价值 |
| CVaR (95%) | VaR 之外的平均损失 | 条件在险价值 |

### 6.3 风险调整收益（α/β 核心）

| 指标 | 计算 | 说明 |
|------|------|------|
| **Sharpe Ratio** | `(annual_return - rf) / annual_vol` | 每单位总风险获得多少超额收益，> 1.5 优秀 |
| **Sortino Ratio** | `(annual_return - rf) / downside_vol` | 用下行波动率替代总波动率 |
| **Calmar Ratio** | `annual_return / max_drawdown` | 收益与最大回撤的比值，> 1 可接受 |
| **Information Ratio** | `excess_return.mean() / tracking_error * sqrt(252)` | 每单位主动风险获得的超额收益 |
| **Jensen's Alpha** | `α = R_p - [R_f + β_p(R_m - R_f)]` | CAPM 框架下的超额收益 |
| **Beta (β)** | `Cov(R_p, R_m) / Var(R_m)` | 策略对市场的系统性风险暴露 |
| **Fama-French Alpha** | 对三因子/五因子模型回归的截距项 | 经多因子调整后的纯 α |
| **Treynor Ratio** | `(annual_return - rf) / beta` | 每单位系统性风险的超额收益 |
| **Omega Ratio** | `Σmax(0, R-R_threshold) / Σmax(0, R_threshold-R)` | 收益-损失概率加权比 |

### 6.4 交易分析

| 指标 | 说明 |
|------|------|
| 年化换手率 | 双边，衡量交易频率 |
| 胜率 | 盈利交易笔数 / 总交易笔数 |
| 盈亏比 | 平均盈利 / 平均亏损 |
| 平均持仓天数 | 从买入到卖出的平均跨度 |
| 容量估算 | 基于日均成交量和持仓占比，估算策略最大资金容量 |

### 6.5 归因分析

```
Brinson 归因 (行业层面):
  - 配置效应: 超配/低配某行业带来的收益
  - 选股效应: 在某行业内选股带来的收益
  - 交互效应: 配置和选股的交叉影响

Barra 风格归因 (因子层面):
  - 对 Barra 风险因子 (Size, Value, Momentum, Volatility, Quality, ...)
    做收益回归
  - 分解收益为: 因子收益 + 特质收益 (Alpha)
```

### 6.6 Tear Sheet 输出

```
PerformanceReport
├── summary_stats          # 核心指标汇总表
├── equity_curve           # 权益曲线图 (含基准对比)
├── drawdown_chart         # 回撤曲线图
├── monthly_heatmap        # 月度收益热力图
├── annual_returns         # 年度收益柱状图
├── rolling_stats          # 滚动 Sharpe/回撤 (12个月窗口)
├── factor_attribution     # 因子归因图
├── industry_attribution   # Brinson 归因表
└── trade_distribution     # 交易分布 (按行业/市值/因子暴露)
```

---

## 7. C++ 加速层

### 7.1 加速边界

```
Python 层                        C++ 层
(灵活性高, 开发快)                (性能关键路径)

策略逻辑编写                     因子批量计算
  • 用户自定义策略                 • 横截面标准化 (zscore / rank)
  • 信号生成逻辑                   • winsorize (缩尾处理)
  • 自定义因子 Python 实现          • 滚动窗口统计 (mean, std, regression)
                                   • 技术指标计算 (RSI, MACD, BB, ATR)
数据 I/O
  • Parquet 读写                 组合优化
  • 数据预处理                     • 均值-方差 QP 求解器
  • 缺失值处理                     • 风险平价求解
                                   • 带约束的组合优化
撮合/订单管理
  • 订单状态机                   回测核心循环 (可选)
  • T+1 / 涨跌停逻辑               • 当股票池 >5000 时
  • 成本计算                       • 因子计算密集时的主循环
```

### 7.2 pybind11 绑定接口

```cpp
// cpp_engine/factor_compute.h
#pragma once
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

namespace py = pybind11;

namespace quant::factor {

// 横截面排名 (0-1 标准化), 支持行业分组
py::array_t<double> cross_sectional_rank(
    py::array_t<double> values,
    py::array_t<int> groups
);

// 横截面 z-score 标准化
py::array_t<double> cross_sectional_zscore(
    py::array_t<double> values,
    py::array_t<int> groups,
    double winsorize_pct = 0.01       // 默认 1% 缩尾
);

// 滚动窗口计算 (批量)
py::array_t<double> rolling_mean(
    py::array_t<double> values,
    py::array_t<int64_t> dates,
    int window
);

// 组合优化: 均值-方差有效前沿
struct PortfolioOptResult {
    py::array_t<double> weights;
    double expected_return;
    double volatility;
    bool feasible;
};

PortfolioOptResult mean_variance_optimize(
    py::array_t<double> returns,       // N × T 收益矩阵
    py::array_t<double> constraints,   // 权重约束
    double target_return               // 目标收益 (0 = 最小方差)
);

}  // namespace quant::factor
```

### 7.3 Python 侧调用

```python
# quant_engine/factor/compute.py
from quant_engine.cpp import factor_compute as _cpp

def cs_rank(values: pd.Series, groups: pd.Series = None) -> pd.Series:
    """截面排名, 调用 C++ 实现"""
    g = groups.cat.codes.values.astype(np.int32) if groups is not None \
        else np.zeros(len(values), dtype=np.int32)
    result = _cpp.cross_sectional_rank(values.values, g)
    return pd.Series(result, index=values.index)

def cs_zscore(values, groups=None, winsorize=0.01):
    """截面标准化, 调用 C++ 实现"""
    g = groups.cat.codes.values.astype(np.int32) if groups is not None \
        else np.zeros(len(values), dtype=np.int32)
    result = _cpp.cross_sectional_zscore(
        values.values, g, winsorize
    )
    return pd.Series(result, index=values.index)
```

---

## 8. 技术栈总览

### 开发环境

| 项目 | 选择 |
|------|------|
| 操作系统 (开发) | Windows 11 |
| 操作系统 (部署) | Ubuntu 22.04 LTS |
| 包管理 | conda (Python 科学计算生态) + pip |
| C++ 构建 | CMake + vcpkg (包管理) |

### 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| **Python** | 3.11+ | 主语言 |
| **C++** | 17/20 | 性能热路径 |
| **绑定** | pybind11 | Python ↔ C++ |
| **数据处理** | pandas, numpy, polars | 数据清洗/变换 |
| **数值计算** | scipy, cvxpy | 优化求解 |
| **数据存储** | pyarrow (Parquet) | 行情/因子存储 |
| **元数据** | sqlite3 (内置) | 配置/元数据 |
| **可视化** | plotly, matplotlib | 绩效图表 |
| **数据源** | akshare, tushare | 行情/财务 |
| **测试** | pytest | 单元/集成测试 |
| **代码质量** | ruff, mypy | 代码检查 |
| **版本控制** | git | 代码版本管理 |

### 项目结构

```
quant/
├── quant_engine/             # 核心引擎包
│   ├── __init__.py
│   ├── data/                 # 数据层
│   │   ├── __init__.py
│   │   ├── api.py            # DataAPI 统一接口
│   │   ├── fetcher/          # 数据采集器
│   │   ├── store/            # 存储层 (Parquet + SQLite)
│   │   ├── calendar.py       # 交易日历
│   │   └── adjust.py         # 复权处理
│   │
│   ├── backtest/             # 回测引擎
│   │   ├── __init__.py
│   │   ├── engine.py         # 事件驱动主循环
│   │   ├── data_handler.py   # 数据供给 (Point-in-Time)
│   │   ├── strategy.py       # Strategy 基类
│   │   ├── order_manager.py  # 订单管理 + 撮合
│   │   ├── portfolio.py      # 组合估值
│   │   ├── recorder.py       # 结果记录
│   │   └── cost_model.py     # 交易成本模型
│   │
│   ├── factor/               # 因子模块
│   │   ├── __init__.py
│   │   ├── base.py           # Factor 基类
│   │   ├── registry.py       # 因子注册表
│   │   ├── compute.py        # 因子计算 (调用 C++ 或 Python)
│   │   ├── evaluation.py     # 因子评估 (IC/分层/归因)
│   │   └── synthesis.py      # 因子合成 + 中性化
│   │
│   └── analytics/            # 绩效分析
│       ├── __init__.py
│       ├── metrics.py        # 指标计算 (Sharpe, MaxDD, Alpha, Beta...)
│       ├── attribution.py    # 归因分析 (Brinson / Barra)
│       └── report.py         # Tear Sheet 生成
│
├── cpp_engine/               # C++ 加速层
│   ├── CMakeLists.txt
│   ├── include/
│   │   └── quant/
│   │       ├── factor_compute.h
│   │       ├── rolling.h
│   │       └── optimize.h
│   ├── src/
│   │   ├── factor_compute.cpp
│   │   ├── rolling.cpp
│   │   └── optimize.cpp
│   └── bindings/
│       └── pybind_module.cpp
│
├── tests/                    # 测试
│   ├── test_data_api.py
│   ├── test_backtest_engine.py
│   ├── test_factors.py
│   └── test_analytics.py
│
├── examples/                 # 示例
│   ├── 01_fetch_data.py
│   ├── 02_simple_backtest.py
│   └── 03_factor_research.py
│
├── data/                     # 本地数据目录 (.gitignore)
├── docs/                     # 文档
│   └── superpowers/
│       └── specs/
│           └── 2026-07-07-phase-1-design.md
│
├── pyproject.toml
├── CMakeLists.txt            # 顶层 CMake
└── README.md
```

---

## 9. 开发路线图

### 阶段一开发顺序

```
Week 1-2: 数据层
  ├── 交易日历模块
  ├── 数据采集器 (AKShare adapter)
  ├── Parquet 存储层 (读写 + 分区)
  ├── 复权处理器 (事件驱动)
  ├── SQLite 元数据库
  └── DataAPI 统一接口

Week 3-4: 回测引擎
  ├── Strategy 基类 + 上下文
  ├── DataHandler (Point-in-Time 数据供给)
  ├── OrderManager + 撮合引擎
  ├── Portfolio 组合估值
  ├── CostModel 成本模型
  ├── Recorder 结果记录
  └── 完整事件驱动主循环

Week 5-6: C++ 加速层
  ├── 搭建 pybind11 + CMake + vcpkg 构建
  ├── 横截面计算 (rank / zscore / winsorize)
  ├── 滚动窗口计算 (mean / std / regression)
  ├── 技术指标 (RSI, MACD, BB)
  └── Python 侧封装 + 切换逻辑

Week 7-8: 因子研究
  ├── Factor 基类 + 注册表
  ├── 内置因子库 (估值/动量/波动/质量...)
  ├── IC 分析 + 分层回测
  ├── 因子相关性矩阵
  ├── Fama-MacBeth 回归
  └── 因子合成 + 中性化

Week 9-10: 绩效分析 + 集成
  ├── 收益/风险指标计算
  ├── α/β 归因分析
  ├── Brinson 行业归因
  ├── Tear Sheet 生成 (plotly)
  ├── 示例策略 + 集成测试
  └── 文档 + README
```

### 里程碑

| 里程碑 | 验收标准 |
|--------|---------|
| M1: 数据就绪 | 能拉取全A股5年日线数据，通过 DataAPI 查询 |
| M2: 回测跑通 | 能运行一个简单的均线交叉策略并输出交易记录 |
| M3: 加速集成 | C++ 因子计算性能 5x+ 于纯 Python |
| M4: 因子完备 | IC 分析 + 分层回测 + 因子合成全部可用 |
| M5: 报告输出 | 完整 Tear Sheet 包含所有专业指标 |

---

## 附录 A: 参考资料

### 学术参考
- Fama, E. F., & French, K. R. (1993). Common risk factors in the returns on stocks and bonds. *Journal of Financial Economics*, 33(1), 3-56.
- Fama, E. F., & MacBeth, J. D. (1973). Risk, return, and equilibrium: Empirical tests. *Journal of Political Economy*, 81(3), 607-636.
- Grinold, R. C., & Kahn, R. N. (2000). *Active Portfolio Management*. McGraw-Hill.

### 开源项目参考
- **Qlib**: https://github.com/microsoft/qlib — 微软开源的 AI 量化平台
- **Zipline**: https://github.com/quantopian/zipline — Quantopian 的事件驱动回测引擎
- **vnpy**: https://github.com/vnpy/vnpy — Python 量化交易框架
- **backtrader**: https://github.com/mementum/backtrader — 事件驱动回测框架

### 数据源
- **AKShare**: https://github.com/akfamily/akshare — 免费开源 A 股数据接口
- **Tushare**: https://tushare.pro — A 股数据平台
