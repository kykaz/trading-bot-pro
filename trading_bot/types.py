from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(slots=True)
class MarketSnapshot:
    market_id: str
    token_id: str
    question: str
    best_bid: float
    best_ask: float
    fair_probability: float
    volume_24h: float
    timestamp: datetime
    source: str = "unknown"
    market_type: str = "binary"
    symbol: str = ""
    liquidity_imbalance: float = 0.0
    book_depth: float = 0.0
    tick_size: str = "0.01"
    size_precision: int = 2
    min_order_size: float = 0.0
    last_trade_price: float = 0.0
    vwap_24h: float = 0.0
    open_24h: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    neg_risk: bool = False
    contract_size: float = 1.0
    order_step_size: float = 0.0
    max_order_size: float = 0.0
    preferred_order_size: float = 0.0
    context: dict[str, float | int | str | bool] = field(default_factory=dict)

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return max(self.best_ask - self.best_bid, 0.0)


@dataclass(slots=True)
class Signal:
    market_id: str
    side: Side
    confidence: float
    expected_edge: float
    fair_probability: float
    market_price: float
    reason: str
    features: dict[str, float]
    timestamp: datetime
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass(slots=True)
class OrderIntent:
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    reason: str
    symbol: str = ""
    market_type: str = "binary"
    tick_size: str = "0.01"
    neg_risk: bool = False
    order_type: str = "GTC"
    stop_loss: float = 0.0
    take_profit: float = 0.0
    contract_size: float = 1.0


@dataclass(slots=True)
class Fill:
    market_id: str
    side: Side
    price: float
    size: float
    fee_paid: float
    timestamp: datetime
    realized_pnl: float = 0.0


@dataclass(slots=True)
class Position:
    market_id: str
    size: float = 0.0
    average_price: float = 0.0
    contract_size: float = 1.0
    updated_at: datetime | None = None


@dataclass(slots=True)
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0

    def exposure(self) -> float:
        return sum(
            abs(position.size) * position.average_price * max(position.contract_size, 1.0)
            for position in self.positions.values()
        )

    def equity(self, marks: dict[str, float] | None = None) -> float:
        marks = marks or {}
        marked_value = 0.0
        for market_id, position in self.positions.items():
            mark = marks.get(market_id, position.average_price)
            marked_value += position.size * mark * max(position.contract_size, 1.0)
        return self.cash + marked_value
