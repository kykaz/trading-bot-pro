from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from trading_bot.xau_scalping import (
    ClosedTrade,
    OhlcBar,
    active_session_window,
    XauScalpConfig,
    aggregate_bars,
    in_session,
    load_dukascopy_bars,
    run_pullback_backtest,
    summarize_trades,
)


class XauScalpingTest(unittest.TestCase):
    @staticmethod
    def _sample_path() -> Path:
        path = Path("var/test_xau_load.json")
        path.write_text(
            json.dumps(
                [
                    {"timestamp": 1_700_000_000_000, "open": 2000, "high": 2001, "low": 1999, "close": 2000.5},
                    {"timestamp": 1_700_000_060_000, "open": 2000.5, "high": 2002, "low": 2000, "close": 2001.5},
                ]
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _base_timestamp() -> datetime:
        return datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)

    def test_load_dukascopy_bars_parses_rows(self) -> None:
        path = self._sample_path()
        bars = load_dukascopy_bars(path)
        self.assertEqual(len(bars), 2)
        self.assertAlmostEqual(bars[0].open, 2000.0)
        self.assertAlmostEqual(bars[1].close, 2001.5)

    def test_aggregate_bars_rolls_up_5_minutes(self) -> None:
        bars = [
            OhlcBar(
                timestamp=self._base_timestamp().replace(minute=index),
                open=100 + index,
                high=101 + index,
                low=99 - index,
                close=100.5 + index,
            )
            for index in range(5)
        ]
        rolled = aggregate_bars(bars, 5)
        self.assertEqual(len(rolled), 1)
        self.assertAlmostEqual(rolled[0].open, 100.0)
        self.assertAlmostEqual(rolled[0].high, 105.0)
        self.assertAlmostEqual(rolled[0].low, 95.0)
        self.assertAlmostEqual(rolled[0].close, 104.5)

    def test_summarize_trades_computes_win_rate(self) -> None:
        timestamp = self._base_timestamp()
        trades = [
            ClosedTrade("buy", timestamp, timestamp, 100, 101, 99, 101, 1.0, "tp"),
            ClosedTrade("sell", timestamp, timestamp, 101, 102, 103, 100, -1.0, "sl"),
            ClosedTrade("buy", timestamp, timestamp, 100, 101, 99, 101, 2.0, "tp"),
        ]
        stats = summarize_trades(trades, starting_balance=100.0, ending_balance=102.0, max_drawdown=1.0)
        self.assertEqual(stats.trades, 3)
        self.assertAlmostEqual(stats.win_rate, 66.6666666667, places=2)
        self.assertAlmostEqual(stats.net_pnl, 2.0)

    def test_run_pullback_backtest_handles_small_dataset(self) -> None:
        bars = [
            OhlcBar(
                timestamp=self._base_timestamp().replace(minute=index),
                open=2000 + index,
                high=2000.5 + index,
                low=1999.5 + index,
                close=2000.2 + index,
            )
            for index in range(20)
        ]
        report = run_pullback_backtest(
            bars,
            XauScalpConfig(
                session_start_utc=0,
                session_end_utc=23,
                fast_ema_period=2,
                slow_ema_period=3,
                pullback_ema_period=2,
                rsi_period=2,
                atr_period=2,
            ),
        )
        self.assertGreaterEqual(report.stats.trades, 0)
        self.assertGreaterEqual(report.stats.ending_balance, 0)

    def test_in_session_is_utc_based(self) -> None:
        timestamp = self._base_timestamp()
        self.assertTrue(in_session(timestamp, 14, 18))
        self.assertFalse(in_session(timestamp, 15, 18))

    def test_active_session_window_supports_multiple_windows(self) -> None:
        timestamp = datetime(2026, 4, 1, 12, 30, tzinfo=timezone.utc)
        window = active_session_window(
            timestamp,
            14,
            17,
            ("Londres|07:00-09:30", "Solape NY|12:20-15:30"),
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.label, "Solape NY")
        self.assertTrue(in_session(timestamp, 14, 17, ("Londres|07:00-09:30", "Solape NY|12:20-15:30")))


if __name__ == "__main__":
    unittest.main()
