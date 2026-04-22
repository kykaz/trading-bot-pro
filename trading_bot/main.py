from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import webbrowser

from trading_bot.alerts import AlertPayload, maybe_alert
from trading_bot.backtest import run_backtest
from trading_bot.config import load_config
from trading_bot.dashboard import build_dashboard_file
from trading_bot.execution import PaperExecutor
from trading_bot.live import (
    arm_kraken_dead_man_switch,
    build_order_preview,
    cancel_all_alpaca_orders,
    cancel_all_kraken_orders,
    cancel_alpaca_order,
    close_mt5_position,
    cancel_kraken_order,
    check_live_stack,
    close_alpaca_position,
    get_alpaca_account,
    get_alpaca_balances,
    get_alpaca_open_orders,
    get_alpaca_positions,
    get_kraken_balances,
    get_kraken_open_orders,
    get_mt5_account,
    get_mt5_open_orders,
    get_mt5_positions,
    set_kill_switch,
    submit_alpaca_order,
    submit_kraken_order,
    submit_mt5_order,
    update_mt5_position_risk,
    validate_kraken_order,
)
from trading_bot.local_runtime import ensure_env_template, load_env_file
from trading_bot.market import (
    AlpacaBtcUsdDataSource,
    KrakenBtcUsdDataSource,
    MockMarketDataSource,
    Mt5DataSource,
    PolymarketPublicDataSource,
)
from trading_bot.mt5_backtest import default_mt5_gold_backtest_paths, load_bars_for_paths, run_mt5_xau_backtest
from trading_bot.mt5_layers import (
    build_mt5_layer_adjustments,
    decide_mt5_layer_entry,
    layer_status_lines,
    positions_for_opposite_close,
)
from trading_bot.mt5_status import build_mt5_session_status, write_mt5_benchmark
from trading_bot.portfolio_state import build_portfolio_state_path, load_portfolio, reset_portfolio, save_portfolio
from trading_bot.publish import publish_dashboard
from trading_bot.readiness import evaluate_readiness
from trading_bot.risk import RiskEngine
from trading_bot.storage import build_storage
from trading_bot.strategy import BtcUsdMicrostructureStrategy, EventValueStrategy, Mt5TrendStrategy, Mt5XauScalpStrategy
from trading_bot.types import Fill, OrderIntent, Portfolio, Position, Side
from trading_bot.xau_scalping import PRESETS as XAU_PRESETS
from trading_bot.xau_scalping import (
    XauScalpConfig,
    default_gold_backtest_paths,
    describe_session_windows_utc,
    load_dukascopy_bars,
    run_pullback_backtest,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
KRAKEN_ENV_PATH = PROJECT_ROOT / ".env.kraken.local"
ALPACA_ENV_PATH = PROJECT_ROOT / ".env.alpaca.local"
MT5_ENV_PATH = PROJECT_ROOT / ".env.mt5.local"
ALPACA_SIGNUP_URL = "https://app.alpaca.markets/account/signup"
ALPACA_LOGIN_URL = "https://app.alpaca.markets/account/login"
ALPACA_TRADING_DOCS_URL = "https://docs.alpaca.markets/docs/getting-started-with-trading-api"
MT5_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"
MT5_PYTHON_DOCS_URL = "https://www.mql5.com/en/docs/python_metatrader5"
GOLD_BENCHMARK_PATH = PROJECT_ROOT / "var" / "gold_backtest_last.json"


def load_app_config():
    load_env_file(KRAKEN_ENV_PATH)
    load_env_file(ALPACA_ENV_PATH)
    load_env_file(MT5_ENV_PATH)
    config = load_config(CONFIG_PATH)
    config.storage.sqlite_path = str((PROJECT_ROOT / config.storage.sqlite_path).resolve())
    config.vercel.dashboard_output = str((PROJECT_ROOT / config.vercel.dashboard_output).resolve())
    config.live.kill_switch_path = str((PROJECT_ROOT / config.live.kill_switch_path).resolve())
    config.paper.portfolio_state_dir = str((PROJECT_ROOT / config.paper.portfolio_state_dir).resolve())
    return config


def resolve_spot_venue(config) -> str:
    return config.spot.venue if config.spot.venue in {"kraken", "alpaca"} else "kraken"


def has_alpaca_credentials(config) -> bool:
    import os

    return bool(os.getenv(config.alpaca_paper.api_key_env)) and bool(os.getenv(config.alpaca_paper.api_secret_env))


def alpaca_paper_available(config) -> bool:
    return resolve_spot_venue(config) == "alpaca" and config.alpaca_paper.enabled and has_alpaca_credentials(config)


def build_market_data_source(config, source: str | None = None):
    selected_source = source or config.data.source
    if selected_source == "mt5":
        return Mt5DataSource(
            mt5_config=config.mt5,
            mt5_strategy_config=config.mt5_strategy,
        )
    if selected_source == "btcusd":
        if alpaca_paper_available(config):
            return AlpacaBtcUsdDataSource(
                alpaca_config=config.alpaca,
                alpaca_paper_config=config.alpaca_paper,
                data_config=config.data,
            )
        return KrakenBtcUsdDataSource(
            kraken_config=config.kraken,
            data_config=config.data,
        )
    if selected_source == "real":
        return PolymarketPublicDataSource(
            gamma_host=config.polymarket.gamma_host,
            clob_host=config.polymarket.host,
            data_config=config.data,
            strategy_config=config.strategy,
        )
    return MockMarketDataSource()


def build_strategy(config, source: str | None = None):
    selected_source = source or config.data.source
    if selected_source == "mt5":
        if config.mt5_strategy.name == "mt5_xau_scalp":
            return Mt5XauScalpStrategy(config.mt5_strategy)
        return Mt5TrendStrategy(config.mt5_strategy)
    if selected_source == "btcusd":
        return BtcUsdMicrostructureStrategy(config.btc_strategy)
    return EventValueStrategy(config.strategy, config.bot.min_edge)


def build_runtime_stack(config, source: str | None = None):
    return build_market_data_source(config, source=source), build_strategy(config, source=source)


def build_portfolio_for_source(config, source: str) -> tuple[Portfolio, Path]:
    state_path = build_portfolio_state_path(config.paper.portfolio_state_dir, source)
    if not config.paper.persist_portfolio:
        return Portfolio(cash=config.bot.starting_cash), state_path
    return load_portfolio(state_path, config.bot.starting_cash), state_path


def build_live_btcusd_portfolio(snapshot, balances: dict[str, float]) -> Portfolio:
    usd_balance = _first_balance(balances, "ZUSD", "USD")
    btc_balance = _first_balance(balances, "XXBT", "XBT", "BTC")
    portfolio = Portfolio(cash=usd_balance)
    if btc_balance > 0:
        from trading_bot.types import Position

        portfolio.positions[snapshot.market_id] = Position(
            market_id=snapshot.market_id,
            size=btc_balance,
            average_price=snapshot.last_trade_price or snapshot.mid_price,
            updated_at=snapshot.timestamp,
        )
    return portfolio


def build_alpaca_btcusd_portfolio(snapshot, account: dict[str, object], positions: list[dict[str, object]]) -> Portfolio:
    cash = _first_balance(
        {"USD": float(account.get("cash") or 0.0), "buying_power": float(account.get("buying_power") or 0.0)},
        "USD",
        "buying_power",
    )
    portfolio = Portfolio(cash=cash)
    for raw_position in positions:
        symbol = str(raw_position.get("symbol") or "")
        if symbol not in {snapshot.symbol, snapshot.token_id, snapshot.market_id.replace("alpaca:", "")}:
            continue
        qty = float(raw_position.get("qty") or 0.0)
        if qty <= 0:
            continue
        portfolio.positions[snapshot.market_id] = Position(
            market_id=snapshot.market_id,
            size=qty,
            average_price=float(raw_position.get("avg_entry_price") or snapshot.last_trade_price or snapshot.mid_price),
            updated_at=snapshot.timestamp,
        )
    return portfolio


def build_mt5_portfolio(snapshot, account: dict[str, object], positions: list[dict[str, object]]) -> Portfolio:
    portfolio = Portfolio(cash=float(account.get("balance") or 0.0))
    for raw_position in positions:
        symbol = str(raw_position.get("symbol") or "")
        if symbol != snapshot.symbol:
            continue
        volume = float(raw_position.get("volume") or 0.0)
        if volume <= 0:
            continue
        position_type = int(raw_position.get("type") or 0)
        signed_volume = volume if position_type == 0 else -volume
        portfolio.positions[snapshot.market_id] = Position(
            market_id=snapshot.market_id,
            size=signed_volume,
            average_price=float(raw_position.get("price_open") or snapshot.mid_price),
            contract_size=snapshot.contract_size,
            updated_at=snapshot.timestamp,
        )
    equity = float(account.get("equity") or portfolio.cash)
    portfolio.realized_pnl = equity - portfolio.cash
    return portfolio


def build_fill_from_alpaca_submission(snapshot, order, submission: dict[str, object]) -> Fill | None:
    filled_qty = float(submission.get("filled_qty") or 0.0)
    if filled_qty <= 0:
        return None
    fill_price = float(
        submission.get("filled_avg_price")
        or submission.get("limit_price")
        or snapshot.mid_price
    )
    return Fill(
        market_id=snapshot.market_id,
        side=order.side,
        price=fill_price,
        size=filled_qty,
        fee_paid=0.0,
        timestamp=datetime.now(snapshot.timestamp.tzinfo),
    )


def _first_balance(balances: dict[str, float], *names: str) -> float:
    for name in names:
        amount = balances.get(name)
        if amount is not None:
            return float(amount)
    return 0.0


def _has_open_order_for_pair(open_orders: list[dict[str, object]], pair: str, wsname: str) -> bool:
    normalized = {pair.upper(), wsname.upper().replace("/", "")}
    for row in open_orders:
        pair_value = str(row.get("pair") or "").upper().replace("/", "")
        if pair_value in normalized:
            return True
    return False


def _has_open_order_for_symbol(open_orders: list[dict[str, object]], *symbols: str) -> bool:
    normalized = {symbol.upper().replace("/", "") for symbol in symbols}
    for row in open_orders:
        candidate = str(row.get("pair") or row.get("symbol") or "").upper().replace("/", "")
        if candidate in normalized:
            return True
    return False


def get_spot_live_report(config):
    venue = resolve_spot_venue(config)
    return check_live_stack(config, venue=venue)


def get_spot_balances(config) -> dict[str, float]:
    if resolve_spot_venue(config) == "alpaca":
        return get_alpaca_balances(config)
    return get_kraken_balances(config)


def get_spot_open_orders(config) -> list[dict[str, object]]:
    if resolve_spot_venue(config) == "alpaca":
        return get_alpaca_open_orders(config)
    return get_kraken_open_orders(config)


def _decide_publish(config, run_id: int, signals_count: int, fills_count: int) -> tuple[str, str | None]:
    if not config.vercel.auto_publish_dashboard:
        return "disabled", "Autopublicacion apagada."
    if config.vercel.publish_on_activity_only and signals_count == 0 and fills_count == 0:
        return "skipped", "Sin actividad relevante; se omite deploy."

    storage = build_storage(config.storage)
    try:
        rows = storage.fetch_recent_runs(limit=25)
    finally:
        storage.close()

    previous_attempt = next(
        (
            row
            for row in rows
            if int(row["id"]) != run_id and row.get("publish_status") in {"success", "failed", "skipped"}
        ),
        None,
    )
    if previous_attempt and previous_attempt.get("ended_at"):
        previous_ended = datetime.fromisoformat(str(previous_attempt["ended_at"]))
        current_time = datetime.now(previous_ended.tzinfo)
        minutes_since = (current_time - previous_ended).total_seconds() / 60.0
        if minutes_since < config.vercel.min_publish_interval_minutes:
            return (
                "skipped",
                f"Cooldown de publish activo; ultimo intento hace {minutes_since:.0f} min.",
            )
    return "ready", None


def run_once(source: str | None = None, live: bool = False, panel_mode: bool = False) -> int:
    config = load_app_config()
    spot_venue = resolve_spot_venue(config)
    use_alpaca_paper = alpaca_paper_available(config)
    if panel_mode:
        config.alerts.open_dashboard_on_alert = False
        config.alerts.sound_on_alert = False
    storage = build_storage(config.storage)
    data_source = source or config.data.source
    if live and data_source not in {"btcusd", "mt5"}:
        print("live_error=El modo live integrado solo esta soportado para btcusd y mt5.")
        return 2
    if live:
        if Path(config.live.kill_switch_path).exists():
            print("live_error=El kill switch esta activo; no se puede correr run-once en live.")
            return 1
        if data_source == "mt5":
            if not config.mt5.enable_demo_trading:
                print("live_error=MT5 demo sigue deshabilitado en config.")
                return 1
        elif spot_venue == "alpaca":
            if not config.alpaca.enable_live_trading:
                print("live_error=Alpaca live sigue deshabilitado en config.")
                return 1
        else:
            if not config.kraken.enable_live_trading or not config.kraken_live.enabled:
                print("live_error=Kraken live sigue deshabilitado en config.")
                return 1
            if config.kraken_live.dry_run:
                print("live_error=Kraken live esta en DRY_RUN; desactivalo antes de usar --live.")
                return 1
    market_data, strategy = build_runtime_stack(config, source=data_source)
    risk = RiskEngine(config.bot)
    executor = PaperExecutor(config.paper)
    try:
        snapshots = market_data.get_snapshots()
    except Exception as exc:
        storage.close()
        print(f"data_error={exc}")
        return 1
    marks = {snapshot.market_id: snapshot.mid_price for snapshot in snapshots}
    portfolio_state_path = build_portfolio_state_path(config.paper.portfolio_state_dir, data_source)
    live_open_orders: list[dict[str, object]] = []
    starting_balances: dict[str, float] = {}
    mt5_positions: list[dict[str, object]] = []
    if data_source == "mt5" and snapshots and live:
        try:
            _, mt5_positions, live_open_orders, portfolio, starting_balances = _refresh_mt5_runtime_state(
                config,
                snapshots[0],
            )
        except Exception as exc:
            print(f"live_error={exc}")
            return 1
    elif data_source == "btcusd" and spot_venue == "alpaca" and snapshots and (use_alpaca_paper or live):
        try:
            alpaca_account = get_alpaca_account(config)
            alpaca_positions = get_alpaca_positions(config)
            starting_balances = get_alpaca_balances(config)
            portfolio = build_alpaca_btcusd_portfolio(snapshots[0], alpaca_account, alpaca_positions)
            live_open_orders = get_alpaca_open_orders(config)
        except Exception as exc:
            print(f"broker_error={exc}")
            return 1
    elif live and snapshots:
        try:
            starting_balances = get_kraken_balances(config)
            portfolio = build_live_btcusd_portfolio(snapshots[0], starting_balances)
            live_open_orders = get_kraken_open_orders(config)
        except Exception as exc:
            print(f"live_error={exc}")
            return 1
    else:
        portfolio, portfolio_state_path = build_portfolio_for_source(config, data_source)

    mode_label = "live" if live else config.bot.mode
    starting_equity = (
        float(starting_balances.get("equity") or 0.0)
        if data_source == "mt5" and live
        else portfolio.equity(marks)
    )
    run = storage.start_run(
        mode=mode_label,
        data_source=data_source,
        command="run-once-live" if live else "run-once",
        starting_cash=starting_equity,
    )
    signals_count = 0
    fills_count = 0
    submitted_orders = 0

    print(f"mode={mode_label}")
    print(f"data_source={data_source}")
    print(f"portfolio_state={portfolio_state_path}")
    if data_source == "mt5":
        print(f"mt5_symbol={config.mt5.symbol}")
        print(f"mt5_timeframe={config.mt5.timeframe}")
    if data_source == "btcusd":
        print(f"spot_venue={spot_venue}")
        if spot_venue == "alpaca" and not use_alpaca_paper:
            print("spot_broker_warning=Faltan credenciales Alpaca; usando simulacion local con datos publicos de Kraken.")
    if data_source == "mt5" and live:
        print(f"account_balance={starting_balances.get('balance', 0.0):.2f}")
        print(f"account_equity={starting_balances.get('equity', 0.0):.2f}")
        print(f"open_orders_before={len(live_open_orders)}")
        _log_mt5_layers(snapshots[0], mt5_positions)
        administered_layers = _administer_mt5_layers(config, snapshots[0], mt5_positions)
        if administered_layers:
            try:
                _, mt5_positions, live_open_orders, portfolio, starting_balances = _refresh_mt5_runtime_state(
                    config,
                    snapshots[0],
                )
            except Exception as exc:
                print(f"mt5_layer_admin_error={exc}")
                storage.close()
                return 1
            _log_mt5_layers(snapshots[0], mt5_positions)
    elif live or (data_source == "btcusd" and spot_venue == "alpaca" and use_alpaca_paper):
        print(f"account_cash={_first_balance(starting_balances, 'ZUSD', 'USD'):.2f}")
        print(f"account_btc={_first_balance(starting_balances, 'XXBT', 'XBT', 'BTC', config.alpaca.symbol, config.alpaca.legacy_symbol):.8f}")
        print(f"open_orders_before={len(live_open_orders)}")
    try:
        for snapshot in snapshots:
            signal = strategy.evaluate(snapshot)
            if signal is None:
                print(f"[skip] {snapshot.market_id}: no edge")
                continue

            layer_decision = None
            if data_source == "mt5" and live:
                opposite_positions = positions_for_opposite_close(signal.side, mt5_positions)
                if opposite_positions and config.mt5_layers.close_opposite_on_signal:
                    for closed in _close_mt5_opposite_layers(config, signal, mt5_positions):
                        print(
                            f"[mt5-layer-close] ticket={closed['ticket']} retcode={closed['retcode']} "
                            f"deal={closed['deal']} order_id={closed['order_id']} comment={closed['comment']}"
                        )
                    _, mt5_positions, live_open_orders, portfolio, starting_balances = _refresh_mt5_runtime_state(
                        config,
                        snapshot,
                    )
                    _log_mt5_layers(snapshot, mt5_positions)

            signals_count += 1
            storage.log_signal(run.run_id, signal)
            order = risk.propose_order(signal, snapshot, portfolio)
            if order is None:
                print(f"[blocked] {snapshot.market_id}: risk engine rejected signal")
                continue
            if data_source == "mt5":
                order, layer_decision = _prepare_mt5_layered_order(config, snapshot, signal, order, mt5_positions)
                if order is None:
                    print(f"[blocked] {snapshot.market_id}: {layer_decision.reason}")
                    continue

            print(f"[signal] market={snapshot.market_id} question={snapshot.question}")
            print(f"[features] {signal.features}")
            if data_source == "mt5" and layer_decision is not None:
                print(
                    f"[mt5-layer-check] allowed={layer_decision.allowed} next_layer={layer_decision.next_layer_index} "
                    f"same_side={layer_decision.same_side_layers} opposite={layer_decision.opposite_side_layers} "
                    f"size={order.size:.2f} reason={layer_decision.reason}"
                )
            if data_source == "mt5" and live:
                try:
                    submission = submit_mt5_order(config, order, live=True)
                    submitted_orders += 1 if submission["submitted"] else 0
                    _, mt5_positions, live_open_orders, portfolio, starting_balances = _refresh_mt5_runtime_state(
                        config,
                        snapshot,
                    )
                    print(
                        f"[mt5-order] validated={submission['validated']} submitted={submission['submitted']} "
                        f"retcode={submission.get('send_retcode', submission['retcode'])} "
                        f"deal={submission.get('deal', 0)} order_id={submission.get('order_id', 0)} "
                        f"open_orders_now={len(live_open_orders)} balance={starting_balances.get('balance', 0.0):.2f} "
                        f"equity={starting_balances.get('equity', 0.0):.2f}"
                    )
                    _log_mt5_layers(snapshot, mt5_positions)
                    _administer_mt5_layers(config, snapshot, mt5_positions)
                    _, mt5_positions, live_open_orders, portfolio, starting_balances = _refresh_mt5_runtime_state(
                        config,
                        snapshot,
                    )
                except Exception as exc:
                    print(f"[mt5-error] {snapshot.market_id}: {exc}")
                continue
            if data_source == "btcusd" and spot_venue == "alpaca" and (use_alpaca_paper or live):
                try:
                    symbol_has_open_orders = _has_open_order_for_symbol(
                        live_open_orders,
                        config.alpaca.symbol,
                        config.alpaca.legacy_symbol,
                    )
                    if config.alpaca_paper.cancel_existing_before_submit and symbol_has_open_orders:
                        cancelled = cancel_all_alpaca_orders(config)
                        print(
                            f"[cancel-all] cancelled={cancelled['cancelled']} count={cancelled['count']} "
                            f"errors={cancelled['errors']}"
                        )
                        live_open_orders = get_alpaca_open_orders(config)
                        symbol_has_open_orders = _has_open_order_for_symbol(
                            live_open_orders,
                            config.alpaca.symbol,
                            config.alpaca.legacy_symbol,
                        )
                    if config.alpaca_paper.skip_if_open_orders and symbol_has_open_orders:
                        print(f"[blocked] {snapshot.market_id}: ya existen ordenes abiertas en Alpaca para {config.alpaca.symbol}")
                        continue
                    submission = submit_alpaca_order(config, order, live=live)
                    submitted_orders += 1 if submission["submitted"] else 0
                    live_open_orders = get_alpaca_open_orders(config)
                    fill = build_fill_from_alpaca_submission(snapshot, order, submission)
                    if fill is None:
                        print(
                            f"[broker-order] venue=alpaca submitted={submission['submitted']} "
                            f"status={submission['status']} id={submission['id']} "
                            f"open_orders_now={len(live_open_orders)}"
                        )
                    else:
                        fills_count += 1
                        storage.log_fill(run.run_id, fill)
                        risk.register_fill(fill.market_id, fill.timestamp)
                        print(
                            f"[fill] market={fill.market_id} side={fill.side.value} "
                            f"price={fill.price:.4f} size={fill.size:.6f} fee={fill.fee_paid:.2f} "
                            f"status={submission['status']}"
                        )
                    refreshed_account = get_alpaca_account(config)
                    refreshed_positions = get_alpaca_positions(config)
                    portfolio = build_alpaca_btcusd_portfolio(snapshot, refreshed_account, refreshed_positions)
                    starting_balances = get_alpaca_balances(config)
                except Exception as exc:
                    print(f"[broker-error] {snapshot.market_id}: {exc}")
                continue
            if live:
                try:
                    pair_has_open_orders = _has_open_order_for_pair(
                        live_open_orders,
                        config.kraken.pair,
                        config.kraken.wsname,
                    )
                    if config.kraken_live.cancel_existing_before_submit and pair_has_open_orders:
                        cancelled = cancel_all_kraken_orders(config)
                        print(
                            f"[cancel-all] cancelled={cancelled['cancelled']} count={cancelled['count']} "
                            f"errors={cancelled['errors']}"
                        )
                        live_open_orders = get_kraken_open_orders(config)
                        pair_has_open_orders = _has_open_order_for_pair(
                            live_open_orders,
                            config.kraken.pair,
                            config.kraken.wsname,
                        )
                    if config.kraken_live.skip_if_open_orders and pair_has_open_orders:
                        print(f"[blocked] {snapshot.market_id}: ya existen ordenes abiertas en Kraken para {config.kraken.wsname}")
                        continue
                    if config.kraken_live.auto_arm_dead_man_switch:
                        dead_man = arm_kraken_dead_man_switch(config, config.kraken_live.dead_man_timeout_seconds)
                        print(
                            f"[dead-man] armed={dead_man['armed']} trigger_time={dead_man['trigger_time'] or 'n/a'} "
                            f"errors={dead_man['errors']}"
                        )
                    submission = submit_kraken_order(config, order, validate=False)
                    submitted_orders += 1 if submission["submitted"] else 0
                    live_open_orders = get_kraken_open_orders(config)
                    print(
                        f"[live-order] submitted={submission['submitted']} txid={submission['txid']} "
                        f"errors={submission['errors']} open_orders_now={len(live_open_orders)}"
                    )
                except Exception as exc:
                    print(f"[live-error] {snapshot.market_id}: {exc}")
                continue

            fill = executor.execute(order, portfolio)
            if fill is None:
                print(f"[rejected] {snapshot.market_id}: execution failed")
                continue

            fills_count += 1
            storage.log_fill(run.run_id, fill)
            risk.register_fill(fill.market_id, fill.timestamp)
            print(
                f"[fill] market={fill.market_id} side={fill.side.value} "
                f"price={fill.price:.4f} size={fill.size:.6f} fee={fill.fee_paid:.2f} "
                f"fair_value={signal.fair_probability:.4f} mid={snapshot.mid_price:.4f}"
            )
    finally:
        ending_equity = (
            float(starting_balances.get("equity") or 0.0)
            if data_source == "mt5" and live
            else portfolio.equity(marks)
        )
        storage.finish_run(
            run.run_id,
            ending_cash=ending_equity,
            signals_count=signals_count,
            fills_count=fills_count,
            open_positions=len(portfolio.positions),
        )
        summary = storage.fetch_run_summary(run.run_id)
        storage.close()
        if config.paper.persist_portfolio and not live and not (data_source == "btcusd" and spot_venue == "alpaca" and use_alpaca_paper):
            save_portfolio(portfolio_state_path, portfolio)

    dashboard_storage = build_storage(config.storage)
    try:
        readiness = evaluate_readiness(config, dashboard_storage)
        dashboard_path = build_dashboard_file(
            dashboard_storage,
            output_path=str(PROJECT_ROOT / config.vercel.dashboard_output),
            limit=20,
            readiness=readiness,
        )
    finally:
        dashboard_storage.close()
    print(f"dashboard_snapshot={dashboard_path.resolve()}")
    published_url: str | None = None
    publish_status = "disabled"
    publish_error: str | None = None
    publish_decision, publish_note = _decide_publish(
        config,
        run_id=int(summary["id"]),
        signals_count=signals_count,
        fills_count=fills_count,
    )
    if publish_decision == "ready":
        publish_status = "failed"
        try:
            publish_result = publish_dashboard(PROJECT_ROOT, config.vercel)
            published_url = publish_result.alias or publish_result.url
            publish_status = "success"
            print(f"vercel_publish_url={publish_result.url or 'unknown'}")
            if publish_result.alias:
                print(f"vercel_alias={publish_result.alias}")
        except Exception as exc:
            publish_error = str(exc)
            print(f"vercel_publish_error={publish_error}")
    else:
        publish_status = publish_decision
        publish_error = publish_note
        if publish_note:
            print(f"vercel_publish_skip={publish_note}")

    publish_storage = build_storage(config.storage)
    try:
        publish_storage.mark_publish_result(
            int(summary["id"]),
            status=publish_status,
            url=published_url,
            error=publish_error,
        )
        summary = publish_storage.fetch_run_summary(int(summary["id"]))
        refreshed_readiness = evaluate_readiness(config, publish_storage)
        build_dashboard_file(
            publish_storage,
            output_path=str(PROJECT_ROOT / config.vercel.dashboard_output),
            limit=20,
            readiness=refreshed_readiness,
        )
    finally:
        publish_storage.close()

    alerted = maybe_alert(
        config.alerts,
        AlertPayload(
            signals_count=signals_count,
            fills_count=fills_count,
            run_id=int(summary["id"]),
            dashboard_path=dashboard_path,
            dashboard_url=published_url,
        ),
    )
    if alerted:
        print(f"alert_triggered=run:{summary['id']} signals:{signals_count} fills:{fills_count}")

    final_equity = (
        float(starting_balances.get("equity") or 0.0)
        if data_source == "mt5" and live
        else portfolio.equity(marks)
    )
    print(
        f"cash={portfolio.cash:.2f} equity={final_equity:.2f} "
        f"open_positions={len(portfolio.positions)} submitted_orders={submitted_orders} run_id={summary['id']}"
    )
    return 0


def backtest(source: str | None = None) -> int:
    config = load_app_config()
    selected_source = source or config.data.source
    market_data, strategy = build_runtime_stack(config, source=selected_source)
    try:
        market_data.get_snapshots()
    except Exception as exc:
        print(f"backtest_error={exc}")
        return 1
    result = run_backtest(config, market_data, iterations=10, strategy=strategy)
    print(f"starting_cash={result.starting_cash:.2f}")
    print(f"ending_cash={result.ending_cash:.2f}")
    print(f"fills={len(result.fills)}")
    print(f"open_positions={result.open_positions}")
    return 0


def report_runs(limit: int) -> int:
    config = load_app_config()
    storage = build_storage(config.storage)
    try:
        rows = storage.fetch_recent_runs(limit=limit)
    finally:
        storage.close()

    if not rows:
        print("No runs found.")
        return 0

    for row in rows:
        print(
            f"run_id={row['id']} command={row['command']} source={row['data_source']} "
            f"mode={row['mode']} pnl={row['pnl']} signals={row['signals_count']} "
            f"fills={row['fills_count']} open_positions={row['open_positions']} "
            f"publish={row['publish_status'] or 'n/a'}"
        )
        if row.get("publish_error"):
            print(f"publish_error={row['publish_error']}")
    return 0


def report_signals(limit: int, run_id: int | None) -> int:
    config = load_app_config()
    storage = build_storage(config.storage)
    try:
        rows = storage.fetch_recent_signals(limit=limit, run_id=run_id)
    finally:
        storage.close()

    if not rows:
        print("No signals found.")
        return 0

    for row in rows:
        print(
            f"signal_id={row['id']} run_id={row['run_id']} market={row['market_id']} "
            f"side={row['side']} confidence={row['confidence']:.3f} edge={row['expected_edge']:.3f}"
        )
        print(f"reason={row['reason']}")
        print(f"features={json.dumps(row['features'], sort_keys=True)}")
    return 0


def report_fills(limit: int, run_id: int | None) -> int:
    config = load_app_config()
    storage = build_storage(config.storage)
    try:
        rows = storage.fetch_recent_fills(limit=limit, run_id=run_id)
    finally:
        storage.close()

    if not rows:
        print("No fills found.")
        return 0

    for row in rows:
        notional = row["price"] * row["size"]
        print(
            f"fill_id={row['id']} run_id={row['run_id']} market={row['market_id']} "
            f"side={row['side']} price={row['price']:.4f} size={row['size']:.6f} "
            f"notional={notional:.2f} fee={row['fee_paid']:.2f}"
        )
    return 0


def build_dashboard(limit: int, output: str, open_browser: bool) -> int:
    config = load_app_config()
    storage = build_storage(config.storage)
    try:
        target_output = output if Path(output).is_absolute() else str((PROJECT_ROOT / output).resolve())
        readiness = evaluate_readiness(config, storage)
        dashboard_path = build_dashboard_file(storage, output_path=target_output, limit=limit, readiness=readiness)
    finally:
        storage.close()

    resolved_path = dashboard_path.resolve()
    print(f"dashboard={resolved_path}")
    if open_browser:
        webbrowser.open(resolved_path.as_uri())
    return 0


def live_check(venue: str) -> int:
    config = load_app_config()
    report = check_live_stack(config, venue=venue)
    print(f"venue={report.venue}")
    print(f"live_mode={report.live_mode}")
    print(f"kill_switch={report.kill_switch_status}")
    print(f"public_api={report.public_api_status}")
    print(f"auth_stack={report.auth_status}")
    for detail in report.details:
        print(f"- {detail}")
    return 0


def mt5_session_status() -> int:
    config = load_app_config()
    status = build_mt5_session_status(
        config,
        timezone_name="America/Mexico_City",
        benchmark_path=GOLD_BENCHMARK_PATH,
    )
    print("venue=mt5")
    print(f"session_state={status.session_state}")
    print(f"window_utc={status.session_window_utc}")
    print(f"window_local={status.session_window_local}")
    print(f"next_event_local={status.next_event_local}")
    print(f"setup_detected={'SI' if status.setup_detected else 'NO'}")
    print(f"setup_reason={status.setup_reason}")
    print(f"buy_layers={status.buy_layers}")
    print(f"sell_layers={status.sell_layers}")
    if status.live_win_rate is None:
        print("live_win_rate=sd")
    else:
        print(
            f"live_win_rate={status.live_win_rate.win_rate:.2f}% "
            f"wins={status.live_win_rate.wins} losses={status.live_win_rate.losses} "
            f"trades={status.live_win_rate.trades} pnl={status.live_win_rate.pnl:.2f}"
        )
    if status.benchmark is None:
        print("benchmark_win_rate=sd")
    else:
        print(
            f"benchmark_preset={status.benchmark.preset} "
            f"benchmark_win_rate={status.benchmark.win_rate:.2f}% "
            f"benchmark_pf={status.benchmark.profit_factor:.2f} "
            f"benchmark_trades={status.benchmark.trades} "
            f"benchmark_net={status.benchmark.net_pnl:.2f}"
        )
    if status.error:
        print(f"mt5_status_error={status.error}")
        return 1
    return 0


def alpaca_connect(open_browser: bool = False) -> int:
    ensure_env_template(
        ALPACA_ENV_PATH,
        [
            "APCA_API_KEY_ID",
            "APCA_API_SECRET_KEY",
        ],
    )
    print("venue=alpaca")
    print(f"env_template={ALPACA_ENV_PATH}")
    print(f"signup_url={ALPACA_SIGNUP_URL}")
    print(f"login_url={ALPACA_LOGIN_URL}")
    print(f"docs_url={ALPACA_TRADING_DOCS_URL}")
    print("manual_step=Debes completar el alta y generar tus claves dentro de Alpaca.")
    print("next_step=Cuando las tengas, pegalas en la cabina o guardalas en .env.alpaca.local.")
    if open_browser:
        webbrowser.open_new_tab(ALPACA_SIGNUP_URL)
        webbrowser.open_new_tab(ALPACA_LOGIN_URL)
        webbrowser.open_new_tab(ALPACA_TRADING_DOCS_URL)
        print("browser_opened=true")
    return 0


def mt5_connect(open_browser: bool = False) -> int:
    ensure_env_template(
        MT5_ENV_PATH,
        [
            "MT5_LOGIN",
            "MT5_PASSWORD",
            "MT5_SERVER",
            "MT5_TERMINAL_PATH",
        ],
    )
    print("venue=mt5")
    print(f"env_template={MT5_ENV_PATH}")
    print(f"download_url={MT5_DOWNLOAD_URL}")
    print(f"docs_url={MT5_PYTHON_DOCS_URL}")
    print("manual_step=Instala MetaTrader 5, inicia sesion en una cuenta demo y guarda las credenciales MT5.")
    print("next_step=Despues corre live-check --venue mt5 y preview-order --source mt5 --validate-live.")
    if open_browser:
        webbrowser.open_new_tab(MT5_DOWNLOAD_URL)
        webbrowser.open_new_tab(MT5_PYTHON_DOCS_URL)
        print("browser_opened=true")
    return 0


def preview_order(source: str | None = None, validate_live: bool = False) -> int:
    config = load_app_config()
    selected_source = source or config.data.source
    spot_venue = resolve_spot_venue(config)
    live_portfolio_error: str | None = None
    try:
        candidate = find_candidate_order(config, selected_source, use_live_portfolio=validate_live)
    except Exception as exc:
        candidate = None
        live_portfolio_error = str(exc)
        if validate_live:
            try:
                candidate = find_candidate_order(config, selected_source, use_live_portfolio=False)
            except Exception:
                candidate = None
        elif selected_source == "mt5":
            print(f"preview_error={live_portfolio_error}")
            return 1
    if candidate is not None:
        snapshot, signal, order = candidate
        preview = build_order_preview(config, snapshot, signal, order)
        print(f"preview_source={selected_source}")
        for key, value in preview.items():
            print(f"{key}={value}")
        if live_portfolio_error:
            print(f"live_portfolio_warning={live_portfolio_error}")
        if validate_live:
            if selected_source == "mt5":
                try:
                    account = get_mt5_account(config)
                    print("live_validation=ok")
                    print(f"validation_login={account.get('login')}")
                    print(f"validation_server={account.get('server')}")
                    print(f"validation_trade_allowed={account.get('trade_allowed')}")
                except Exception as exc:
                    print("live_validation=failed")
                    print(f"validation_error={exc}")
            elif selected_source != "btcusd":
                print("live_validation=unsupported_for_source")
            elif spot_venue == "alpaca":
                try:
                    account = get_alpaca_account(config)
                    print("live_validation=ok")
                    print(f"validation_account_status={account.get('status') or 'unknown'}")
                    print(f"validation_trading_blocked={account.get('trading_blocked')}")
                except Exception as exc:
                    print("live_validation=failed")
                    print(f"validation_error={exc}")
            elif not config.kraken_live.validate_orders:
                print("live_validation=disabled_in_config")
            else:
                try:
                    validation = validate_kraken_order(config, order)
                    print(f"live_validation={'ok' if validation['validated'] else 'failed'}")
                    print(f"validation_errors={validation['errors']}")
                    print(f"validation_description={validation['description']}")
                except Exception as exc:
                    print("live_validation=failed")
                    print(f"validation_error={exc}")
        return 0

    print("No order candidate found.")
    return 0


def kill_switch(action: str) -> int:
    config = load_app_config()
    enabled = None
    if action == "on":
        enabled = True
    elif action == "off":
        enabled = False
    set_kill_switch(config.live.kill_switch_path, enabled)
    state = "ON" if Path(config.live.kill_switch_path).exists() else "OFF"
    print(f"kill_switch={state}")
    print(f"path={config.live.kill_switch_path}")
    return 0


def dead_man_switch(timeout_seconds: int | None) -> int:
    config = load_app_config()
    if resolve_spot_venue(config) == "alpaca":
        print("venue=alpaca")
        print("armed=False")
        print("error=Alpaca no usa dead-man switch en este flujo.")
        return 1
    selected_timeout = timeout_seconds or config.kraken_live.dead_man_timeout_seconds
    try:
        result = arm_kraken_dead_man_switch(config, selected_timeout)
    except Exception as exc:
        print("venue=kraken")
        print("armed=False")
        print(f"error={exc}")
        return 1
    print(f"venue={result['venue']}")
    print(f"armed={result['armed']}")
    print(f"timeout_seconds={result['timeout_seconds']}")
    if result["current_time"]:
        print(f"current_time={result['current_time']}")
    if result["trigger_time"]:
        print(f"trigger_time={result['trigger_time']}")
    if result["errors"]:
        print(f"errors={result['errors']}")
    return 0


def portfolio_command(action: str, source: str) -> int:
    config = load_app_config()
    if source == "mt5":
        if action == "reset":
            state_path = build_portfolio_state_path(config.paper.portfolio_state_dir, source)
            reset_portfolio(state_path)
            print(f"portfolio_state_reset={state_path}")
            print("note=El reset MT5 solo limpia el portfolio local; no cierra posiciones en el terminal.")
            return 0
        try:
            account = get_mt5_account(config)
            positions = get_mt5_positions(config)
        except Exception:
            state_path = build_portfolio_state_path(config.paper.portfolio_state_dir, source)
            portfolio = load_portfolio(state_path, config.bot.starting_cash)
            print(f"portfolio_state={state_path}")
            print(f"cash={portfolio.cash:.2f}")
            print(f"realized_pnl={portfolio.realized_pnl:.2f}")
            print(f"open_positions={len(portfolio.positions)}")
            if not portfolio.positions:
                print("positions=[]")
                return 0
            for market_id, position in portfolio.positions.items():
                print(
                    f"position market={market_id} size={position.size:.4f} "
                    f"avg_price={position.average_price:.5f}"
                )
            return 0

        print("portfolio_state=mt5-demo")
        print(f"cash={float(account.get('balance') or 0.0):.2f}")
        print(f"realized_pnl={float(account.get('equity') or 0.0) - float(account.get('balance') or 0.0):.2f}")
        print(f"open_positions={len(positions)}")
        if not positions:
            print("positions=[]")
            return 0
        for raw_position in positions:
            direction = "buy" if int(raw_position.get("type") or 0) == 0 else "sell"
            print(
                f"position market=mt5:{raw_position.get('symbol')} side={direction} "
                f"size={float(raw_position.get('volume') or 0.0):.4f} "
                f"avg_price={float(raw_position.get('price_open') or 0.0):.5f}"
            )
        return 0

    if source == "btcusd" and resolve_spot_venue(config) == "alpaca" and alpaca_paper_available(config):
        if action == "reset":
            try:
                cancel_all_alpaca_orders(config)
                if config.alpaca_paper.close_positions_on_reset:
                    close_alpaca_position(config, config.alpaca.symbol)
            except Exception as exc:
                print(f"portfolio_reset_error={exc}")
                return 1
            print("portfolio_state_reset=alpaca-paper")
            print(f"symbol={config.alpaca.symbol}")
            return 0

        try:
            account = get_alpaca_account(config)
            positions = get_alpaca_positions(config)
        except Exception as exc:
            print(f"portfolio_error={exc}")
            return 1

        print("portfolio_state=alpaca-paper")
        print(f"cash={float(account.get('cash') or 0.0):.2f}")
        print(f"realized_pnl={float(account.get('equity') or 0.0) - float(account.get('last_equity') or account.get('equity') or 0.0):.2f}")
        print(f"open_positions={len(positions)}")
        if not positions:
            print("positions=[]")
            return 0
        for raw_position in positions:
            print(
                f"position market=alpaca:{raw_position.get('symbol')} size={float(raw_position.get('qty') or 0.0):.8f} "
                f"avg_price={float(raw_position.get('avg_entry_price') or 0.0):.4f}"
            )
        return 0

    state_path = build_portfolio_state_path(config.paper.portfolio_state_dir, source)
    if action == "reset":
        reset_portfolio(state_path)
        print(f"portfolio_state_reset={state_path}")
        return 0

    portfolio = load_portfolio(state_path, config.bot.starting_cash)
    print(f"portfolio_state={state_path}")
    print(f"cash={portfolio.cash:.2f}")
    print(f"realized_pnl={portfolio.realized_pnl:.2f}")
    print(f"open_positions={len(portfolio.positions)}")
    if not portfolio.positions:
        print("positions=[]")
        return 0
    for market_id, position in portfolio.positions.items():
        print(
            f"position market={market_id} size={position.size:.8f} "
            f"avg_price={position.average_price:.4f}"
        )
    return 0


def kraken_balance() -> int:
    config = load_app_config()
    try:
        balances = get_spot_balances(config)
    except Exception as exc:
        print(f"balance_error={exc}")
        return 1

    print(f"venue={resolve_spot_venue(config)}")
    if not balances:
        print("balances={}")
        return 0
    for asset in sorted(balances):
        print(f"balance asset={asset} amount={balances[asset]:.8f}")
    return 0


def kraken_open_orders() -> int:
    config = load_app_config()
    try:
        rows = get_spot_open_orders(config)
    except Exception as exc:
        print(f"open_orders_error={exc}")
        return 1

    print(f"venue={resolve_spot_venue(config)}")
    if not rows:
        print("open_orders=[]")
        return 0
    for row in rows:
        if resolve_spot_venue(config) == "alpaca":
            print(
                f"open_order id={row['id']} symbol={row['symbol']} side={row['side']} "
                f"type={row['type']} tif={row['time_in_force']} price={row['limit_price']} "
                f"qty={row['qty']} filled={row['filled_qty']} status={row['status']}"
            )
        else:
            print(
                f"open_order txid={row['txid']} pair={row['pair']} type={row['type']} "
                f"ordertype={row['ordertype']} price={row['price']} volume={row['volume']} "
                f"filled={row['volume_exec']} status={row['status']}"
            )
    return 0


def submit_order(source: str, live: bool) -> int:
    config = load_app_config()
    spot_venue = resolve_spot_venue(config)
    try:
        candidate = find_candidate_order(config, source, use_live_portfolio=live)
    except Exception as exc:
        print(f"submit_error={exc}")
        return 1
    if candidate is None:
        print("No order candidate found.")
        return 0

    snapshot, signal, order = candidate
    preview = build_order_preview(config, snapshot, signal, order)
    print(f"submit_source={source}")
    for key, value in preview.items():
        print(f"{key}={value}")
    try:
        if source == "mt5":
            result = submit_mt5_order(config, order, live=live)
        elif source == "btcusd" and spot_venue == "alpaca":
            result = submit_alpaca_order(config, order, live=live)
        else:
            result = submit_kraken_order(config, order, validate=not live)
    except Exception as exc:
        print(f"submit_error={exc}")
        return 1

    print(f"venue={'mt5' if source == 'mt5' else (spot_venue if source == 'btcusd' else 'kraken')}")
    print(f"submission_mode={'live' if live else 'validate'}")
    print(f"validated={result['validated']}")
    print(f"submitted={result['submitted']}")
    if source == "mt5":
        print(f"retcode={result.get('send_retcode', result['retcode'])}")
        print(f"comment={result.get('comment', '')}")
        print(f"deal={result.get('deal', 0)}")
        print(f"order_id={result.get('order_id', 0)}")
    elif spot_venue == "alpaca" and source == "btcusd":
        print(f"status={result['status']}")
        print(f"id={result['id']}")
        print(f"filled_qty={result['filled_qty']}")
        print(f"filled_avg_price={result['filled_avg_price']}")
    else:
        print(f"errors={result['errors']}")
        print(f"description={result['description']}")
        print(f"txid={result['txid']}")
    return 0


def force_demo_order(
    *,
    side: str,
    size: float | None = None,
    take_profit_atr: float | None = None,
    stop_loss_atr: float | None = None,
) -> int:
    config = load_app_config()
    if not config.mt5.enable_demo_trading:
        print("force_demo_error=MT5 demo esta deshabilitado en config.")
        return 1

    try:
        snapshots = build_market_data_source(config, source="mt5").get_snapshots()
    except Exception as exc:
        print(f"force_demo_error={exc}")
        return 1

    if not snapshots:
        print("force_demo_error=MT5 no devolvio snapshot para el simbolo configurado.")
        return 1

    snapshot = snapshots[0]
    try:
        side_enum = Side(side)
    except ValueError:
        print(f"force_demo_error=Lado no soportado: {side}")
        return 1

    requested_size = size if size is not None else snapshot.preferred_order_size or config.mt5.order_size_lots
    order_size = _normalize_mt5_size(requested_size, snapshot)
    if order_size <= 0:
        print("force_demo_error=El tamano de la orden demo no es valido.")
        return 1

    atr = _context_float(snapshot, "atr")
    point = max(_context_float(snapshot, "point"), 0.01)
    take_multiple = take_profit_atr if take_profit_atr is not None else config.mt5_strategy.take_profit_atr
    stop_multiple = stop_loss_atr if stop_loss_atr is not None else config.mt5_strategy.stop_loss_atr
    stop_distance = max(atr * stop_multiple, point * 10)
    take_distance = max(atr * take_multiple, point * 4)
    entry_price = snapshot.best_ask if side_enum is Side.BUY else snapshot.best_bid
    stop_loss = entry_price - stop_distance if side_enum is Side.BUY else entry_price + stop_distance
    take_profit = entry_price + take_distance if side_enum is Side.BUY else entry_price - take_distance
    order = OrderIntent(
        market_id=snapshot.market_id,
        token_id=snapshot.token_id,
        side=side_enum,
        price=entry_price,
        size=order_size,
        reason="manual_demo_order",
        symbol=snapshot.symbol,
        market_type="mt5",
        tick_size=snapshot.tick_size,
        order_type="market",
        stop_loss=stop_loss,
        take_profit=take_profit,
        contract_size=snapshot.contract_size,
    )

    print("force_demo_source=mt5")
    print(f"symbol={snapshot.symbol}")
    print(f"side={order.side.value}")
    print(f"price={order.price}")
    print(f"size={order.size}")
    print(f"stop_loss={order.stop_loss}")
    print(f"take_profit={order.take_profit}")
    print(f"atr={atr}")
    try:
        result = submit_mt5_order(config, order, live=True)
    except Exception as exc:
        print(f"force_demo_error={exc}")
        return 1

    print("venue=mt5")
    print("submission_mode=force-demo-live")
    print(f"validated={result['validated']}")
    print(f"submitted={result['submitted']}")
    print(f"retcode={result.get('send_retcode', result['retcode'])}")
    print(f"comment={result.get('comment', '')}")
    print(f"deal={result.get('deal', 0)}")
    print(f"order_id={result.get('order_id', 0)}")
    accepted = int(result.get("send_retcode") or 0) in {10008, 10009}
    if accepted:
        print("mt5_hint=Revisa la pestana Trade del terminal para ver la posicion demo.")
        return 0
    return 1


def cancel_order(txid: str) -> int:
    config = load_app_config()
    try:
        if resolve_spot_venue(config) == "alpaca":
            result = cancel_alpaca_order(config, txid)
        else:
            result = cancel_kraken_order(config, txid)
    except Exception as exc:
        print(f"cancel_error={exc}")
        return 1

    print(f"venue={result['venue']}")
    print(f"cancelled={result['cancelled']}")
    print(f"errors={result['errors']}")
    print(f"count={result['count']}")
    print(f"pending={result['pending']}")
    return 0


def cancel_all_orders() -> int:
    config = load_app_config()
    try:
        if resolve_spot_venue(config) == "alpaca":
            result = cancel_all_alpaca_orders(config)
        else:
            result = cancel_all_kraken_orders(config)
    except Exception as exc:
        print(f"cancel_all_error={exc}")
        return 1

    print(f"venue={result['venue']}")
    print(f"cancelled={result['cancelled']}")
    print(f"errors={result['errors']}")
    print(f"count={result['count']}")
    print(f"pending={result['pending']}")
    return 0


def _context_float(snapshot, key: str) -> float:
    value = snapshot.context.get(key, 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_mt5_size(size: float, snapshot) -> float:
    try:
        requested = float(size)
    except (TypeError, ValueError):
        return 0.0
    step = float(snapshot.order_step_size or snapshot.min_order_size or 0.01)
    minimum = float(snapshot.min_order_size or step or 0.01)
    maximum = float(snapshot.max_order_size or requested)
    if requested < minimum:
        requested = minimum
    if maximum > 0:
        requested = min(requested, maximum)
    if step <= 0:
        return requested
    units = round(requested / step)
    normalized = units * step
    precision = max(snapshot.size_precision, 2)
    return round(normalized, precision)


def _refresh_mt5_runtime_state(config, snapshot):
    account = get_mt5_account(config)
    positions = get_mt5_positions(config)
    open_orders = get_mt5_open_orders(config)
    portfolio = build_mt5_portfolio(snapshot, account, positions)
    balances = {
        "balance": float(account.get("balance") or 0.0),
        "equity": float(account.get("equity") or 0.0),
    }
    return account, positions, open_orders, portfolio, balances


def _log_mt5_layers(snapshot, positions: list[dict[str, object]]) -> None:
    for line in layer_status_lines(snapshot, positions):
        print(f"[mt5-layers] {line}")


def _prepare_mt5_layered_order(config, snapshot, signal, order, positions: list[dict[str, object]]):
    decision = decide_mt5_layer_entry(
        config.mt5_layers,
        snapshot,
        signal.side,
        positions,
        fallback_size=order.size,
    )
    if not decision.allowed:
        return None, decision

    trade_profile = str(signal.features.get("trade_profile") or "core").lower()
    opportunistic_cap = max(int(getattr(config.mt5_strategy, "opportunistic_max_layers_per_side", 0) or 0), 0)
    if trade_profile == "oportunista" and opportunistic_cap > 0 and decision.same_side_layers >= opportunistic_cap:
        decision.allowed = False
        decision.reason = (
            f"Modo oportunista solo permite {opportunistic_cap} capa(s) por lado fuera de ventana."
        )
        decision.requested_size = 0.0
        return None, decision

    normalized_size = _normalize_mt5_size(
        decision.requested_size if decision.requested_size > 0 else order.size,
        snapshot,
    )
    if normalized_size <= 0:
        decision.allowed = False
        decision.reason = "El tamano de la nueva capa quedo por debajo del minimo del broker."
        return None, decision

    order.size = normalized_size
    order.reason = (
        f"{order.reason} | layer={decision.next_layer_index} "
        f"same_side={decision.same_side_layers} opposite={decision.opposite_side_layers}"
    )
    return order, decision


def _close_mt5_opposite_layers(config, signal, positions: list[dict[str, object]]) -> list[dict[str, object]]:
    closed: list[dict[str, object]] = []
    for raw_position in positions_for_opposite_close(signal.side, positions):
        result = close_mt5_position(config, raw_position)
        closed.append(result)
    return closed


def _administer_mt5_layers(config, snapshot, positions: list[dict[str, object]]) -> int:
    adjustments = build_mt5_layer_adjustments(config.mt5_layers, config.mt5_strategy, snapshot, positions)
    updates = 0
    for adjustment in adjustments:
        result = update_mt5_position_risk(
            config,
            ticket=adjustment.ticket,
            stop_loss=adjustment.stop_loss,
            take_profit=adjustment.take_profit,
        )
        print(
            f"[mt5-layer-admin] ticket={adjustment.ticket} side={adjustment.side.value} "
            f"sl={adjustment.stop_loss:.3f} tp={adjustment.take_profit:.3f} "
            f"retcode={result['retcode']} reason={adjustment.reason}"
        )
        updates += 1
    return updates


def find_candidate_order(config, source: str, use_live_portfolio: bool = False):
    market_data, strategy = build_runtime_stack(config, source=source)
    risk = RiskEngine(config.bot)
    snapshots = market_data.get_snapshots()
    mt5_positions: list[dict[str, object]] = []
    if source == "mt5" and snapshots:
        account, mt5_positions, _, portfolio, _ = _refresh_mt5_runtime_state(config, snapshots[0])
    elif source == "btcusd" and snapshots and resolve_spot_venue(config) == "alpaca" and (alpaca_paper_available(config) or use_live_portfolio):
        account = get_alpaca_account(config)
        positions = get_alpaca_positions(config)
        portfolio = build_alpaca_btcusd_portfolio(snapshots[0], account, positions)
    elif use_live_portfolio and source == "btcusd" and snapshots:
        balances = get_kraken_balances(config)
        portfolio = build_live_btcusd_portfolio(snapshots[0], balances)
    else:
        portfolio, _ = build_portfolio_for_source(config, source)

    for snapshot in snapshots:
        signal = strategy.evaluate(snapshot)
        if signal is None:
            continue
        order = risk.propose_order(signal, snapshot, portfolio)
        if order is None:
            continue
        if source == "mt5":
            order, layer_decision = _prepare_mt5_layered_order(config, snapshot, signal, order, mt5_positions)
            if order is None:
                continue
        return snapshot, signal, order
    return None


def readiness_report() -> int:
    config = load_app_config()
    storage = build_storage(config.storage)
    try:
        readiness = evaluate_readiness(config, storage)
    finally:
        storage.close()

    print(f"verdict={readiness.verdict}")
    print(
        "status_operativa="
        f"{readiness.operational_status} "
        f"status_edge={readiness.edge_status} "
        f"status_live={readiness.live_status}"
    )
    print(f"summary={readiness.summary}")
    print(
        "metrics "
        f"total_runs={readiness.metrics.total_runs} "
        f"real_runs={readiness.metrics.real_runs} "
        f"real_fills={readiness.metrics.real_fills} "
        f"positive_real_run_rate={readiness.metrics.positive_real_run_rate:.3f} "
        f"max_run_drawdown={readiness.metrics.max_run_drawdown:.2f} "
        f"zero_fill_streak={readiness.metrics.zero_fill_streak}"
    )
    for gate in readiness.gates:
        print(f"[{gate.status}] {gate.category}:{gate.name} -> {gate.detail}")
    return 0


def gold_backtest(
    *,
    preset: str,
    balance: float,
    lot_size: float,
    spread: float,
    data_paths: list[str] | None = None,
) -> int:
    selected = XAU_PRESETS[preset]
    config = XauScalpConfig(
        session_start_utc=selected.session_start_utc,
        session_end_utc=selected.session_end_utc,
        fast_ema_period=selected.fast_ema_period,
        slow_ema_period=selected.slow_ema_period,
        pullback_ema_period=selected.pullback_ema_period,
        rsi_period=selected.rsi_period,
        rsi_threshold=selected.rsi_threshold,
        atr_period=selected.atr_period,
        take_profit_atr=selected.take_profit_atr,
        stop_loss_atr=selected.stop_loss_atr,
        spread=spread,
        lot_size=lot_size,
        contract_size=selected.contract_size,
        initial_balance=balance,
        session_windows_utc=selected.session_windows_utc,
    )
    resolved_paths = [Path(path) for path in (data_paths or [])]
    if not resolved_paths:
        resolved_paths = default_gold_backtest_paths(PROJECT_ROOT)
    if not resolved_paths:
        print("gold_backtest_error=No encontre archivos Dukascopy de XAUUSD en download/.")
        return 1

    print("strategy=xauusd_m1_pullback_m5_trend")
    print(f"preset={preset}")
    print(
        "session_utc="
        f"{describe_session_windows_utc(config.session_windows_utc, config.session_start_utc, config.session_end_utc)}"
    )
    print(f"rsi_threshold={config.rsi_threshold:.2f}")
    print(f"tp_atr={config.take_profit_atr:.2f}")
    print(f"sl_atr={config.stop_loss_atr:.2f}")
    print(f"spread={config.spread:.2f}")
    print(f"lot_size={config.lot_size:.3f}")
    print(f"starting_balance={config.initial_balance:.2f}")

    combined_bars = []
    for path in resolved_paths:
        bars = load_dukascopy_bars(path)
        combined_bars.extend(bars)
        report = run_pullback_backtest(bars, config)
        stats = report.stats
        print(
            "[period] "
            f"label={path.stem} trades={stats.trades} wins={stats.wins} losses={stats.losses} "
            f"win_rate={stats.win_rate:.2f}% pf={stats.profit_factor:.2f} "
            f"net_pnl={stats.net_pnl:.2f} max_dd={stats.max_drawdown:.2f} "
            f"ending_balance={stats.ending_balance:.2f}"
        )

    combined_bars.sort(key=lambda bar: bar.timestamp)
    combined = run_pullback_backtest(combined_bars, config)
    stats = combined.stats
    print(
        "[combined] "
        f"trades={stats.trades} wins={stats.wins} losses={stats.losses} "
        f"win_rate={stats.win_rate:.2f}% pf={stats.profit_factor:.2f} "
        f"net_pnl={stats.net_pnl:.2f} max_dd={stats.max_drawdown:.2f} "
        f"expectancy={stats.expectancy:.2f} ending_balance={stats.ending_balance:.2f}"
    )
    write_mt5_benchmark(
        GOLD_BENCHMARK_PATH,
        preset=preset,
        win_rate=stats.win_rate,
        profit_factor=stats.profit_factor,
        net_pnl=stats.net_pnl,
        trades=stats.trades,
    )
    if stats.win_rate >= 65.0 and stats.profit_factor >= 1.0 and stats.net_pnl > 0:
        print("verdict=GO_DEMO")
    elif stats.win_rate >= 65.0:
        print("verdict=WIN_RATE_OK_BUT_EXPECTANCY_FAIL")
    else:
        print("verdict=NO_GO")
    return 0


def mt5_gold_backtest(
    *,
    balance: float,
    spread: float,
    data_paths: list[str] | None = None,
) -> int:
    config = load_app_config()
    resolved_paths = [Path(path) for path in (data_paths or [])]
    if not resolved_paths:
        resolved_paths = default_mt5_gold_backtest_paths(PROJECT_ROOT)
    if not resolved_paths:
        print("mt5_backtest_error=No encontre archivos Dukascopy de XAUUSD en download/.")
        return 1

    bars = load_bars_for_paths(resolved_paths)
    report = run_mt5_xau_backtest(
        bars,
        mt5_config=config.mt5,
        strategy_config=config.mt5_strategy,
        layer_config=config.mt5_layers,
        balance=balance,
        spread=spread,
    )
    stats = report.stats
    print("strategy=mt5_xau_scalp_mixed_layered")
    print(f"symbol={config.mt5.symbol}")
    print(f"timeframe={config.mt5_strategy.entry_timeframe}/{config.mt5_strategy.filter_timeframe}")
    print(f"trading_mode={config.mt5_strategy.trading_mode}")
    print(
        "session_utc="
        f"{describe_session_windows_utc(config.mt5_strategy.session_windows_utc, config.mt5_strategy.session_start_utc, config.mt5_strategy.session_end_utc)}"
    )
    print(f"rsi_threshold={config.mt5_strategy.rsi_threshold:.2f}")
    print(f"tp_atr={config.mt5_strategy.take_profit_atr:.2f}")
    print(f"sl_atr={config.mt5_strategy.stop_loss_atr:.2f}")
    print(f"min_confidence={config.mt5_strategy.min_confidence:.2f}")
    print(f"core_reclaim_points={config.mt5_strategy.core_reclaim_points:.2f}")
    print(f"opportunistic_min_confidence={config.mt5_strategy.opportunistic_min_confidence:.2f}")
    print(f"spread={spread:.2f}")
    print(f"starting_balance={balance:.2f}")
    print(f"data_points={len(bars)}")
    print(
        "[combined] "
        f"trades={stats.trades} wins={stats.wins} losses={stats.losses} "
        f"win_rate={stats.win_rate:.2f}% pf={stats.profit_factor:.2f} "
        f"net_pnl={stats.net_pnl:.2f} max_dd={stats.max_drawdown:.2f} "
        f"expectancy={stats.expectancy:.2f} ending_balance={stats.ending_balance:.2f}"
    )
    print(
        "[profiles] "
        f"core_trades={stats.core_trades} core_win_rate={stats.core_win_rate:.2f}% "
        f"opportunistic_trades={stats.opportunistic_trades} opportunistic_win_rate={stats.opportunistic_win_rate:.2f}%"
    )
    print(
        "[layers] "
        f"max_open={stats.max_open_layers} max_long={stats.max_long_layers} "
        f"max_short={stats.max_short_layers} adjustments={stats.layer_adjustments} flip_closes={stats.flip_closes}"
    )
    print(
        "[trade_shape] "
        f"avg_win={stats.avg_win:.2f} avg_loss={stats.avg_loss:.2f} payoff_ratio={stats.payoff_ratio:.2f}"
    )
    write_mt5_benchmark(
        GOLD_BENCHMARK_PATH,
        preset="mt5-mixed-layered",
        win_rate=stats.win_rate,
        profit_factor=stats.profit_factor,
        net_pnl=stats.net_pnl,
        trades=stats.trades,
    )
    if stats.win_rate >= 80.0 and stats.profit_factor >= 1.0 and stats.net_pnl > 0:
        print("verdict=GO_DEMO_80")
    elif stats.win_rate >= 80.0:
        print("verdict=WIN_RATE_80_BUT_EXPECTANCY_FAIL")
    elif stats.win_rate >= 70.0 and stats.profit_factor >= 1.0 and stats.net_pnl > 0:
        print("verdict=GO_DEMO")
    else:
        print("verdict=NO_GO")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trading bot professional scaffold")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run_once_parser = subcommands.add_parser("run-once", help="Run one paper-trading cycle")
    run_once_parser.add_argument("--source", choices=["mock", "real", "btcusd", "mt5"], default=None)
    run_once_parser.add_argument("--live", action="store_true")
    backtest_parser = subcommands.add_parser("backtest", help="Run a simple local backtest")
    backtest_parser.add_argument("--source", choices=["mock", "real", "btcusd", "mt5"], default=None)
    gold_backtest_parser = subcommands.add_parser("gold-backtest", help="Backtest a simple XAUUSD scalping setup")
    gold_backtest_parser.add_argument("--preset", choices=sorted(XAU_PRESETS.keys()), default="high-win")
    gold_backtest_parser.add_argument("--balance", type=float, default=100.0)
    gold_backtest_parser.add_argument("--lot-size", type=float, default=0.01)
    gold_backtest_parser.add_argument("--spread", type=float, default=0.25)
    gold_backtest_parser.add_argument("--data", nargs="*", default=None)
    mt5_gold_backtest_parser = subcommands.add_parser("mt5-gold-backtest", help="Backtest the MT5 XAUUSD mixed-mode layered flow")
    mt5_gold_backtest_parser.add_argument("--balance", type=float, default=100.0)
    mt5_gold_backtest_parser.add_argument("--spread", type=float, default=0.25)
    mt5_gold_backtest_parser.add_argument("--data", nargs="*", default=None)
    report_parser = subcommands.add_parser("report", help="Inspect stored runs, signals, and fills")
    report_subcommands = report_parser.add_subparsers(dest="report_command", required=True)

    report_runs_parser = report_subcommands.add_parser("runs", help="Show recent runs")
    report_runs_parser.add_argument("--limit", type=int, default=10)

    report_signals_parser = report_subcommands.add_parser("signals", help="Show recent signals")
    report_signals_parser.add_argument("--limit", type=int, default=10)
    report_signals_parser.add_argument("--run-id", type=int, default=None)

    report_fills_parser = report_subcommands.add_parser("fills", help="Show recent fills")
    report_fills_parser.add_argument("--limit", type=int, default=10)
    report_fills_parser.add_argument("--run-id", type=int, default=None)

    dashboard_parser = subcommands.add_parser("dashboard", help="Build a local HTML dashboard")
    dashboard_parser.add_argument("--limit", type=int, default=20)
    dashboard_parser.add_argument("--output", default="var/dashboard.html")
    dashboard_parser.add_argument("--open-browser", action="store_true")
    subcommands.add_parser("readiness", help="Show operational go/no-go status")

    live_check_parser = subcommands.add_parser("live-check", help="Check live trading prerequisites")
    live_check_parser.add_argument("--venue", choices=["polymarket", "kraken", "alpaca", "mt5"], default="kraken")
    subcommands.add_parser("mt5-session-status", help="Show MT5 XAU session, setup, layers, and benchmark status")

    alpaca_connect_parser = subcommands.add_parser("alpaca-connect", help="Open the Alpaca onboarding bridge")
    alpaca_connect_parser.add_argument("--open-browser", action="store_true")

    mt5_connect_parser = subcommands.add_parser("mt5-connect", help="Prepare the MetaTrader 5 bridge")
    mt5_connect_parser.add_argument("--open-browser", action="store_true")

    preview_parser = subcommands.add_parser("preview-order", help="Preview the next order without executing")
    preview_parser.add_argument("--source", choices=["mock", "real", "btcusd", "mt5"], default="btcusd")
    preview_parser.add_argument("--validate-live", action="store_true")

    kill_switch_parser = subcommands.add_parser("kill-switch", help="Manage the live-trading kill switch")
    kill_switch_parser.add_argument("action", choices=["status", "on", "off"])

    dead_man_parser = subcommands.add_parser("dead-man-switch", help="Arm Kraken's dead-man switch")
    dead_man_parser.add_argument("--seconds", type=int, default=None)

    portfolio_parser = subcommands.add_parser("portfolio", help="Inspect or reset the persisted paper portfolio")
    portfolio_parser.add_argument("action", choices=["show", "reset"])
    portfolio_parser.add_argument("--source", choices=["mock", "real", "btcusd", "mt5"], default="btcusd")

    subcommands.add_parser("kraken-balance", help="Show authenticated Kraken balances")
    subcommands.add_parser("kraken-open-orders", help="Show authenticated Kraken open orders")

    submit_parser = subcommands.add_parser("submit-order", help="Validate or submit the current candidate order")
    submit_parser.add_argument("--source", choices=["btcusd", "mt5"], default="btcusd")
    submit_parser.add_argument("--live", action="store_true")
    force_demo_parser = subcommands.add_parser("force-demo-order", help="Send a manual MT5 demo order immediately")
    force_demo_parser.add_argument("--side", choices=["buy", "sell"], required=True)
    force_demo_parser.add_argument("--size", type=float, default=None)
    force_demo_parser.add_argument("--tp-atr", type=float, default=None)
    force_demo_parser.add_argument("--sl-atr", type=float, default=None)

    cancel_parser = subcommands.add_parser("cancel-order", help="Cancel a specific Kraken order by txid")
    cancel_parser.add_argument("txid")

    subcommands.add_parser("cancel-all-orders", help="Cancel all open Kraken orders")

    operator_parser = subcommands.add_parser("operator-panel", help="Open the local operator cockpit")
    operator_parser.add_argument("--host", default="127.0.0.1")
    operator_parser.add_argument("--port", type=int, default=8787)
    operator_parser.add_argument("--source", choices=["mt5", "btcusd"], default="mt5")
    operator_parser.add_argument("--open-browser", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run-once":
        return run_once(source=args.source, live=args.live)
    if args.command == "backtest":
        return backtest(source=args.source)
    if args.command == "gold-backtest":
        return gold_backtest(
            preset=args.preset,
            balance=args.balance,
            lot_size=args.lot_size,
            spread=args.spread,
            data_paths=args.data,
        )
    if args.command == "mt5-gold-backtest":
        return mt5_gold_backtest(
            balance=args.balance,
            spread=args.spread,
            data_paths=args.data,
        )
    if args.command == "report":
        if args.report_command == "runs":
            return report_runs(limit=args.limit)
        if args.report_command == "signals":
            return report_signals(limit=args.limit, run_id=args.run_id)
        if args.report_command == "fills":
            return report_fills(limit=args.limit, run_id=args.run_id)
    if args.command == "dashboard":
        return build_dashboard(limit=args.limit, output=args.output, open_browser=args.open_browser)
    if args.command == "readiness":
        return readiness_report()
    if args.command == "live-check":
        return live_check(venue=args.venue)
    if args.command == "mt5-session-status":
        return mt5_session_status()
    if args.command == "alpaca-connect":
        return alpaca_connect(open_browser=args.open_browser)
    if args.command == "mt5-connect":
        return mt5_connect(open_browser=args.open_browser)
    if args.command == "preview-order":
        return preview_order(source=args.source, validate_live=args.validate_live)
    if args.command == "kill-switch":
        return kill_switch(action=args.action)
    if args.command == "dead-man-switch":
        return dead_man_switch(timeout_seconds=args.seconds)
    if args.command == "portfolio":
        return portfolio_command(action=args.action, source=args.source)
    if args.command == "kraken-balance":
        return kraken_balance()
    if args.command == "kraken-open-orders":
        return kraken_open_orders()
    if args.command == "submit-order":
        return submit_order(source=args.source, live=args.live)
    if args.command == "force-demo-order":
        return force_demo_order(
            side=args.side,
            size=args.size,
            take_profit_atr=args.tp_atr,
            stop_loss_atr=args.sl_atr,
        )
    if args.command == "cancel-order":
        return cancel_order(txid=args.txid)
    if args.command == "cancel-all-orders":
        return cancel_all_orders()
    if args.command == "operator-panel":
        from trading_bot.operator_panel import serve_operator_panel

        serve_operator_panel(host=args.host, port=args.port, open_browser=args.open_browser, source=args.source)
        return 0
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
