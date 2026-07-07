"""结果记录器

回测过程中持续记录：持仓、组合、成交、委托、信号。
回测结束时统一写入 Parquet + JSON。
"""
import json
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from quant_engine.backtest.types import Trade, Order, Position


class Recorder:
    """回测结果记录器

    使用内存缓冲区收集数据，save() 时批量写入磁盘。
    """

    def __init__(self, output_dir: str = "backtest_result"):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓冲区
        self._positions: list[dict] = []
        self._portfolio: list[dict] = []
        self._trades: list[dict] = []
        self._orders: list[dict] = []
        self._signals: list[dict] = []

        # 元信息
        self._start_date: Optional[date] = None
        self._end_date: Optional[date] = None
        self._meta: dict = {}

    def set_meta(self, key: str, value):
        """设置回测元信息"""
        self._meta[key] = value

    def record_positions(self, dt: date, positions: dict[str, Position]):
        """记录日终持仓快照"""
        for code, pos in positions.items():
            if pos.shares > 0:
                self._positions.append({
                    "date": dt,
                    "code": code,
                    "shares": pos.shares,
                    "avg_cost": pos.avg_cost,
                    "market_value": pos.market_value,
                    "weight": 0.0,  # 由 save() 时计算
                })

    def record_portfolio(self, dt: date, portfolio):
        """记录日终组合总览"""
        self._start_date = self._start_date or dt
        self._end_date = dt

        self._portfolio.append({
            "date": dt,
            "total_value": portfolio.total_value,
            "cash": portfolio.cash,
            "market_value": portfolio.market_value,
            "daily_return": portfolio.daily_return,
            "n_positions": len(portfolio.positions),
        })

    def record_trade(self, trade: Trade):
        """记录成交"""
        self._trades.append({
            "trade_id": trade.trade_id,
            "order_id": trade.order_id,
            "code": trade.code,
            "date": trade.date,
            "side": trade.side.value,
            "shares": trade.shares,
            "price": trade.price,
            "amount": trade.amount,
            "commission": trade.commission,
            "stamp_duty": trade.stamp_duty,
            "slippage": trade.slippage,
        })

    def record_order(self, order: Order):
        """记录委托"""
        self._orders.append({
            "order_id": order.order_id,
            "code": order.code,
            "date": order.date,
            "side": order.side.value,
            "shares": order.shares,
            "price_limit": order.price_limit,
            "status": order.status.value,
            "fill_shares": order.fill_shares,
            "reject_reason": order.reject_reason,
        })

    def record_signal(self, dt: date, signals: dict[str, float]):
        """记录信号快照"""
        for code, weight in signals.items():
            self._signals.append({
                "date": dt,
                "code": code,
                "target_weight": weight,
            })

    def save(self):
        """保存所有记录到磁盘"""
        # 持仓
        if self._positions:
            df_pos = pd.DataFrame(self._positions)
            # 计算权重 (每日组内)
            daily_totals = df_pos.groupby("date")["market_value"].transform("sum")
            df_pos["weight"] = df_pos["market_value"] / daily_totals.replace(0, 1)
            df_pos.to_parquet(
                self._output_dir / "daily_positions.parquet", index=False
            )

        # 组合总览
        if self._portfolio:
            df_port = pd.DataFrame(self._portfolio)
            # 计算累计收益
            df_port = df_port.sort_values("date")
            df_port["cumulative_return"] = (
                (1 + df_port["daily_return"].fillna(0)).cumprod() - 1
            )
            df_port.to_parquet(
                self._output_dir / "daily_portfolio.parquet", index=False
            )

        # 成交
        if self._trades:
            df_trades = pd.DataFrame(self._trades)
            df_trades.to_parquet(
                self._output_dir / "trades.parquet", index=False
            )

        # 委托
        if self._orders:
            df_orders = pd.DataFrame(self._orders)
            df_orders.to_parquet(
                self._output_dir / "orders.parquet", index=False
            )

        # 信号
        if self._signals:
            df_signals = pd.DataFrame(self._signals)
            df_signals.to_parquet(
                self._output_dir / "signals.parquet", index=False
            )

        # 摘要
        summary = {
            "start_date": str(self._start_date) if self._start_date else None,
            "end_date": str(self._end_date) if self._end_date else None,
            "n_trading_days": len(self._portfolio),
            "n_trades": len(self._trades),
            "n_orders": len(self._orders),
            "n_signals_snapshots": len(self._signals),
        }
        summary.update(self._meta)

        with open(self._output_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2, default=str)
