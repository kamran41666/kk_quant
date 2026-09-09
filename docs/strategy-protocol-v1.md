# kk_quant 统一策略协议 v1.0

> 文档状态：旧协议，仅用于兼容和历史解释。新增或修改策略统一使用 [`strategy-protocol-v2.md`](strategy-protocol-v2.md)。

策略必须实现 `quant_engine.backtest.strategy.Strategy`，并且只能通过本地 `strategies.<module>.<ClassName>` 类路径登记。

## 最小实现

```python
from datetime import date
from quant_engine.backtest.strategy import Strategy


class MyStrategy(Strategy):
    def initialize(self):
        self.lookback = int(self._strategy_kwargs.get("lookback", 20))

    def generate_signals(self, dt: date) -> dict[str, float]:
        # 返回证券代码到目标权重；返回 {} 表示空仓
        return {"000001.SZ": 1.0}
```

## 生命周期

引擎按以下顺序调用：`initialize` → `before_trading` → `generate_signals` → `on_rebalance` → `on_order_filled` → `teardown`。

只有 `initialize` 和 `generate_signals` 是必须实现的；其余钩子可按需重写。

## 输入与输出约束

- `initialize` 从 `self._strategy_kwargs` 读取 JSON 参数；参数必须是可序列化对象，不能包含账户密钥或网络配置。
- `generate_signals(dt)` 接收一个交易日 `datetime.date`，通过 `self.ctx` 或引擎数据处理器读取数据，不直接访问网络。
- 返回值为 `dict[str, float]`：键是证券代码，值是目标组合权重。
- 权重必须是有限数且不小于 0；空字典表示空仓。
- 权重归一化、交易日判断、费用、成交和风控由引擎统一处理，策略不重复实现。

## 市场与执行

当前协议版本支持 A 股和国内基金；美股回测尚未开放。通过同一策略定义可以进入回测和 paper-only 模拟观察，但不会连接真实券商、提交真实委托或转移真实资金。

页面接口 `GET /api/v1/strategies/protocol` 返回机器可读的同版本契约，策略管理页和后续校验器均以该接口为准。
