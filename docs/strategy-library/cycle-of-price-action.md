# STR-001：Cycle of Price Action A股多头量化策略

**版本**：1.1
**报告日期**：2026-09-05  
**研究状态**：价格量能版实现完成，尚未真实数据回测
**提出者**：Oliver Kell  
**适用市场**：第一版为沪深A股  
**交易方向**：只做多  
**预期持仓周期**：约2至12周

## 1. 执行摘要

Cycle of Price Action（COPA）是一套以价格、成交量、移动平均线、多周期和市场环境为核心的主观交易框架，而不是参数完整公开的机械策略。公开的上行周期为：

```text
Reversal Extension
→ Wedge Pop
→ EMA Crossback
→ Base n’ Break
→ Exhaustion Extension
→ Wedge Drop
```

适合本项目的版本是“市场环境过滤 + 领导股筛选 + 个股有限状态机 + Wedge Pop/首次EMA Crossback/Base n’ Break入场 + ATR风险定仓 + 均线和结构退出”的日线纯多头策略。

本报告没有执行历史回测，不包含收益率、夏普、胜率或最大回撤结论。下文数值均为待验证的工程基准，不能表述为Oliver Kell公布的固定参数。

## 2. 来源与事实核验

2020年U.S. Investing Championship最终榜单显示，Oliver Kell在账户低于100万美元的股票组以+941.1%排名第一。当届共有124名参赛者。该成绩是特定账户、特定年度的个人实现结果，不能证明量化版本可获得相同收益。

主要来源：

1. [U.S. Investing Championship 2020最终榜单](https://financial-competitions.com/previousstandings/2021/1/13/december-2020-standings)
2. [TraderLion：Cycle of Price Action](https://traderlion.com/technical-analysis/chart-patterns/cycle-of-price-action-by-oliver-kell/)
3. [Oliver Kell访谈：风险、仓位和执行](https://stockbsessed.substack.com/p/oliver-kell-interview-the-mind-and)
4. [Swing Trading Masterclass最终课件](https://traderlion.com/wp-content/uploads/sites/2/2021/12/STMFinalWebinarPPTSlideDeck.pdf)

可以确认：Kell使用10/20日EMA观察中期趋势，50/200日SMA观察长期趋势；从周线开始分析，用日线管理交易，并用更低周期优化入场；公开访谈说明其倾向分批建仓、不向下摊平，并根据市场环境调整总敞口。

不能确认：2020年全部成交、杠杆细节、每次盘中判断、完整筛选器以及“紧密”“放量”“延伸”的唯一数值阈值。因此无法依据公开信息逐笔复现+941.1%。

## 3. 原策略流程与A股动作

| 阶段 | 公开含义 | A股多头动作 |
|---|---|---|
| Reversal Extension | 下跌后远离均线，在高周期支撑附近出现恐慌和反转 | 第一版只观察，不直接抄底 |
| Wedge Pop | 反弹后收缩，重新突破短期阻力并收复均线 | 主要初始买点 |
| EMA Crossback | 上涨启动后首次回踩10/20 EMA并出现承接 | 第二买点或盈利加仓点 |
| Base n’ Break | 趋势中横盘整理，均线追上后继续突破 | 趋势延续买点 |
| Exhaustion Extension | 趋势后段明显远离日线、周线均线 | 禁止追买，分批减仓 |
| Wedge Drop | 顶部收缩后放量跌破均线 | 清仓，不反手做空 |

原方法先选择盈利和销售增长较强、相对大盘领先、流动性良好的股票，再用价格和成交量决定交易。缺少公告日对齐的成长数据时，项目只能将第一版标为 `COPA_PRICE_ONLY`，不能称为完整原策略。

## 4. 固定量化模式

### 4.1 公共特征

```text
EMA10  = EMA(Close, 10)
EMA20  = EMA(Close, 20)
SMA50  = SMA(Close, 50)
SMA200 = SMA(Close, 200)
ATR14  = Wilder ATR(14)

RVOL20 = Volume_t / median(Volume[t-20:t-1])
RS20   = 股票20日收益率 - 基准20日收益率
RS63   = 股票63日收益率 - 基准63日收益率

EMA20Slope5 = EMA20_t / EMA20_{t-5} - 1
Extension   = (Close_t - EMA10_t) / ATR14_t
BandWidth_n = (max(High,n)-min(Low,n)) / Close_t
```

所有特征只能使用信号日及以前的数据。

### 4.2 趋势与收缩

```text
LONG_TREND =
    Close > EMA10 > EMA20
    AND EMA20Slope5 > 0
    AND Close > SMA50

DOWN_OR_RESET =
    Close < EMA20
    AND EMA20Slope5 <= 0

TIGHT_5 =
    最近5日价格区间 <= 1.5 * ATR14
    AND abs(EMA10-EMA20)/Close <= 1%
```

### 4.3 Wedge Pop

```text
前置：过去60日曾Close < EMA20；最近3至10日横盘或缓慢下移
触发：Close_t > max(High_{t-10:t-1})
      AND Close_t > EMA10_t
      AND Close_t > EMA20_t
      AND RVOL20_t >= 1.20
      AND RS63_t > 0
      AND 市场状态不是Risk-off
止损：min(整理区低点, EMA20_t - 0.5*ATR14_t)
```

### 4.4 首次EMA Crossback

```text
前置：过去20日内发生有效Wedge Pop，且尚未完成Crossback入场
回踩：Low_t <= max(EMA10_t, EMA20_t) + 0.25*ATR14_t
      AND Close_t >= EMA20_t
      AND Close位于当日振幅上半部
      AND Volume_t <= 1.2*median(Volume,20)
触发：收盘确认后下一交易日开盘买入；或下一日突破回踩日高点才买入
止损：min(回踩日低点, EMA20_t - 0.5*ATR14_t)
```

两种触发方式应作为两个独立模型回测，不能逐笔选择更优成交方式。

### 4.5 Base n’ Break

```text
前置：LONG_TREND；最近5至15日形成平台；平台未有效跌破EMA20
收缩：BandWidth_n <= 3.0*ATR14/Close，后半段波动不高于前半段
触发：Close_t > max(High_{t-n:t-1})
      AND RVOL20_t >= 1.20
      AND RS63_t > 0
      AND Extension_t <= 1.5
止损：max(EMA20_t-0.5*ATR14_t, 平台低点-0.1*ATR14_t)
```

## 5. 退出和风险管理

退出优先于开仓：

1. 停牌不假定成交；跌停卖单必须延续到下一可成交日。
2. 跳空跌破止损时按首个真实可成交价格退出，不能假定按止损价成交。
3. `Close < EMA20-0.25*ATR`且放量，或连续两日收于EMA20下方，下一日退出。
4. 入场后三日内最大有利变动不足`0.5R`且重新跌破枢轴，判定突破失败。
5. `Extension >= 2.5`且出现放量反转时减仓1/3；周线同时极端延伸时可再减1/3。
6. Wedge Drop触发后清仓，进入至少三个交易日的冷却期。

其中`R=实际入场价-初始止损价`。三日、0.5R、2.5ATR及减仓比例都是待检验参数。

风险定仓基准：

```text
risk_per_trade = 0.25% * account_equity
stop_distance  = entry_price - initial_stop
raw_shares     = floor(risk_per_trade / stop_distance)

初始单票市值 <= 账户权益15%
单一行业市值 <= 账户权益30%
订单规模     <= 20日平均成交额5%
```

首笔使用计划仓位的50%；只有价格确认并且组合风险不增加时才加第二笔。禁止浮亏加仓。

## 6. 市场环境

| 状态 | 研究基准 | 总仓位上限 | 新开仓 |
|---|---|---:|---|
| Risk-on | 指数Close>EMA20>SMA50，EMA20向上；市场宽度≥55% | 90% | 三类信号均允许 |
| Neutral | 不满足另外两类 | 50% | 只允许高质量Wedge Pop或首次Crossback |
| Risk-off | 指数Close<EMA20且EMA20向下；市场宽度≤40% | 0%至20% | 默认不开新仓 |

市场宽度定义为股票池中`Close>SMA50`的股票比例。建议连续两个收盘确认转换，并设置迟滞，避免状态抖动。

## 7. A股适配

第一版建议只研究沪深主板非ST股票，排除上市不足120个交易日、退市整理、长期停牌、流动性不足和数据异常股票。创业板、科创板和北交所应单独校准，因为其涨跌幅和申报单位不同。

制度来源：

- [上交所交易规则（2026年修订）](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
- [上交所科创板交易问答](https://edu.sse.com.cn/tib/qa/)
- [深交所创业板涨跌幅规则](https://www.szse.cn/www/investor/index/update/t20200729_580056.html)
- [北交所交易制度](https://www.bse.cn/important_news/200010675.html)
- [财政部、税务总局关于减半征收证券交易印花税的公告](https://www.mof.gov.cn/jrttts/202308/t20230828_3904235.htm)

回测必须按历史日期应用费用，并处理T+1、涨跌停、停牌、除权除息、退市和板块申报单位。

## 8. 回测协议

消融实验：

```text
S0 股票池等权基准
S1 市场状态 + 领导股过滤
S2 S1 + Wedge Pop
S3 S2 + 首次EMA Crossback
S4 S3 + Base n’ Break
S5 S4 + Exhaustion减仓
S6 S5 + 公告日对齐的成长基本面
```

信号必须在`t`日收盘后产生，最早在`t+1`日成交。研究至少包含按时间排序的训练、验证和最终封存测试，以及walk-forward、参数±10%/±20%扰动、交易成本翻倍、容量、行业与市值归因、市场状态分层、block bootstrap和极端交易贡献检查。

必须报告净收益、相对基准超额、Sharpe、Sortino、Calmar、最大回撤、恢复时间、按闭合交易计算的胜率与R分布、换手率、时间在场比例、拒单率、理论止损与实际退出偏差和容量。

## 9. 项目适配建议

当前项目可以复用point-in-time历史窗口、日线OHLCV、次日开盘执行、停牌和涨跌停检查、T+1持仓锁定以及事件驱动回测。

实现前建议补充：

1. 能表达形态、触发价、止损、有效期和分批信息的信号协议。
2. 每日运行且优先于开仓的退出风险引擎。
3. 个股状态机、枢轴、止损、R值、加仓和冷却期的持久化。
4. 部分成交、容量限制和跌停未成交订单续存。
5. 按板块配置申报单位、价格最小变动和涨跌幅规则。
6. 按历史日期版本化佣金、印花税、过户费和滑点。
7. 禁止正式研究使用固定股票清单静默兜底。
8. 公告日对齐的盈利与收入数据。
9. 按完整开仓和平仓计算已实现盈亏的交易账本。
10. 获得可信分钟数据后，才单独研究65分钟执行层。

## 10. 最终判断

COPA的可量化价值主要是：在有利环境中选择领导股，在波动收缩向扩张转换时以明确止损入场，只对盈利交易加仓，并在趋势后段主动降低风险。

推荐第一版采用日线、纯多头、8至12只持仓上限、风险定仓和市场状态控制总敞口。Reversal Extension只作观察，Exhaustion Extension只减仓，Wedge Drop清仓，下跌周期保持现金。

它目前是待验证的策略假设，而不是已经由冠军收益证明有效的机械系统。

## 11. 项目实现状态

已实现 `strategies.cycle_of_price_action.CycleOfPriceActionStrategy`，稳定 ID 为 `cycle-of-price-action-price-only`，协议版本为 Strategy Protocol v2，策略版本为 `1.0.0`。

实现采用每日收盘生成目标、下一交易日开盘执行，并从每次调用可见的 PIT 历史窗口确定性重放状态。相同历史输入在回测与重新实例化的纸面观察中产生相同目标，不依赖进程内状态快照。`ablation_stage` 固定支持 S1–S5：

- S1：股票池宽度环境与横截面领导强度；
- S2：增加 Wedge Pop；
- S3：增加首次 EMA Crossback 的收盘确认分支；
- S4：增加固定 10 日 Base Break；
- S5：增加日线 Exhaustion 一次性减仓代理。

当前实现与原规则的明确偏差：

- 市场状态使用股票池 `Close>SMA50` 宽度的两日确认；没有独立指数序列；
- 领导股使用 20/63 日收益横截面分位，不称为相对独立基准的 RS；
- 没有公告日对齐成长基本面、行业分类、ST/退市整理和精确上市日字段；代码规则只保留沪深主板，280 日完整历史覆盖 200 日均线和最长约 12 周状态重放；
- 风险仓位按信号收盘价到理论止损距离计算；下一日开盘跳空后不声称仍精确等于账户权益 0.25%；
- “下一日突破回踩日高点”、止损触发单、跌停排队、周线极端延伸和分钟执行未纳入本版本；
- 形态、失败突破、冷却和 Exhaustion 状态由价格路径重放；Crossback 加仓只有在持久组合的实际平均成本已盈利至少 `0.5R` 时才提升目标仓位，但初始止损仍来自信号日理论结构。

代码验收已覆盖协议发现、量比窗口、环境两日确认、退出与冷却重放、相同 PIT 输入确定性、仓位上限，以及信号日后下一交易日开盘成交。尚未导入固定真实数据集，因此本节不提供绩效结论，也不授权进入连续纸面观察。

## 12. 修订记录

| 版本 | 日期 | 修改内容 |
|---|---|---|
| 1.0 | 2026-09-05 | 完成来源核验、A股纯多头规则化方案、回测协议及项目适配建议 |
| 1.1 | 2026-09-08 | 完成 Strategy Protocol v2 的 COPA_PRICE_ONLY S1–S5 可表达部分实现，登记偏差与代码验收边界 |
