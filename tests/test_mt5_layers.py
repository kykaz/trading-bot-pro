import unittest
from datetime import UTC, datetime, timedelta

from trading_bot.config import load_config
from trading_bot.mt5_layers import (
    build_mt5_layer_adjustments,
    decide_mt5_layer_entry,
    positions_for_opposite_close,
)
from trading_bot.types import MarketSnapshot, Side


def _snapshot(*, bid: float, ask: float, atr: float = 1.0) -> MarketSnapshot:
    return MarketSnapshot(
        market_id="mt5:XAUUSD",
        token_id="XAUUSD",
        question="Gold vs US Dollar",
        best_bid=bid,
        best_ask=ask,
        fair_probability=(bid + ask) / 2.0,
        volume_24h=10000.0,
        timestamp=datetime.now(UTC),
        source="mt5",
        market_type="mt5",
        symbol="XAUUSD",
        tick_size="0.01",
        size_precision=2,
        min_order_size=0.01,
        order_step_size=0.01,
        max_order_size=100.0,
        preferred_order_size=0.01,
        contract_size=100.0,
        context={
            "point": 0.01,
            "atr": atr,
        },
    )


class Mt5LayerLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config.toml")

    def test_allows_next_buy_layer_after_deeper_pullback(self) -> None:
        snapshot = _snapshot(bid=3349.45, ask=3349.60, atr=1.0)
        positions = [
            {
                "ticket": 1001,
                "type": 0,
                "volume": 0.01,
                "price_open": 3350.00,
                "time": (datetime.now(UTC) - timedelta(minutes=5)).timestamp(),
                "sl": 3347.0,
                "tp": 3351.0,
            }
        ]

        decision = decide_mt5_layer_entry(
            self.config.mt5_layers,
            snapshot,
            Side.BUY,
            positions,
            fallback_size=0.01,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.next_layer_index, 2)
        self.assertAlmostEqual(decision.requested_size, 0.01, places=6)

    def test_blocks_when_max_buy_layers_are_already_open(self) -> None:
        self.config.mt5_layers.max_long_layers = 3
        snapshot = _snapshot(bid=3349.45, ask=3349.60, atr=1.0)
        now = datetime.now(UTC)
        positions = [
            {"ticket": 1001, "type": 0, "volume": 0.01, "price_open": 3350.00, "time": (now - timedelta(minutes=7)).timestamp()},
            {"ticket": 1002, "type": 0, "volume": 0.01, "price_open": 3349.70, "time": (now - timedelta(minutes=5)).timestamp()},
            {"ticket": 1003, "type": 0, "volume": 0.01, "price_open": 3349.40, "time": (now - timedelta(minutes=3)).timestamp()},
        ]

        decision = decide_mt5_layer_entry(
            self.config.mt5_layers,
            snapshot,
            Side.BUY,
            positions,
            fallback_size=0.01,
        )

        self.assertFalse(decision.allowed)
        self.assertIn("Capas maximas", decision.reason)

    def test_builds_break_even_adjustments_after_second_layer(self) -> None:
        snapshot = _snapshot(bid=3351.20, ask=3351.35, atr=1.0)
        now = datetime.now(UTC)
        positions = [
            {
                "ticket": 2001,
                "type": 0,
                "volume": 0.01,
                "price_open": 3350.00,
                "time": (now - timedelta(minutes=9)).timestamp(),
                "sl": 3346.00,
                "tp": 3350.90,
            },
            {
                "ticket": 2002,
                "type": 0,
                "volume": 0.01,
                "price_open": 3349.60,
                "time": (now - timedelta(minutes=4)).timestamp(),
                "sl": 3346.00,
                "tp": 3350.90,
            },
        ]

        adjustments = build_mt5_layer_adjustments(
            self.config.mt5_layers,
            self.config.mt5_strategy,
            snapshot,
            positions,
        )

        self.assertEqual(len(adjustments), 2)
        self.assertTrue(all(adjustment.side is Side.BUY for adjustment in adjustments))
        self.assertTrue(all(adjustment.stop_loss > 3349.7 for adjustment in adjustments))

    def test_selects_only_opposite_positions_for_close(self) -> None:
        positions = [
            {"ticket": 3001, "type": 0, "volume": 0.01, "price_open": 3350.00},
            {"ticket": 3002, "type": 1, "volume": 0.01, "price_open": 3349.80},
        ]

        closable = positions_for_opposite_close(Side.BUY, positions)

        self.assertEqual(len(closable), 1)
        self.assertEqual(int(closable[0]["ticket"]), 3002)


if __name__ == "__main__":
    unittest.main()
