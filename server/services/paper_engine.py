"""Simulation Account — wraps phase-one backtest engine for daily paper trading"""
import json
import math
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Type
from zoneinfo import ZoneInfo

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


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _trade_date() -> date:
    """Return the platform accounting date in China Standard Time."""
    return datetime.now(SHANGHAI_TZ).date()


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
                self._portfolio._previous_total = last.total_value
                self._portfolio._current_total = last.total_value
                snapshot_day = date.fromisoformat(last.date[:10])
                # Load positions
                positions = (
                    db.query(PaperPositionModel)
                    .filter(PaperPositionModel.snapshot_date == last.date)
                    .all()
                )
                for pp in positions:
                    from quant_engine.backtest.types import Position
                    stored_unlock = None
                    if pp.unlock_date:
                        try:
                            stored_unlock = date.fromisoformat(pp.unlock_date)
                        except ValueError:
                            stored_unlock = None
                    # Older rows lack lot metadata. A same-day restore must be
                    # conservative; an older snapshot is already past T+1.
                    fallback_unlock = (
                        snapshot_day + timedelta(days=1)
                        if snapshot_day >= _trade_date()
                        else snapshot_day
                    )
                    locked_lots = []
                    try:
                        payload = json.loads(pp.locked_lots or "[]")
                        for item in payload if isinstance(payload, list) else []:
                            unlock = date.fromisoformat(str(item["unlock_date"]))
                            shares = int(item["shares"])
                            if shares > 0:
                                locked_lots.append((unlock, shares))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        locked_lots = []
                    unlock_date = stored_unlock or fallback_unlock
                    if locked_lots:
                        unlock_date = max(unlock for unlock, _ in locked_lots)
                    pos = Position(
                        code=pp.code,
                        shares=pp.shares,
                        avg_cost=pp.avg_cost,
                        market_value=pp.market_value,
                        unlock_date=unlock_date,
                    )
                    self._portfolio._positions[pp.code] = pos
                    if locked_lots:
                        self._portfolio._locked_lots[pp.code] = locked_lots
        finally:
            db.close()

    def _save_snapshot(self, dt: date, db):
        """Save today's account state to DB"""
        total_value = self._portfolio.total_value
        n_pos = len([p for p in self._portfolio.positions.values() if p.shares > 0])

        # Daily return: compare with previous day
        day_text = dt.isoformat()
        prev = (
            db.query(PaperSnapshot)
            .filter(PaperSnapshot.date < day_text)
            .order_by(PaperSnapshot.date.desc())
            .first()
        )
        daily_ret = 0.0
        if prev and prev.total_value > 0:
            daily_ret = (total_value - prev.total_value) / prev.total_value

        # A retry for the same date replaces that date's snapshot atomically
        # within the caller transaction instead of creating duplicate history.
        db.query(PaperPositionModel).filter(
            PaperPositionModel.snapshot_date == day_text
        ).delete(synchronize_session=False)
        db.query(PaperSnapshot).filter(
            PaperSnapshot.date == day_text
        ).delete(synchronize_session=False)

        snapshot = PaperSnapshot(
            date=day_text,
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
                locked_lots = [
                    {"unlock_date": unlock.isoformat(), "shares": shares}
                    for unlock, shares in self._portfolio._locked_lots.get(code, [])
                    if shares > 0 and unlock > dt
                ]
                pp = PaperPositionModel(
                    snapshot_date=day_text,
                    code=code,
                    shares=pos.shares,
                    avg_cost=pos.avg_cost,
                    market_value=pos.market_value,
                    weight=pos.market_value / total_mv if total_mv > 0 else 0.0,
                    unlock_date=pos.unlock_date.isoformat(),
                    locked_lots=json.dumps(locked_lots, sort_keys=True),
                )
                db.add(pp)
        # SessionLocal uses autoflush=False. Materialize the replacement rows
        # so another same-day save in the same transaction can delete them.
        db.flush()

    def run_daily(self, dt: Optional[date] = None) -> dict:
        """Execute one day's paper trading.

        Args:
            dt: The trading day to run. Defaults to today.

        Returns:
            dict with status and summary (for API response)
        """
        if dt is None:
            dt = _trade_date()

        if hasattr(self._calendar, "ensure_coverage"):
            calendar_report = self._calendar.ensure_coverage(dt, dt)
            if not calendar_report.get("complete"):
                return {
                    "status": "blocked",
                    "code": "TRADING_CALENDAR_UNAVAILABLE",
                    "reason": (
                        "A verified A-share trading calendar is required before paper valuation; "
                        f"source={calendar_report.get('source', 'unknown')}"
                    ),
                }
        if not self._calendar.is_trading_day(dt):
            return {"status": "skipped", "reason": f"{dt} is not a trading day"}

        # Get today's prices
        codes = list(self._portfolio.positions.keys())
        if not codes:
            # A paper account must still use a point-in-time universe.  Never
            # fall back to a hand-picked survivor list: doing so makes an
            # apparently successful first run introduce look-ahead bias.
            try:
                codes = self._api.index_components("000300", dt)[:50]
            except Exception as exc:
                return {
                    "status": "blocked",
                    "code": "PIT_STOCK_POOL_UNAVAILABLE",
                    "reason": (
                        "No point-in-time index constituent snapshot is available "
                        f"for {dt}; import a dated snapshot before starting paper trading ({type(exc).__name__})."
                    ),
                }
            if not codes:
                return {
                    "status": "blocked",
                    "code": "PIT_STOCK_POOL_EMPTY",
                    "reason": f"The point-in-time stock pool for {dt} is empty.",
                }

        try:
            prices_df = self._api.daily(
                codes=codes,
                start=dt,
                end=dt,
                fields=["close"],
                adjust="event_driven",
            )
        except Exception as exc:
            return {
                "status": "blocked",
                "code": "PAPER_PRICE_DATA_UNAVAILABLE",
                "reason": f"No market data available for {dt}: {type(exc).__name__}: {exc}",
            }
        if prices_df is None or prices_df.empty:
            return {
                "status": "blocked",
                "code": "PAPER_PRICE_DATA_UNAVAILABLE",
                "reason": f"No valid closing prices were returned for {dt}; no snapshot was saved.",
            }

        def close_price(code: str) -> Optional[float]:
            try:
                value = float(prices_df.loc[(code, dt), "close"])
            except (KeyError, TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value > 0 else None

        # Existing positions must never be valued at an invented zero.  A
        # paper run is explicitly blocked until every held security has a
        # valid close, preserving the previous snapshot and its equity.
        missing_position_prices = [
            code for code in self._portfolio.positions
            if close_price(code) is None
        ]
        if missing_position_prices:
            return {
                "status": "blocked",
                "code": "PAPER_POSITION_PRICES_INCOMPLETE",
                "missing_codes": missing_position_prices,
                "reason": (
                    f"Closing prices are missing or invalid for {len(missing_position_prices)} held "
                    "security(ies); no snapshot was saved."
                ),
            }
        missing_requested_prices = [
            code for code in codes
            if close_price(code) is None
        ]
        if missing_requested_prices:
            return {
                "status": "blocked",
                "code": "PAPER_PRICE_DATA_INCOMPLETE",
                "missing_codes": missing_requested_prices,
                "reason": (
                    f"Closing prices are missing or invalid for {len(missing_requested_prices)} "
                    "requested stock-pool security(ies); no snapshot was saved."
                ),
            }

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
                        price = close_price(code)
                        if price is None:
                            continue
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
                        price = close_price(order.code)
                        if price is None:
                            continue
                    except (KeyError, TypeError):
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
            price = close_price(code)
            if price is None:
                # This is defensive redundancy for mutations introduced by a
                # strategy/fill; the preflight above handles normal paths.
                return {
                    "status": "blocked",
                    "code": "PAPER_POSITION_PRICES_INCOMPLETE",
                    "missing_codes": [code],
                    "reason": "A held security lost its valid close before valuation; no snapshot was saved.",
                }
            prices_dict[code] = price

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
