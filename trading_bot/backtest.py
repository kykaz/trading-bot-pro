from __future__ import annotations

from dataclasses import dataclass

from trading_bot.config import AppConfig
from trading_bot.execution import PaperExecutor
from trading_bot.market import MarketDataSource
from trading_bot.risk import RiskEngine
from trading_bot.types import Fill, Portfolio


@dataclass(slots=True)
class BacktestResult:
    starting_cash: float
    ending_cash: float
    open_positions: int
    fills: list[Fill]


def run_backtest(
    config: AppConfig,
    market_data: MarketDataSource,
    iterations: int = 5,
    strategy=None,
) -> BacktestResult:
    portfolio = Portfolio(cash=config.bot.starting_cash)
    strategy = strategy
    risk = RiskEngine(config.bot)
    executor = PaperExecutor(config.paper)
    fills: list[Fill] = []

    if strategy is None:
        from trading_bot.strategy import EventValueStrategy

        strategy = EventValueStrategy(config.strategy, config.bot.min_edge)

    for _ in range(iterations):
        for snapshot in market_data.get_snapshots():
            signal = strategy.evaluate(snapshot)
            if signal is None:
                continue
            order = risk.propose_order(signal, snapshot, portfolio)
            if order is None:
                continue
            fill = executor.execute(order, portfolio)
            if fill is None:
                continue
            risk.register_fill(fill.market_id, fill.timestamp)
            fills.append(fill)

    return BacktestResult(
        starting_cash=config.bot.starting_cash,
        ending_cash=portfolio.cash,
        open_positions=len(portfolio.positions),
        fills=fills,
    )
