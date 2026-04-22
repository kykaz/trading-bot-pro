from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trading_bot.config import BotConfig
from trading_bot.types import MarketSnapshot, OrderIntent, Portfolio, Side, Signal


@dataclass(slots=True)
class RiskState:
    cooldowns: dict[str, datetime] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.state = RiskState()

    def propose_order(
        self,
        signal: Signal,
        snapshot: MarketSnapshot,
        portfolio: Portfolio,
    ) -> OrderIntent | None:
        cooldown_until = self.state.cooldowns.get(signal.market_id)
        if cooldown_until and cooldown_until > signal.timestamp:
            return None

        if portfolio.realized_pnl <= -self.config.max_daily_loss:
            return None

        if len(portfolio.positions) >= self.config.max_open_positions and signal.market_id not in portfolio.positions:
            return None

        if portfolio.exposure() >= self.config.max_total_exposure:
            return None

        price = snapshot.best_ask if signal.side is Side.BUY else snapshot.best_bid
        max_size = snapshot.preferred_order_size
        if max_size <= 0:
            max_size = self.config.max_position_notional / max(price, 0.01)
        if snapshot.max_order_size > 0:
            max_size = min(max_size, snapshot.max_order_size)
        if max_size <= 0:
            return None

        if snapshot.market_type == "spot" and signal.side is Side.SELL:
            existing_position = portfolio.positions.get(signal.market_id)
            if existing_position is None or existing_position.size <= 0:
                return None
            max_size = min(max_size, existing_position.size)

        if snapshot.preferred_order_size > 0:
            size = self._clamp_size(snapshot.preferred_order_size, snapshot)
        else:
            size = self._clamp_size(max_size * signal.confidence, snapshot)
        if size <= 0:
            return None
        if snapshot.market_type == "spot" and signal.side is Side.BUY:
            estimated_notional = price * size
            if estimated_notional > portfolio.cash:
                affordable_size = portfolio.cash / max(price, 0.01)
                size = self._clamp_size(affordable_size, snapshot)
        if snapshot.market_type == "spot" and signal.side is Side.SELL:
            existing_position = portfolio.positions.get(signal.market_id)
            if existing_position is not None:
                size = min(size, self._clamp_size(existing_position.size, snapshot))
        if snapshot.min_order_size > 0 and size < snapshot.min_order_size:
            return None
        if size <= 0:
            return None

        return OrderIntent(
            market_id=signal.market_id,
            token_id=snapshot.token_id,
            side=signal.side,
            price=price,
            size=size,
            reason=signal.reason,
            symbol=snapshot.symbol,
            market_type=snapshot.market_type,
            tick_size=snapshot.tick_size,
            neg_risk=snapshot.neg_risk,
            order_type="market" if snapshot.market_type == "mt5" else "limit",
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            contract_size=snapshot.contract_size,
        )

    def register_fill(self, market_id: str, timestamp: datetime) -> None:
        self.state.cooldowns[market_id] = timestamp + timedelta(minutes=self.config.cooldown_minutes)

    @staticmethod
    def _clamp_size(size: float, snapshot: MarketSnapshot) -> float:
        if size <= 0:
            return 0.0
        if snapshot.order_step_size > 0:
            steps = max(int(size / snapshot.order_step_size), 1)
            size = steps * snapshot.order_step_size
        size = round(size, snapshot.size_precision)
        if snapshot.max_order_size > 0:
            size = min(size, round(snapshot.max_order_size, snapshot.size_precision))
        if snapshot.min_order_size > 0 and size < snapshot.min_order_size:
            return round(snapshot.min_order_size, snapshot.size_precision)
        return size
