from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

_order_id_counter = itertools.count(1)
_fill_id_counter = itertools.count(1)


def next_order_id() -> int:
    return next(_order_id_counter)


def next_fill_id() -> int:
    return next(_fill_id_counter)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    id: int
    account_id: str
    product: str
    side: Side
    type: OrderType
    qty: int
    price: Optional[float]  # None for market orders
    remaining_qty: int
    status: OrderStatus
    timestamp: float
    reject_reason: Optional[str] = None

    @property
    def is_resting(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


@dataclass
class Fill:
    id: int
    product: str
    price: float
    qty: int
    timestamp: float
    maker_order_id: int
    taker_order_id: int
    maker_account_id: str
    taker_account_id: str
    taker_side: Side


@dataclass
class Position:
    qty: int = 0
    avg_cost: float = 0.0


@dataclass
class Account:
    id: str
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    frozen: bool = False

    def position_for(self, product: str) -> Position:
        return self.positions.setdefault(product, Position())
