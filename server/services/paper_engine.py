"""Simulation Account — wraps phase-one backtest engine for daily paper trading"""
import json
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Type

import pandas as pd

from quant_engine.data.api import DataAPI
from quant_engine.data.calendar import TradingCalendar
from quant_engine.backtest.types import OrderSide
from quant_engine.backtest.portfolio import Portfolio
from quant_engine.backtest.cost_model import CostModel
from quant_engine.backtest.order_manager import OrderManager
from quant_engine.backtest.matcher import Matcher
from quant_engine.backtest.strategy import Strategy

from server.models.database import SessionLocal
from server.models.schema import PaperSnapshot, PaperPosition as PaperPositionModel, Deviation
from server.config import settings


class SimulationAccount:
    """Persistent paper trading account.

    Loads state from SQLite on startup, runs one day at a time,
    saves snapshots after each run.
    """

    def __init__(
        self,
        strategy_class: Type[Strategy],
        strategy_params: dict,
        initial_capital: float = 1_000_000.0,
    ):
        self._strategy_cls = strategy_class
        self._strategy_params = strategy_params
        self._initial_capital = initial_capital
        self._portfolio = Portfolio(initial_capital=initial_capital)
        self._calendar = TradingCalendar()
        self._cost_model = CostModel()
        self._order_manager = OrderManager(self._cost_model)
        self._matcher = Matcher(self._cost_model)
        self._api = DataAPI()
        self._signals: dict[str, float] = {}
        self._current_weights: dict[str, float] = {}

        # Load state from DB
        self._load_state()

    def _load_state(self):
        """Load last snapshot from DB"""
        db = SessionLocal()
        try:
            last = (
                db.query(PaperSnapshot)
                .order_by(PaperSnapshot.date.desc())
                .first()
            )
            if last:
                self._portfolio = Portfolio(initial_capital=self._initial_capital)
                self._portfolio._cash = last.cash
                # Load positions
                positions = (
                    db.query(PaperPositionModel)
                    .filter(PaperPositionModel.snapshot_date == last.date)
                    .all()
                )
                for pp in positions:
                    from quant_engine.backtest.types import Position
                    # Reconstruct position (unlock_date unknown, assume unlocked)
                    pos = Position(
                        code=pp.code,
                        shares=pp.shares,
                        avg_cost=pp.avg_cost,
                        market_value=pp.market_value,
                        unlock_date=date.today(),  # assume already unlocked
                    )
                    self._portfolio._positions[pp.code] = pos
        finally:
            db.close()

    def _save_snapshot(self, dt: date, db):
        """Save today's account state to DB"""
        total_value = self._portfolio.total_value
        n_pos = len([p for p in self._portfolio.positions.values() if p.shares > 0])

        # Daily return: compare with previous day
        prev = (
            db.query(PaperSnapshot)
            .order_by(PaperSnapshot.date.desc())
            .first()
        )
        daily_ret = 0.0
        if prev and prev.total_value > 0:
            daily_ret = (total_value - prev.total_value) / prev.total_value

        snapshot = PaperSnapshot(
            date=dt.isoformat(),
            cash=self._portfolio.cash,
            market_value=self._portfolio.market_value,
            total_value=total_value,
            daily_return=daily_ret,
            n_positions=n_pos,
        )
        db.add(snapshot)
        db.flush()

        # Save positions
        total_mv = self._portfolio.market_value
        for code, pos in self._portfolio.positions.items():
            if pos.shares > 0:
                pp = PaperPositionModel(
                    snapshot_date=dt.isoformat(),
                    code=code,
                    shares=pos.shares,
                    avg_cost=pos.avg_cost,
                    market_value=pos.market_value,
                    weight=pos.market_value / total_mv if total_mv > 0 else 0.0,
                )
                db.add(pp)

    def run_daily(self, dt: Optional[date] = None) -> dict:
        """Execute one day's paper trading.

        Args:
            dt: The trading day to run. Defaults to today.

        Returns:
            dict with status and summary (for API response)
        """
        if dt is None:
            dt = date.today()

        if not self._calendar.is_trading_day(dt):
            return {"status": "skipped", "reason": f"{dt} is not a trading day"}

        # Get today's prices
        codes = list(self._portfolio.positions.keys())
        if not codes:
            # Use a default watchlist for initial run
            try:
                codes = self._api.index_components("000300", dt)[:50]
            except Exception:
                codes = ["000001.SZ", "000002.SZ", "000858.SZ", "002415.SZ", "600000.SH"]

        try:
            prices_df = self._api.daily(
                codes=codes,
                start=dt,
                end=dt,
                fields=["close"],
                adjust="event_driven",
            )
        except Exception:
            return {"status": "skipped", "reason": "No market data available"}

        # Check if it's a rebalance day (Friday)
        is_friday = dt.weekday() == 4

        context = type('Context', (), {
            'current_date': dt,
            '_registered_factors': [],
        })()

        strategy = self._strategy_cls(context, **self._strategy_params)
        strategy.initialize()

        if is_friday:
            try:
                self._signals = strategy.generate_signals(dt)
            except Exception:
                self._signals = {}

            if self._signals:
                # Convert signals to orders
                total_value = self._portfolio.total_value
                orders = []
                for code, target_weight in self._signals.items():
                    if target_weight <= 0:
                        continue
                    try:
                        price = float(prices_df.loc[(code, dt), 'close'])
                    except (KeyError, TypeError):
                        continue

                    target_amount = total_value * target_weight
                    target_shares = self._order_manager.round_lot(int(target_amount / price))
                    current = self._portfolio.positions.get(code)
                    current_shares = current.shares if current else 0
                    diff = target_shares - current_shares

                    if diff > 0:
                        orders.append(self._order_manager.submit(code, OrderSide.BUY, diff, dt))
                    elif diff < 0:
                        sellable = self._portfolio.get_sellable_shares(code, dt)
                        if sellable > 0:
                            orders.append(
                                self._order_manager.submit(code, OrderSide.SELL, min(abs(diff), sellable), dt)
                            )

                # Match orders
                fills = []
                for order in orders:
                    # Simple match: use close price directly (no DataHandler in paper mode)
                    try:
                        price = float(prices_df.loc[(order.code, dt), 'close'])
                    except KeyError:
                        continue

                    fill_shares = order.remaining
                    order.fill(order.fill_shares + fill_shares)

                    from quant_engine.backtest.types import Trade
                    trade = Trade.from_order(
                        order=order,
                        price=self._cost_model.est_fill_price(price, order.side),
                        commission=max(price * fill_shares * self._cost_model.commission_rate,
                                     self._cost_model.min_commission),
                        stamp_duty=price * fill_shares * self._cost_model.stamp_duty_rate if order.side == OrderSide.SELL else 0.0,
                        slippage=0.0,  # already in est_fill_price
                    )
                    self._portfolio.apply_trade(trade)
                    fills.append(trade)

        # Update market values
        prices_dict = {}
        for code in list(self._portfolio.positions.keys()):
            try:
                prices_dict[code] = float(prices_df.loc[(code, dt), 'close'])
            except (KeyError, TypeError):
                prices_dict[code] = 0.0

        self._portfolio.update_market_values(prices_dict, dt)

        # Save snapshot
        db = SessionLocal()
        try:
            self._save_snapshot(dt, db)
            db.commit()
        finally:
            db.close()

        return {
            "status": "completed",
            "date": dt.isoformat(),
            "cash": self._portfolio.cash,
            "market_value": self._portfolio.market_value,
            "total_value": self._portfolio.total_value,
            "is_rebalance_day": is_friday,
            "n_signals": len(self._signals),
            "n_positions": len([p for p in self._portfolio.positions.values() if p.shares > 0]),
        }

    def reset(self, initial_capital: float = 1_000_000.0):
        """Reset account to initial capital"""
        self._portfolio = Portfolio(initial_capital=initial_capital)
        self._signals = {}
        self._current_weights = {}

        # Clear DB snapshots
        db = SessionLocal()
        try:
            db.query(PaperPositionModel).delete()
            db.query(PaperSnapshot).delete()
            db.query(Deviation).delete()
            db.commit()
        finally:
            db.close()
