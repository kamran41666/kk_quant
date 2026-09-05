# KhQuant 可植入性评估

## 结论

KhQuant（按官方仓库 `khscience/OSkhQuant` 评估）可以提供数据适配、策略生命周期和初学者策略模板的设计参考，但不应整体嵌入当前项目。当前项目已经有 FastAPI + Vue、统一 `MarketDataProvider`、回测引擎、模拟交易和实盘准备控制面；直接复制 KhQuant 的 GUI、回测或交易模块会造成两套订单、费用和撮合语义。

## 可复用能力与当前落点

| KhQuant 能力 | 当前项目落点 | 植入方式 | 优先级 |
| --- | --- | --- | --- |
| MiniQMT/`xtquant` 实时与历史数据 | `quant_engine/data/live.py` provider 链 | 后续新增可选 `MiniQMTProvider`，统一输出 `MarketQuote`、`source`、`as_of`、`freshness` | P1 |
| `khHistory` 的复权、周期、截止时间 | DataAPI / 日 K provider | 先补契约测试和配置，不复制实现 | P1 |
| 双均线、RSI | `quant_engine/strategy` 与策略页 | 以当前 Strategy 接口重新实现，支持回测和模拟交易 | P1 |
| T+1/T+0、100 股整数倍、滑点和费用 | PaperBroker / RiskEngine | 转为可配置规则，订单执行前校验 | P1 |
| `init`/盘前/逐 bar/盘后生命周期 | Strategy runner | 作为策略事件模型参考，保持当前引擎的时间语义 | P2 |
| `MyTT.py` 指标集合 | 指标模块 | 只按需求重新实现并增加数值测试，不直接复制源码 | P2 |

## 当前已落地的数据链路

市场页 `/market` 现在按以下顺序取数：

1. 腾讯公开快照 `tencent:qt`；
2. AKShare Eastmoney，再回退 Sina；
3. 本地日线为空时使用腾讯 `tencent:kline` 前复权日 K；
4. 全部失败时返回结构化 `503`，不使用旧样本或生成值冒充实时数据。

腾讯公开接口只用于低频展示和研究。它没有券商交易柜台、鉴权、成交保证或本项目可验证的 SLA，不能作为真实下单确认依据。页面会显示 `freshness` 和实际 `source`；非交易日或超时数据会标为 `stale`/`unknown`。

## 推荐的 MiniQMT 适配方案

后续若用户安装了券商 MiniQMT，应通过独立 Windows worker/sidecar 进程接入：

```text
MiniQMT worker
  ├─ xtdata.subscribe_quote / get_full_tick
  ├─ xtdata.download_history_data / get_market_data_ex
  └─ HTTP/本地队列 → MarketDataProvider 契约
```

FastAPI 不直接加载 PyQt 或 MiniQMT 客户端。worker 必须输出连接健康状态、上游时间戳和来源，并在断连时 fail-closed；没有有效报价时，纸面成交和实盘草案不能继续。

## 策略迁移边界

推荐迁移“思想”而不是复制文件：

- 双均线：短均线上穿长均线产生买入候选，下穿产生卖出候选；
- RSI：超卖/超买仅作为候选信号，必须叠加仓位和风险限制；
- A 股规则：100 股整数倍、T+1 可卖数量、费用、滑点；
- 回测安全：信号只能使用 `current_time` 之前的数据，成交发生在下一可用 bar。

不复制 `GUIkhQuant.py`、`khFrame.py`、`khTrade.py`、`MyTT.py` 的整体实现，因为当前项目已经有对应领域模块。KhQuant 官方 `OSkhQuant` 源码采用 CC BY-NC 4.0，包含署名和非商业限制；未来若平台商业化，直接复制源码存在许可证风险，应使用独立实现并保留来源记录。

## 事实来源

- [OSkhQuant 官方仓库](https://github.com/khscience/OSkhQuant)
- [项目文件说明](https://github.com/khscience/OSkhQuant/blob/main/%E9%A1%B9%E7%9B%AE%E6%96%87%E4%BB%B6%E8%AF%B4%E6%98%8E.md)
- [策略开发文档](https://raw.githubusercontent.com/khscience/khquant-skill/main/skills/khquant/references/strategy-development.md)
- [数据管理文档](https://raw.githubusercontent.com/khscience/khquant-skill/main/skills/khquant/references/data-management.md)
- [OSkhQuant 许可证](https://github.com/khscience/OSkhQuant/blob/main/LICENSE)
