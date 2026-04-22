from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from trading_bot.config import load_config
from trading_bot.mt5_backtest import run_mt5_xau_backtest
from trading_bot.xau_scalping import OhlcBar


class Mt5BacktestTest(unittest.TestCase):
    @staticmethod
    def _bars() -> list[OhlcBar]:
        base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        bars: list[OhlcBar] = []
        price = 3300.0
        for index in range(240):
            if index % 12 in {8, 9}:
                price -= 0.9
            else:
                price += 0.45
            open_price = price - 0.10
            close_price = price
            bars.append(
                OhlcBar(
                    timestamp=base + timedelta(minutes=index),
                    open=round(open_price, 2),
                    high=round(max(open_price, close_price) + 0.25, 2),
                    low=round(min(open_price, close_price) - 0.35, 2),
                    close=round(close_price, 2),
                )
            )
        return bars

    def test_mt5_backtest_handles_empty_input(self) -> None:
        config = load_config("config.toml")
        report = run_mt5_xau_backtest(
            [],
            mt5_config=config.mt5,
            strategy_config=config.mt5_strategy,
            layer_config=config.mt5_layers,
            balance=100.0,
            spread=0.25,
        )
        self.assertEqual(report.stats.trades, 0)
        self.assertEqual(report.stats.ending_balance, 100.0)

    def test_mt5_backtest_returns_stats_for_synthetic_series(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.session_windows_utc = ("Sesion|00:00-23:59",)
        config.mt5_strategy.trading_mode = "always_on"
        config.mt5_strategy.core_reclaim_points = 0.0
        config.mt5_strategy.min_ma_gap_points = 5.0
        config.mt5_strategy.max_spread_points = 50.0
        report = run_mt5_xau_backtest(
            self._bars(),
            mt5_config=config.mt5,
            strategy_config=config.mt5_strategy,
            layer_config=config.mt5_layers,
            balance=100.0,
            spread=0.25,
        )
        self.assertGreaterEqual(report.stats.trades, 0)
        self.assertGreaterEqual(report.stats.max_drawdown, 0.0)
        self.assertGreaterEqual(report.stats.max_open_layers, 0)


if __name__ == "__main__":
    unittest.main()
