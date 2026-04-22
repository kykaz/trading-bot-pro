import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from trading_bot.config import load_config
from trading_bot.main import find_candidate_order, force_demo_order, preview_order, run_once
from trading_bot.market import Mt5DataSource
from trading_bot.strategy import Mt5XauScalpStrategy
from trading_bot.types import MarketSnapshot, Side, Signal


class FakeMt5Client:
    def terminal_info(self) -> dict[str, object]:
        return {
            "name": "MetaTrader 5",
            "trade_allowed": True,
            "tradeapi_disabled": False,
            "connected": True,
        }

    def account_info(self) -> dict[str, object]:
        return {
            "login": 25115284,
            "server": "MetaQuotes-Demo",
            "balance": 10000.0,
            "equity": 10025.0,
            "currency": "USD",
            "trade_allowed": True,
            "trade_expert": True,
        }

    def symbol_info(self, symbol: str) -> dict[str, object]:
        return {
            "name": symbol,
            "description": "Gold vs US Dollar",
            "digits": 2,
            "point": 0.01,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "trade_contract_size": 100.0,
            "trade_tick_size": 0.01,
            "visible": True,
        }

    def symbol_tick(self, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "bid": 3350.20,
            "ask": 3350.45,
            "last": 3350.30,
            "time": datetime.now(UTC),
        }

    def copy_rates(self, symbol: str, timeframe: str, count: int) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        if timeframe == "M5":
            start = 3300.0
            for index in range(count):
                open_price = start + (index * 0.35)
                close_price = open_price + 0.18
                rows.append(
                    {
                        "time": now - timedelta(minutes=(count - index) * 5),
                        "open": round(open_price, 2),
                        "high": round(close_price + 0.22, 2),
                        "low": round(open_price - 0.14, 2),
                        "close": round(close_price, 2),
                        "tick_volume": 200 + index,
                    }
                )
            return rows

        base = 3340.0
        closes: list[float] = []
        for index in range(max(count - 6, 0)):
            closes.append(base + (index * 0.08))
        closes.extend([base + 24.0, base + 23.6, base + 23.2, base + 22.9, base + 23.1, base + 23.4])
        closes = closes[-count:]
        for index, close_price in enumerate(closes):
            open_price = closes[index - 1] if index > 0 else close_price - 0.08
            rows.append(
                {
                    "time": now - timedelta(minutes=(len(closes) - index)),
                    "open": round(open_price, 2),
                    "high": round(max(open_price, close_price) + 0.18, 2),
                    "low": round(min(open_price, close_price) - (0.35 if index == len(closes) - 2 else 0.16), 2),
                    "close": round(close_price, 2),
                    "tick_volume": 100 + index,
                }
            )
        return rows

    def positions_get(self, symbol: str | None = None) -> list[dict[str, object]]:
        return []

    def orders_get(self, symbol: str | None = None) -> list[dict[str, object]]:
        return []


class Mt5IntegrationTest(unittest.TestCase):
    def test_mt5_data_source_returns_snapshot(self) -> None:
        config = load_config("config.toml")
        source = Mt5DataSource(
            mt5_config=config.mt5,
            mt5_strategy_config=config.mt5_strategy,
            client=FakeMt5Client(),
        )
        snapshots = source.get_snapshots()
        self.assertEqual(len(snapshots), 1)
        snapshot = snapshots[0]
        self.assertEqual(snapshot.market_type, "mt5")
        self.assertEqual(snapshot.symbol, config.mt5.symbol)
        self.assertIn("m1_rsi", snapshot.context)
        self.assertIn("m5_fast_ema", snapshot.context)
        self.assertGreater(snapshot.context["atr_points"], 0)

    def test_mt5_strategy_emits_buy_signal_for_xau_pullback(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.core_reclaim_points = 0.0
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3350.20,
            best_ask=3350.45,
            fair_probability=3350.30,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 15, 0, tzinfo=UTC),
            source="mt5",
            market_type="mt5",
            symbol="XAUUSD",
            context={
                "point": 0.01,
                "spread_points": 25.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3350.1,
                "m1_rsi": 21.0,
                "atr": 0.9,
                "atr_points": 90.0,
                "last_close": 3350.15,
                "last_low": 3349.9,
                "last_high": 3350.5,
            },
        )
        signal = Mt5XauScalpStrategy(config.mt5_strategy).evaluate(snapshot)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, Side.BUY)
        self.assertIn("ma_gap_points", signal.features)
        self.assertIn("session_window", signal.features)
        self.assertEqual(signal.features["trade_profile"], "core")

    def test_mt5_strategy_can_emit_opportunistic_signal_outside_core_window(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.trading_mode = "mixed"
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3350.20,
            best_ask=3350.45,
            fair_probability=3350.30,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 10, 30, tzinfo=UTC),
            source="mt5",
            market_type="mt5",
            symbol="XAUUSD",
            context={
                "point": 0.01,
                "spread_points": 14.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3350.1,
                "m1_rsi": 18.0,
                "atr": 1.0,
                "atr_points": 100.0,
                "last_close": 3350.30,
                "last_low": 3349.9,
                "last_high": 3350.5,
            },
        )

        signal = Mt5XauScalpStrategy(config.mt5_strategy).evaluate(snapshot)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.features["trade_profile"], "oportunista")
        self.assertEqual(signal.features["session_window"], "Fuera de ventana")
        self.assertIn("profile=oportunista", signal.reason)

    def test_mt5_strategy_rejects_outside_window_when_core_only(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.trading_mode = "core"
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3350.20,
            best_ask=3350.45,
            fair_probability=3350.30,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 10, 30, tzinfo=UTC),
            source="mt5",
            market_type="mt5",
            symbol="XAUUSD",
            context={
                "point": 0.01,
                "spread_points": 14.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3350.1,
                "m1_rsi": 18.0,
                "atr": 1.0,
                "atr_points": 100.0,
                "last_close": 3350.30,
                "last_low": 3349.9,
                "last_high": 3350.5,
            },
        )

        signal = Mt5XauScalpStrategy(config.mt5_strategy).evaluate(snapshot)
        self.assertIsNone(signal)

    def test_mt5_strategy_core_reclaim_filter_can_block_weak_reentry(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.core_reclaim_points = 12.0
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3350.20,
            best_ask=3350.45,
            fair_probability=3350.30,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 15, 0, tzinfo=UTC),
            source="mt5",
            market_type="mt5",
            symbol="XAUUSD",
            context={
                "point": 0.01,
                "spread_points": 25.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3350.10,
                "m1_rsi": 21.0,
                "atr": 0.9,
                "atr_points": 90.0,
                "last_close": 3350.15,
                "last_low": 3349.9,
                "last_high": 3350.5,
            },
        )

        signal = Mt5XauScalpStrategy(config.mt5_strategy).evaluate(snapshot)
        self.assertIsNone(signal)

    def test_preview_order_mt5_fails_clean_without_terminal(self) -> None:
        with patch("trading_bot.main.find_candidate_order", side_effect=RuntimeError("terminal missing")):
            result = preview_order(source="mt5", validate_live=False)
        self.assertEqual(result, 1)

    def test_force_demo_order_submits_manual_mt5_order(self) -> None:
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3350.20,
            best_ask=3350.45,
            fair_probability=3350.30,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 15, 0, tzinfo=UTC),
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
                "atr": 0.9,
            },
        )

        class FakeSource:
            def get_snapshots(self):
                return [snapshot]

        captured = {}

        def fake_submit(app_config, order, live):
            captured["live"] = live
            captured["order"] = order
            return {
                "venue": "mt5",
                "validated": True,
                "submitted": True,
                "retcode": 0,
                "send_retcode": 10009,
                "comment": "Request executed",
                "deal": 123,
                "order_id": 456,
            }

        with patch("trading_bot.main.build_market_data_source", return_value=FakeSource()):
            with patch("trading_bot.main.submit_mt5_order", side_effect=fake_submit):
                result = force_demo_order(side="buy")

        self.assertEqual(result, 0)
        self.assertTrue(captured["live"])
        self.assertEqual(captured["order"].side, Side.BUY)
        self.assertEqual(captured["order"].market_type, "mt5")
        self.assertGreater(captured["order"].take_profit, captured["order"].price)
        self.assertLess(captured["order"].stop_loss, captured["order"].price)

    def test_run_once_mt5_fails_clean_without_terminal(self) -> None:
        class FailingSource:
            def get_snapshots(self):
                raise RuntimeError("terminal missing")

        class DummyStrategy:
            def evaluate(self, snapshot):
                return None

        with patch("trading_bot.main.build_runtime_stack", return_value=(FailingSource(), DummyStrategy())):
            result = run_once(source="mt5", live=False)
        self.assertEqual(result, 1)

    def test_find_candidate_order_blocks_when_mt5_layers_are_full(self) -> None:
        config = load_config("config.toml")
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3349.45,
            best_ask=3349.60,
            fair_probability=3349.52,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 15, 0, tzinfo=UTC),
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
                "spread_points": 15.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3349.4,
                "m1_rsi": 18.0,
                "atr": 1.0,
                "atr_points": 100.0,
                "last_close": 3349.5,
                "last_low": 3349.2,
                "last_high": 3349.8,
            },
        )

        class FakeSource:
            def get_snapshots(self):
                return [snapshot]

        class FakeStrategy:
            def evaluate(self, incoming_snapshot):
                return Signal(
                    market_id=incoming_snapshot.market_id,
                    side=Side.BUY,
                    confidence=0.72,
                    expected_edge=42.0,
                    fair_probability=incoming_snapshot.best_ask,
                    market_price=incoming_snapshot.mid_price,
                    reason="layer test",
                    features={},
                    timestamp=incoming_snapshot.timestamp,
                    stop_loss=3347.0,
                    take_profit=3350.4,
                )

        now = datetime.now(UTC)
        positions = [
            {"ticket": 4001, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price_open": 3350.00, "time": (now - timedelta(minutes=8)).timestamp()},
            {"ticket": 4002, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price_open": 3349.75, "time": (now - timedelta(minutes=5)).timestamp()},
            {"ticket": 4003, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price_open": 3349.50, "time": (now - timedelta(minutes=3)).timestamp()},
        ]

        with patch("trading_bot.main.build_runtime_stack", return_value=(FakeSource(), FakeStrategy())):
            with patch("trading_bot.main.get_mt5_account", return_value={"balance": 100.0, "equity": 100.0}):
                with patch("trading_bot.main.get_mt5_positions", return_value=positions):
                    with patch("trading_bot.main.get_mt5_open_orders", return_value=[]):
                        candidate = find_candidate_order(config, source="mt5", use_live_portfolio=True)

        self.assertIsNone(candidate)

    def test_find_candidate_order_blocks_extra_opportunistic_layers(self) -> None:
        config = load_config("config.toml")
        config.mt5_strategy.trading_mode = "mixed"
        config.mt5_strategy.opportunistic_max_layers_per_side = 1
        snapshot = MarketSnapshot(
            market_id="mt5:XAUUSD",
            token_id="XAUUSD",
            question="Gold vs US Dollar",
            best_bid=3349.45,
            best_ask=3349.60,
            fair_probability=3349.52,
            volume_24h=10000,
            timestamp=datetime(2026, 4, 22, 10, 30, tzinfo=UTC),
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
                "spread_points": 15.0,
                "m5_fast_ema": 3352.0,
                "m5_slow_ema": 3348.5,
                "m5_prev_fast_ema": 3351.5,
                "m5_prev_slow_ema": 3348.1,
                "m1_pullback_ema": 3349.4,
                "m1_rsi": 18.0,
                "atr": 1.0,
                "atr_points": 100.0,
                "last_close": 3349.55,
                "last_low": 3349.2,
                "last_high": 3349.8,
            },
        )

        class FakeSource:
            def get_snapshots(self):
                return [snapshot]

        class FakeStrategy:
            def evaluate(self, incoming_snapshot):
                return Signal(
                    market_id=incoming_snapshot.market_id,
                    side=Side.BUY,
                    confidence=0.82,
                    expected_edge=42.0,
                    fair_probability=incoming_snapshot.best_ask,
                    market_price=incoming_snapshot.mid_price,
                    reason="opportunistic layer test",
                    features={"trade_profile": "oportunista"},
                    timestamp=incoming_snapshot.timestamp,
                    stop_loss=3347.0,
                    take_profit=3350.4,
                )

        now = datetime.now(UTC)
        positions = [
            {"ticket": 4010, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price_open": 3349.50, "time": (now - timedelta(minutes=6)).timestamp()},
        ]

        with patch("trading_bot.main.build_runtime_stack", return_value=(FakeSource(), FakeStrategy())):
            with patch("trading_bot.main.get_mt5_account", return_value={"balance": 100.0, "equity": 100.0}):
                with patch("trading_bot.main.get_mt5_positions", return_value=positions):
                    with patch("trading_bot.main.get_mt5_open_orders", return_value=[]):
                        candidate = find_candidate_order(config, source="mt5", use_live_portfolio=True)

        self.assertIsNone(candidate)


if __name__ == "__main__":
    unittest.main()
