import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from trading_bot.alerts import AlertPayload, maybe_alert
from trading_bot.backtest import run_backtest
from trading_bot.config import load_config
from trading_bot.dashboard import build_dashboard_file
from trading_bot.live import build_order_preview, check_live_stack, set_kill_switch, submit_kraken_order
from trading_bot.local_runtime import ensure_env_template, read_env_file, save_kraken_credentials, update_live_flags
from trading_bot.main import preview_order, run_once
from trading_bot.market import KrakenBtcUsdDataSource, MockMarketDataSource, PolymarketPublicDataSource
from trading_bot.operator_panel import OperatorSnapshot, build_operator_panel_html
from trading_bot.portfolio_state import build_portfolio_state_path, load_portfolio, save_portfolio
from trading_bot.publish import _extract_marker
from trading_bot.readiness import evaluate_readiness
from trading_bot.risk import RiskEngine
from trading_bot.storage import build_storage
from trading_bot.strategy import BtcUsdMicrostructureStrategy, EventValueStrategy
from trading_bot.types import OrderIntent, Portfolio, Position, Side


class SmokeTest(unittest.TestCase):
    def test_backtest_produces_fills(self) -> None:
        config = load_config("config.toml")
        result = run_backtest(config, MockMarketDataSource(), iterations=3)
        self.assertGreater(result.starting_cash, 0)
        self.assertGreaterEqual(len(result.fills), 1)

    def test_real_data_source_returns_snapshots(self) -> None:
        config = load_config("config.toml")
        source = PolymarketPublicDataSource(
            gamma_host=config.polymarket.gamma_host,
            clob_host=config.polymarket.host,
            data_config=config.data,
            strategy_config=config.strategy,
        )
        try:
            snapshots = source.get_snapshots()
        except (URLError, HTTPError) as exc:
            self.skipTest(f"Polymarket publico no respondio durante la prueba: {exc}")
        self.assertGreaterEqual(len(snapshots), 1)
        self.assertGreater(snapshots[0].best_ask, 0)

    def test_btcusd_data_source_returns_snapshot(self) -> None:
        config = load_config("config.toml")
        source = KrakenBtcUsdDataSource(
            kraken_config=config.kraken,
            data_config=config.data,
        )
        try:
            snapshots = source.get_snapshots()
        except URLError as exc:
            self.skipTest(f"Kraken publico no respondio durante la prueba: {exc}")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].market_type, "spot")
        self.assertGreater(snapshots[0].best_bid, 1000)
        self.assertGreater(snapshots[0].min_order_size, 0)

    def test_strategy_emits_explainable_features(self) -> None:
        config = load_config("config.toml")
        strategy = EventValueStrategy(config.strategy, config.bot.min_edge)
        snapshot = MockMarketDataSource().get_snapshots()[0]
        signal = strategy.evaluate(snapshot)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIn("composite_score", signal.features)
        self.assertGreaterEqual(signal.confidence, config.strategy.min_confidence)

    def test_btcusd_strategy_emits_edge_bps(self) -> None:
        config = load_config("config.toml")
        strategy = BtcUsdMicrostructureStrategy(config.btc_strategy)
        snapshot = KrakenBtcUsdDataSource(
            kraken_config=config.kraken,
            data_config=config.data,
        ).get_snapshots()[0]
        signal = strategy.evaluate(snapshot)
        if signal is not None:
            self.assertIn("edge_bps", signal.features)
            self.assertGreaterEqual(signal.confidence, config.btc_strategy.min_confidence)

    def test_storage_creates_run_record(self) -> None:
        db_path = Path("var/test_trading_bot.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="mock", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1100.0, signals_count=2, fills_count=1, open_positions=1)
        summary = storage.fetch_run_summary(run.run_id)
        storage.close()

        self.assertEqual(summary["signals_count"], 2)
        self.assertEqual(summary["fills_count"], 1)
        self.assertTrue(db_path.exists())

    def test_storage_fetches_recent_rows(self) -> None:
        db_path = Path("var/test_trading_bot_rows.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="mock", command="test", starting_cash=1000.0)
        strategy = EventValueStrategy(config.strategy, config.bot.min_edge)
        signal = strategy.evaluate(MockMarketDataSource().get_snapshots()[0])
        assert signal is not None
        storage.log_signal(run.run_id, signal)
        storage.finish_run(run.run_id, ending_cash=1000.0, signals_count=1, fills_count=0, open_positions=0)

        runs = storage.fetch_recent_runs(limit=5)
        signals = storage.fetch_recent_signals(limit=5, run_id=run.run_id)
        fills = storage.fetch_recent_fills(limit=5, run_id=run.run_id)
        storage.close()

        self.assertEqual(len(runs), 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(len(fills), 0)
        self.assertIsNone(runs[0]["publish_status"])

    def test_portfolio_state_roundtrip(self) -> None:
        state_dir = Path("var/test_portfolio")
        state_path = build_portfolio_state_path(str(state_dir), "btcusd")
        if state_path.exists():
            state_path.unlink()

        portfolio = Portfolio(
            cash=9750.0,
            positions={
                "kraken:XXBTZUSD": Position(
                    market_id="kraken:XXBTZUSD",
                    size=0.0025,
                    average_price=74000.0,
                )
            },
            realized_pnl=12.5,
        )
        save_portfolio(state_path, portfolio)
        restored = load_portfolio(state_path, starting_cash=10000.0)

        self.assertEqual(restored.cash, 9750.0)
        self.assertIn("kraken:XXBTZUSD", restored.positions)
        self.assertAlmostEqual(restored.positions["kraken:XXBTZUSD"].size, 0.0025)
        self.assertAlmostEqual(restored.realized_pnl, 12.5)

    def test_dashboard_file_is_generated(self) -> None:
        db_path = Path("var/test_dashboard.db")
        html_path = Path("var/test_dashboard.html")
        if db_path.exists():
            db_path.unlink()
        if html_path.exists():
            html_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="mock", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1015.0, signals_count=2, fills_count=1, open_positions=1)
        readiness = evaluate_readiness(config, storage)
        build_dashboard_file(storage, output_path=str(html_path), limit=10, readiness=readiness)
        storage.close()

        self.assertTrue(html_path.exists())
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("Primero mira la mesa. El detalle puede esperar.", html)
        self.assertIn("Historial de corridas", html)
        self.assertIn("Semaforo operativo", html)

    def test_operator_panel_html_contains_controls(self) -> None:
        html = build_operator_panel_html(
            OperatorSnapshot(
                panel_source="mt5",
                readiness_verdict="PAPER OPERATIVO",
                readiness_summary="El sistema corre.",
                readiness_operational="LISTO",
                readiness_edge="FALLA",
                readiness_live="BLOQUEADO",
                kill_switch="INACTIVO",
                live_mode="DRY_RUN",
                live_enabled=False,
                live_dry_run=True,
                auth_status="SIN_AUTH",
                public_api_status="OK",
                kraken_pair="XBT/USD",
                credential_file="C:/tmp/.env.kraken.local",
                api_key_masked="miss...ing",
                api_secret_masked="miss...ing",
                portfolio_cash=10000.0,
                portfolio_realized_pnl=0.0,
                portfolio_positions=[],
                balances=[],
                balances_error="sin auth",
                open_orders=[],
                open_orders_error="sin auth",
                candidate_preview={"side": "buy", "price": 74000.1},
                recent_runs=[],
                recent_fills=[],
                mt5_session_state="ACTIVA",
                mt5_window_local="08:00-11:00",
                mt5_next_event_local="Cierra 11:00",
                mt5_setup_detected=True,
                mt5_setup_reason="Setup presente.",
                mt5_buy_layers=2,
                mt5_sell_layers=0,
                mt5_live_win_rate=None,
                mt5_live_trade_count=0,
                mt5_benchmark_win_rate=76.87,
                mt5_benchmark_profit_factor=1.09,
                mt5_status_error=None,
            ),
            last_action="Cabina lista",
            last_output="Nada ejecutado",
        )
        self.assertIn("Cabina de Operador", html)
        self.assertIn("Puente MT5", html)
        self.assertIn("Ejecutar Ciclo Demo", html)
        self.assertIn("Enviar Orden Demo", html)
        self.assertIn("Kill Switch ON", html)
        self.assertIn("Comprar Demo Ahora", html)
        self.assertNotIn("Guardar Credenciales", html)
        self.assertIn("Radar XAU", html)
        self.assertIn("76.9%", html)
        self.assertIn("Pulso y Tendencias", html)

    def test_operator_panel_html_switches_to_alpaca_labels(self) -> None:
        html = build_operator_panel_html(
            OperatorSnapshot(
                panel_source="btcusd",
                readiness_verdict="PAPER OPERATIVO",
                readiness_summary="Broker paper listo.",
                readiness_operational="LISTO",
                readiness_edge="FALLA",
                readiness_live="BLOQUEADO",
                kill_switch="INACTIVO",
                live_mode="PAPER_BROKER_ON",
                live_enabled=True,
                live_dry_run=False,
                auth_status="SIN_AUTH",
                public_api_status="OK",
                kraken_pair="BTC/USD",
                credential_file="C:/tmp/.env.alpaca.local",
                api_key_masked="miss...ing",
                api_secret_masked="miss...ing",
                portfolio_cash=10000.0,
                portfolio_realized_pnl=0.0,
                portfolio_positions=[],
                balances=[],
                balances_error="sin auth",
                open_orders=[],
                open_orders_error="sin auth",
                candidate_preview=None,
                recent_runs=[],
                recent_fills=[],
                mt5_session_state="FUERA",
                mt5_window_local="08:00-11:00",
                mt5_next_event_local="Abre 08:00",
                mt5_setup_detected=False,
                mt5_setup_reason="Sin setup.",
                mt5_buy_layers=0,
                mt5_sell_layers=0,
                mt5_live_win_rate=None,
                mt5_live_trade_count=0,
                mt5_benchmark_win_rate=None,
                mt5_benchmark_profit_factor=None,
                mt5_status_error=None,
            ),
            last_action="Cabina lista",
            last_output="Nada ejecutado",
        )
        self.assertIn("Alpaca Paper", html)
        self.assertIn("Auth Alpaca", html)
        self.assertIn("Cuenta Alpaca", html)
        self.assertIn("Abrir Puente Alpaca", html)

    def test_alpaca_live_check_reports_missing_auth(self) -> None:
        config = load_config("config.toml")
        with patch.dict("os.environ", {}, clear=True):
            report = check_live_stack(config, venue="alpaca")
        self.assertEqual(report.venue, "alpaca")
        self.assertEqual(report.auth_status, "SIN_AUTH")

    def test_local_runtime_can_save_credentials(self) -> None:
        env_path = Path("var/test_kraken.env")
        if env_path.exists():
            env_path.unlink()
        save_kraken_credentials(env_path, "abc123", "secret789")
        env_data = read_env_file(env_path)
        self.assertEqual(env_data["KRAKEN_API_KEY"], "abc123")
        self.assertEqual(env_data["KRAKEN_API_SECRET"], "secret789")

    def test_local_runtime_can_seed_commented_template(self) -> None:
        env_path = Path("var/test_alpaca_template.env")
        if env_path.exists():
            env_path.unlink()
        ensure_env_template(env_path, ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"])
        payload = env_path.read_text(encoding="utf-8")
        self.assertIn("# APCA_API_KEY_ID=", payload)
        self.assertIn("# APCA_API_SECRET_KEY=", payload)

    def test_local_runtime_can_update_live_flags(self) -> None:
        config_path = Path("var/test_config_flags.toml")
        config_path.write_text(
            "[kraken]\n"
            "enable_live_trading = false\n\n"
            "[kraken_live]\n"
            "enabled = false\n"
            "dry_run = true\n",
            encoding="utf-8",
        )
        update_live_flags(
            config_path,
            kraken_enable_live_trading=True,
            kraken_live_enabled=True,
            kraken_live_dry_run=False,
        )
        updated = config_path.read_text(encoding="utf-8")
        self.assertIn("enable_live_trading = true", updated)
        self.assertIn("enabled = true", updated)
        self.assertIn("dry_run = false", updated)

    def test_publish_output_parser(self) -> None:
        output = """
        Production: https://trading-bot.example.vercel.app
        Aliased: https://trading-bot-pro.vercel.app
        """
        self.assertEqual(_extract_marker(output, "Production: https://"), "https://trading-bot.example.vercel.app")
        self.assertEqual(_extract_marker(output, "Aliased: https://"), "https://trading-bot-pro.vercel.app")

    def test_alert_logic_triggers_on_activity(self) -> None:
        config = load_config("config.toml")
        config.alerts.open_dashboard_on_alert = False
        config.alerts.sound_on_alert = False
        triggered = maybe_alert(
            config.alerts,
            AlertPayload(
                signals_count=2,
                fills_count=0,
                run_id=1,
                dashboard_path=Path("index.html"),
                dashboard_url=None,
            ),
        )
        self.assertTrue(triggered)

    def test_readiness_report_flags_live_as_blocked(self) -> None:
        db_path = Path("var/test_readiness.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        config.readiness.min_real_runs = 1
        config.readiness.min_real_fills = 1
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="real", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1005.0, signals_count=1, fills_count=1, open_positions=0)
        storage.mark_publish_result(run.run_id, status="success", url="https://example.com", error=None)

        readiness = evaluate_readiness(config, storage)
        storage.close()

        self.assertEqual(readiness.operational_status, "LISTO")
        self.assertEqual(readiness.live_status, "BLOQUEADO")
        self.assertIn("PAPER", readiness.verdict)

    def test_readiness_counts_btcusd_as_real(self) -> None:
        db_path = Path("var/test_readiness_btcusd.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        config.readiness.min_real_runs = 1
        config.readiness.min_real_fills = 1
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="btcusd", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1005.0, signals_count=1, fills_count=1, open_positions=1)
        storage.mark_publish_result(run.run_id, status="skipped", url=None, error="Sin actividad relevante; se omite deploy.")

        readiness = evaluate_readiness(config, storage)
        storage.close()

        self.assertEqual(readiness.metrics.real_runs, 1)
        self.assertEqual(readiness.metrics.real_fills, 1)

    def test_readiness_detects_publish_failure(self) -> None:
        db_path = Path("var/test_readiness_publish.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="real", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1000.0, signals_count=0, fills_count=0, open_positions=0)
        storage.mark_publish_result(
            run.run_id,
            status="failed",
            url=None,
            error="Vercel alcanzo el limite diario de deployments del plan actual.",
        )

        readiness = evaluate_readiness(config, storage)
        storage.close()

        self.assertEqual(readiness.operational_status, "FALLA")
        self.assertIn("limite diario", readiness.gates[4].detail.lower())

    def test_publish_skip_counts_as_operationally_ok(self) -> None:
        db_path = Path("var/test_readiness_skip.db")
        if db_path.exists():
            db_path.unlink()

        config = load_config("config.toml")
        config.storage.sqlite_path = str(db_path)
        storage = build_storage(config.storage)
        run = storage.start_run(mode="paper", data_source="real", command="test", starting_cash=1000.0)
        storage.finish_run(run.run_id, ending_cash=1000.0, signals_count=0, fills_count=0, open_positions=0)
        storage.mark_publish_result(
            run.run_id,
            status="skipped",
            url=None,
            error="Sin actividad relevante; se omite deploy.",
        )

        readiness = evaluate_readiness(config, storage)
        storage.close()

        self.assertEqual(readiness.gates[4].status, "pass")

    def test_live_preview_and_kill_switch(self) -> None:
        config = load_config("config.toml")
        kill_path = Path("var/test_kill_switch.flag")
        if kill_path.exists():
            kill_path.unlink()
        config.live.kill_switch_path = str(kill_path)

        snapshot = MockMarketDataSource().get_snapshots()[0]
        strategy = EventValueStrategy(config.strategy, config.bot.min_edge)
        signal = strategy.evaluate(snapshot)
        assert signal is not None
        order = RiskEngine(config.bot).propose_order(signal, snapshot, Portfolio(cash=config.bot.starting_cash))
        assert order is not None

        set_kill_switch(config.live.kill_switch_path, True)
        preview = build_order_preview(config, snapshot, signal, order)
        report = check_live_stack(config)
        set_kill_switch(config.live.kill_switch_path, False)

        self.assertEqual(preview["kill_switch"], "ON")
        self.assertEqual(preview["token_id"], snapshot.token_id)
        self.assertEqual(report.kill_switch_status, "ACTIVO")

    def test_kraken_live_submit_is_blocked_when_disabled(self) -> None:
        config = load_config("config.toml")
        order = OrderIntent(
            market_id="kraken:XXBTZUSD",
            token_id="XXBTZUSD",
            side=Side.BUY,
            price=74000.0,
            size=0.001,
            reason="test",
            symbol="XBT/USD",
            market_type="spot",
            tick_size="0.1",
            order_type="limit",
        )

        with self.assertRaisesRegex(RuntimeError, "deshabilitado"):
            submit_kraken_order(config, order, validate=False)

    def test_kraken_validate_requires_auth(self) -> None:
        config = load_config("config.toml")
        order = OrderIntent(
            market_id="kraken:XXBTZUSD",
            token_id="XXBTZUSD",
            side=Side.BUY,
            price=74000.0,
            size=0.001,
            reason="test",
            symbol="XBT/USD",
            market_type="spot",
            tick_size="0.1",
            order_type="limit",
        )

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "credenciales Kraken"):
                submit_kraken_order(config, order, validate=True)

    def test_run_once_live_is_blocked_when_disabled(self) -> None:
        result = run_once(source="btcusd", live=True)
        self.assertEqual(result, 1)

    def test_preview_order_validate_live_falls_back_without_auth(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = preview_order(source="btcusd", validate_live=True)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
