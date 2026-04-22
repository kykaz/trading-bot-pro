from __future__ import annotations

from datetime import UTC, datetime

from trading_bot.config import PaperConfig
from trading_bot.types import Fill, OrderIntent, Portfolio, Position, Side


class PaperExecutor:
    def __init__(self, config: PaperConfig) -> None:
        self.config = config

    def execute(self, order: OrderIntent, portfolio: Portfolio) -> Fill | None:
        fee_rate = self.config.fee_bps / 10_000
        slippage_rate = self.config.slippage_bps / 10_000
        signed_slippage = slippage_rate if order.side is Side.BUY else -slippage_rate
        executed_price = order.price * (1 + signed_slippage)
        if executed_price <= 0:
            return None
        notional = executed_price * order.size * max(order.contract_size, 1.0)
        fee_paid = notional * fee_rate
        cash_delta = -(notional + fee_paid) if order.side is Side.BUY else (notional - fee_paid)

        if portfolio.cash + cash_delta < 0:
            return None

        position = portfolio.positions.get(
            order.market_id,
            Position(market_id=order.market_id, contract_size=order.contract_size),
        )
        previous_size = position.size
        previous_average = position.average_price
        signed_size = order.size if order.side is Side.BUY else -order.size
        new_size = previous_size + signed_size
        realized_pnl = 0.0

        if previous_size > 0 and order.side is Side.SELL:
            closing_size = min(order.size, previous_size)
            realized_pnl = (
                (executed_price - previous_average)
                * closing_size
                * max(order.contract_size, 1.0)
                - fee_paid
            )
        elif previous_size < 0 and order.side is Side.BUY:
            closing_size = min(order.size, abs(previous_size))
            realized_pnl = (
                (previous_average - executed_price)
                * closing_size
                * max(order.contract_size, 1.0)
                - fee_paid
            )

        portfolio.cash += cash_delta
        portfolio.realized_pnl += realized_pnl

        if abs(new_size) < 1e-9:
            portfolio.positions.pop(order.market_id, None)
        else:
            position.size = new_size
            position.contract_size = order.contract_size
            if previous_size == 0 or previous_size * new_size < 0:
                position.average_price = executed_price
            elif previous_size * signed_size > 0:
                previous_notional = previous_average * abs(previous_size)
                added_notional = executed_price * order.size
                total_size = abs(previous_size) + order.size
                position.average_price = (previous_notional + added_notional) / max(total_size, 1e-9)
            else:
                position.average_price = previous_average
            position.updated_at = datetime.now(UTC)
            portfolio.positions[order.market_id] = position

        return Fill(
            market_id=order.market_id,
            side=order.side,
            price=executed_price,
            size=order.size,
            fee_paid=fee_paid,
            timestamp=datetime.now(UTC),
            realized_pnl=realized_pnl,
        )
