# 因子原料与高级候选 v1

> 文档状态：2026-09-08 因子基线。文中的“回测协议接入”只适用于具备 factor portal 的通用链路；500股隔离研究引擎仍明确拒绝因子需求。

记录日期：2026-09-08（Asia/Shanghai）

## 本轮边界

本轮在项目原有 `Factor` 单票接口之外增加多资产面板因子模块。新模块接受用户约定的
`(date, ticker)`，同时兼容项目实际的 `(code, date)`；内部统一转换成 `date x code` 做
时序滚动，再恢复原索引，因此滚动窗口不会跨股票。

当前目录共登记 21 个面板因子：15 个可用原料因子（价格、成交量、流动性、风险、
PIT 基本面）、1 个 Alpha#2 防未来版本，以及 5 个高级非线性候选。高级候选均使用
至少三类算子，并包含 `rank` 或 `ts_corr`。所有输出按每个可见日期的截面做 1%/99%
缩尾和 Z-Score；没有采用会让历史值依赖未来样本的全样本标准化。所有价格变化均显式
使用正向 `delay/shift`，负向 delay 会直接拒绝。

基本面原料通过现有 `DataAPI.fundamentals(as_of=...)` 读取最新已公告快照，公告修订不会
回填到历史日期。本机 `data/meta.db` 在本轮检查时 `fundamentals` 为 0 行，因此
PE/PB/ROE 原料没有进入实测 IC，也没有伪造替代数据。

## 本机事实验证

输入为本机日线库存全部 13 只证券，2018-01-02 至 2026-08-31，共 27,278 行，字段为
OHLC、volume、amount、turnover_rate，使用 `event_driven` 复权。相关性基线覆盖项目内置因子中
能由当前字段计算的 12 个因子，并额外加入防未来版本的 20 日动量、20 日波动率和 20 日平均
换手。预测标签与回测执行协议一致，为信号后下一交易日开盘进入、持有 5 个交易日后开盘
退出的收益，仅在研究评估层构造。

规范化输入哈希：`b23027d4bdce5533a80c81c21d03a4946acf66ef79dedfc8b78115281a67c9fd`。
复跑入口：`.venv/bin/python -m scripts.run_factor_research --start 2018-01-02 --end 2026-08-31`。

| 候选 | 最大绝对相关性 | 对应基线/已入选候选 | 5日 Rank IC 均值 | ICIR | 观测日 |
|---|---:|---|---:|---:|---:|
| breakout_dryup_quantile_20_ohlcv | 0.2898 | 防未来 20 日动量 | -0.00010 | -0.0003 | 2,084 |
| liquidity_shock_reversal_decay_20_ohlcv | 0.4199 | 内置市值代理 | 0.00395 | 0.0126 | 2,082 |
| range_compression_volume_release_20_ohlcv | 0.0954 | 缩量压缩突破 | 0.00895 | 0.0292 | 2,084 |
| tail_asymmetry_rank_20_close | 0.2454 | 内置下行波动 1M | -0.00668 | -0.0216 | 2,083 |
| volume_price_divergence_corr_10_ohlcv | 0.1910 | 内置 RSI 14 | 0.01796 | 0.0615 | 2,080 |

五个候选都通过了本轮 0.70 相关性门，但没有任何候选仅凭这次结果获得“强预测能力”结论。
样本只有 13 只当前本地库存证券，不是历史动态股票池，存在严重的代表性和生存者偏差；
IC/ICIR 只用于验证流程已连通，不能作为策略晋级证据。

## 回测协议接入

`DataRequirement` 新增 `factors` 声明。协议解析会从因子目录自动补齐所需原始字段和最大
预热窗口，`StrategyContext.get_factor(name, dt)` 通过 point-in-time 数据适配器返回当日
截面。若请求未来日期、未知因子、缺失字段或尚未定位交易日，链路会 fail closed。

策略声明示例：

```python
DataRequirement(
    "a_share_daily",
    (),
    1,
    factors=("volume_price_divergence_corr_10_ohlcv",),
)
```

信号日调用：

```python
score = self.get_factor("volume_price_divergence_corr_10_ohlcv", dt)
```

## 下一研究门

### 2026-09-08 独立收尾复核

已修复无有限观测/无可计算相关时误过门的问题，输出`insufficient_evidence`；legacy盘前hook只见上一已知交易日，收盘阶段才见当日。隔离的公司行动研究引擎暂不支持因子需求，预检明确拒绝，避免运行时AttributeError。

13股研究在`backtest_result/factor-research/review-20260908-v2`另建目录重跑，输入哈希与原始27278行不变；五候选各有2080—2084个IC观测，相关性和IC与旧报告一致，仍为弱证据，不晋级。该模块没有加入已冻结500股三策略矩阵。专项83项、共同工作树全量678项通过（1个既有matcher告警）。

1. 导入可追溯的 PIT 基本面公告历史后，验证 PE/PB/ROE 原料及公告覆盖率。
2. 使用历史动态成分、上市退市和 ST/停牌过滤重跑宽截面。
3. 在 Train/Validation/Sealed OOS 中预注册候选方向、持有期和成本假设。
4. 只有 OOS IC、分层单调性、换手和成本压力同时通过后，才创建可交易策略版本。

### 2026-09-09 P0 快速迭代

- `neutralize` 已加入显式截距，并在取对数前剔除非有限、零和负市值；精确线性行业/规模暴露测试验证残差只剩浮点误差，非法市值对应结果保持 NaN。
- `ResearchDataPortal.factor` 已使用冻结研究的调整信号视图计算非基本面面板因子，按 `as_of` 限制可见窗口并返回防御性副本；策略声明的 factor requirement 不再被隔离引擎一概拒绝。
- PIT 基本面在隔离研究中仍保持明确阻断，直到提供与冻结研究集对齐的公告时点适配器。
- 使用 `normalized-v2` 最后40个交易日和500只证券实测 `raw_return_lagged_1_close`：2026-08-31 返回500行、463个有限值，有限截面均值约0、总体标准差约1。
- 本阶段全量验证为后端682项通过（1个既有 matcher warning）。这证明因子数据接口和数值约束已接通，不代表候选具有预测能力，也尚未完成500股因子策略回测。
