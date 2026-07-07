"""回测引擎核心数据类型"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional
import uuid
import warnings


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    order_id: str
    code: str
    date: date
    side: OrderSide
    shares: int
    price_limit: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    fill_shares: int = 0
    reject_reason: Optional[str] = None

    @property
    def remaining(self) -> int:
        return self.shares - self.fill_shares

    def fill(self, shares: int):
        if shares > self.remaining:
            warnings.warn(
                f"成交股数({shares})超过剩余委托量({self.remaining})，已截断至剩余量"
            )
        self.fill_shares = min(shares, self.shares)
        if self.fill_shares >= self.shares:
            self.status = OrderStatus.FILLED
        elif self.fill_shares > 0:
            self.status = OrderStatus.PARTIAL

    def reject(self, reason: str):
        self.status = OrderStatus.REJECTED
        self.reject_reason = reason

    def cancel(self):
        if self.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            self.status = OrderStatus.CANCELLED


@dataclass
class Trade:
    trade_id: str
    order_id: str
    code: str
    date: date
    side: OrderSide
    shares: int
    price: float
    amount: float
    commission: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0

    @classmethod
    def from_order(
        cls, order: Order, price: float,
        commission: float = 0.0, stamp_duty: float = 0.0,
        slippage: float = 0.0
    ) -> "Trade":
        shares = order.fill_shares
        amount = shares * price
        return cls(
            trade_id=f"t_{uuid.uuid4().hex[:12]}",
            order_id=order.order_id,
            code=order.code, date=order.date,
            side=order.side, shares=shares,
            price=price, amount=amount,
            commission=commission, stamp_duty=stamp_duty,
            slippage=slippage,
        )


@dataclass
class Position:
    code: str
    shares: int
    avg_cost: float
    market_value: float
    unlock_date: date

    def is_locked(self, dt: date) -> bool:
        return dt < self.unlock_date

    @property
    def unrealized_pnl(self) -> float:
        if self.shares == 0:
            return 0.0
        return self.market_value - self.shares * self.avg_cost

    def __repr__(self):
        return (f"Position({self.code}, shares={self.shares}, "
                f"avg_cost={self.avg_cost:.2f}, "
                f"mv={self.market_value:.2f})")


@dataclass
class AccountState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.market_value
