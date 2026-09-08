# CLAUDE.md

## 项目定位

A股个人量化交易平台，面向低频/中频策略（日线，周频调仓），兼具个人实用工具和简历项目作品双重定位。

## 技术栈

- **主语言**: Python 3.11+
- **C++**: pybind11 加速（技术债，暂不实现）
- **后端**: FastAPI + SQLAlchemy 2.0 + APScheduler + WebSocket
- **前端**: Vue 3 + TypeScript + Vite + ECharts + Pinia
- **存储**: Parquet（时序行情）+ SQLite（元数据/业务数据）
- **部署**: Docker（Nginx + FastAPI 双容器）

## 架构原则

1. **Point-in-Time**: 任何数据查询只能返回查询时刻已知的信息，杜绝前视偏差
2. **不可变数据**: `data/raw/` 写入后不可修改，修正通过追加事件实现
3. **API 隔离**: 上层代码通过 `DataAPI` 单例访问数据，不直接操作文件系统
4. **事件驱动复权**: 存储原始价格 + 独立的分红送转事件表，逐日动态复权
5. **模拟-实盘统一**: 模拟账户与实盘共享相同的信号生成、订单管理、组合估值代码，仅在订单执行环节分岔

## 项目结构

```
kk_quant/
├── quant_engine/         # 阶段一：研究平台（数据层 + 回测引擎 + 因子 + 绩效）
│   ├── data/             # 日历、复权、存储、采集、API
│   ├── backtest/         # 事件驱动引擎与统一策略协议
│   ├── factor/           # 因子注册、内置因子与因子评价
│   └── analytics/        # Sharpe/MaxDD/CAPM/Fama-French
├── server/               # FastAPI API、业务服务、持久化与 WebSocket
├── web/                  # Vue 3 工作台
├── strategies/           # Strategy Protocol v2 策略实现与模板
├── tests/                # 自动化回归测试
└── Dockerfile.*          # 生产部署
```

## 关键设计决策

- **周频调仓**: 周五收盘计算信号 → 下周一开盘执行
- **回测引擎**: 事件驱动循环（Zipline范式），非向量化
- **因子体系**: 内置12个因子（动量/波动率/估值/技术/质量），IC + 分层回测 + 中性化
- **数据库**: 阶段一用 `data/meta.db`，阶段二独立出 `data/server.db`，单用户SQLite足矣
- **前端暗色主题**: CSS变量体系，暗色调金融终端风格

## 开发规范

- **Git**: 所有操作需用户批准后执行；多Agent分批完成后统一 review → 批准 → 提交
- **提交用户**: `kamran41666 <kamran416@sjtu.edu.cn>`
- **远程仓库**: `https://github.com/kamran41666/kk_quant.git`
- **语言**: 所有对话用中文
- **工作流**: Agent并行开发 → Code Review → 修复 → 审批 → 提交 → 推送

## 当前状态

- 当前权威进度：[`docs/project-status.md`](docs/project-status.md)。接手开发、判断阶段完成度或恢复施工时必须先读取该文件。
- 研究/回测、Web 工作台和 A 股/基金纸面链路已可用；Strategy Protocol v2 P0 已完成。
- 当前主线：稳定策略实现 → 固定真实数据集 → 样本外验证 → 连续纸面观察。
- 真实券商适配、完整认证与对账仍未完成，实盘能力保持关闭。

## 知识库与外部参考

- **知识库**: [`docs/knowledge-base.md`](docs/knowledge-base.md) — 量化策略、因子研究、过拟合防御、风控与仓位、系统架构、市场微观结构、因子衰减 7 大分类，40+ 条带评级的可落地知识
