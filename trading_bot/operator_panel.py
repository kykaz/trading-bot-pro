from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from html import escape
import io
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from urllib.parse import parse_qs, urlparse
import webbrowser

from trading_bot.dashboard import short_market
from trading_bot.live import build_order_preview
from trading_bot.local_runtime import mask_secret, read_env_file, save_api_credentials, update_live_flags
from trading_bot.mt5_status import build_mt5_session_status
from trading_bot.portfolio_state import build_portfolio_state_path, load_portfolio
from trading_bot.readiness import evaluate_readiness
from trading_bot.storage import build_storage


@dataclass(slots=True)
class OperatorSnapshot:
    panel_source: str
    readiness_verdict: str
    readiness_summary: str
    readiness_operational: str
    readiness_edge: str
    readiness_live: str
    kill_switch: str
    live_mode: str
    live_enabled: bool
    live_dry_run: bool
    auth_status: str
    public_api_status: str
    kraken_pair: str
    credential_file: str
    api_key_masked: str
    api_secret_masked: str
    portfolio_cash: float
    portfolio_realized_pnl: float
    portfolio_positions: list[dict[str, object]]
    balances: list[tuple[str, float]]
    balances_error: str | None
    open_orders: list[dict[str, object]]
    open_orders_error: str | None
    candidate_preview: dict[str, object] | None
    recent_runs: list[dict[str, object]]
    recent_fills: list[dict[str, object]]
    mt5_session_state: str
    mt5_window_local: str
    mt5_next_event_local: str
    mt5_setup_detected: bool
    mt5_setup_reason: str
    mt5_buy_layers: int
    mt5_sell_layers: int
    mt5_live_win_rate: float | None
    mt5_live_trade_count: int
    mt5_benchmark_win_rate: float | None
    mt5_benchmark_profit_factor: float | None
    mt5_status_error: str | None


def serve_operator_panel(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False, source: str = "mt5") -> None:
    from trading_bot import main as bot_main

    class Handler(BaseHTTPRequestHandler):
        last_action = "Cabina lista."
        last_output = "El panel esta esperando tu siguiente accion."

        def do_GET(self) -> None:  # noqa: N802
            self._render()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode()
            form = parse_qs(payload)
            action = form.get("action", [""])[0]
            self.last_action, self.last_output = _run_action(bot_main, action, form, source)
            self._render()

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return None

        def _render(self) -> None:
            snapshot = build_operator_snapshot(source=source)
            document = build_operator_panel_html(snapshot, self.last_action, self.last_output)
            encoded = document.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"operator_panel={url}")
    print("Presiona Ctrl+C para cerrar la cabina.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("operator_panel=stopped")
    finally:
        server.server_close()


def build_operator_snapshot(source: str = "mt5") -> OperatorSnapshot:
    from trading_bot import main as bot_main

    config = bot_main.load_app_config()
    spot_venue = bot_main.resolve_spot_venue(config)
    panel_source = source if source in {"mt5", "btcusd"} else "mt5"
    env_path = (
        bot_main.MT5_ENV_PATH
        if panel_source == "mt5"
        else (bot_main.ALPACA_ENV_PATH if spot_venue == "alpaca" else bot_main.KRAKEN_ENV_PATH)
    )
    env_data = read_env_file(env_path)
    storage = build_storage(config.storage)
    try:
        readiness = evaluate_readiness(config, storage)
        run_rows = storage.fetch_recent_runs(limit=40)
        fill_rows = storage.fetch_recent_fills(limit=40)
    finally:
        storage.close()

    recent_runs = [
        row for row in run_rows
        if (row.get("data_source") == panel_source if panel_source == "mt5" else row.get("data_source") == "btcusd")
    ][:24]
    recent_fills = [
        row for row in fill_rows
        if (str(row.get("market_id") or "").startswith("mt5:") if panel_source == "mt5" else not str(row.get("market_id") or "").startswith("mt5:"))
    ][:32]

    mt5_status = build_mt5_session_status(
        config,
        timezone_name="America/Mexico_City",
        benchmark_path=bot_main.GOLD_BENCHMARK_PATH,
    )

    if panel_source == "mt5":
        try:
            snapshots = bot_main.build_market_data_source(config, source="mt5").get_snapshots()
            account = bot_main.get_mt5_account(config)
            positions = bot_main.get_mt5_positions(config)
            portfolio = (
                bot_main.build_mt5_portfolio(snapshots[0], account, positions)
                if snapshots
                else bot_main.Portfolio(cash=float(account.get("balance") or 0.0))
            )
            candidate = bot_main.find_candidate_order(config, "mt5", use_live_portfolio=True)
            balances = [
                ("balance", float(account.get("balance") or 0.0)),
                ("equity", float(account.get("equity") or 0.0)),
                ("margin_free", float(account.get("margin_free") or 0.0)),
                ("profit", float(account.get("profit") or 0.0)),
            ]
            balances_error = None
            raw_open_orders = bot_main.get_mt5_open_orders(config)
            open_orders = [
                {
                    "pair": str(row.get("symbol") or config.mt5.symbol),
                    "txid": str(row.get("ticket") or row.get("order") or ""),
                    "type": "buy" if int(row.get("type") or 0) == 0 else "sell",
                    "ordertype": str(row.get("state") or "pending"),
                    "price": str(row.get("price_open") or row.get("price_current") or ""),
                    "volume": str(row.get("volume_initial") or row.get("volume_current") or row.get("volume") or ""),
                }
                for row in raw_open_orders
            ]
            open_orders_error = None
            live_report = bot_main.check_live_stack(config, venue="mt5")
            auth_status = live_report.auth_status
            public_api_status = live_report.public_api_status
            live_mode = live_report.live_mode
            live_enabled = config.mt5.enable_demo_trading
            live_dry_run = False
            market_symbol = config.mt5.symbol
            api_key_masked = mask_secret(env_data.get(config.mt5.login_env))
            api_secret_masked = mask_secret(env_data.get(config.mt5.server_env))
        except Exception as exc:
            portfolio_path = build_portfolio_state_path(config.paper.portfolio_state_dir, "mt5")
            portfolio = load_portfolio(portfolio_path, config.bot.starting_cash)
            candidate = None
            balances = []
            balances_error = str(exc)
            open_orders = []
            open_orders_error = str(exc)
            live_report = bot_main.check_live_stack(config, venue="mt5")
            auth_status = live_report.auth_status
            public_api_status = live_report.public_api_status
            live_mode = live_report.live_mode
            live_enabled = config.mt5.enable_demo_trading
            live_dry_run = False
            market_symbol = config.mt5.symbol
            api_key_masked = mask_secret(env_data.get(config.mt5.login_env))
            api_secret_masked = mask_secret(env_data.get(config.mt5.server_env))
    else:
        portfolio_path = build_portfolio_state_path(config.paper.portfolio_state_dir, "btcusd")
        if spot_venue == "alpaca" and config.alpaca_paper.enabled:
            try:
                snapshots = bot_main.build_market_data_source(config, source="btcusd").get_snapshots()
                account = bot_main.get_alpaca_account(config)
                positions = bot_main.get_alpaca_positions(config)
                portfolio = (
                    bot_main.build_alpaca_btcusd_portfolio(snapshots[0], account, positions)
                    if snapshots
                    else load_portfolio(portfolio_path, config.bot.starting_cash)
                )
            except Exception:
                portfolio = load_portfolio(portfolio_path, config.bot.starting_cash)
        else:
            portfolio = load_portfolio(portfolio_path, config.bot.starting_cash)
        live_report = bot_main.get_spot_live_report(config)
        try:
            candidate = bot_main.find_candidate_order(config, "btcusd")
        except Exception:
            candidate = None
        balances = []
        balances_error = None
        try:
            balances = sorted(bot_main.get_spot_balances(config).items())
        except Exception as exc:
            balances_error = str(exc)
        open_orders = []
        open_orders_error = None
        try:
            open_orders = bot_main.get_spot_open_orders(config)
        except Exception as exc:
            open_orders_error = str(exc)
        auth_status = live_report.auth_status
        public_api_status = live_report.public_api_status
        live_mode = live_report.live_mode
        live_enabled = config.alpaca_paper.enabled if spot_venue == "alpaca" else config.kraken.enable_live_trading and config.kraken_live.enabled
        live_dry_run = False if spot_venue == "alpaca" else config.kraken_live.dry_run
        market_symbol = config.alpaca.symbol if spot_venue == "alpaca" else config.kraken.wsname
        api_key_masked = mask_secret(env_data.get(config.alpaca_paper.api_key_env if spot_venue == "alpaca" else "KRAKEN_API_KEY"))
        api_secret_masked = mask_secret(env_data.get(config.alpaca_paper.api_secret_env if spot_venue == "alpaca" else "KRAKEN_API_SECRET"))

    candidate_preview = None
    if candidate is not None:
        snapshot, signal, order = candidate
        candidate_preview = build_order_preview(config, snapshot, signal, order)

    return OperatorSnapshot(
        panel_source=panel_source,
        readiness_verdict=readiness.verdict,
        readiness_summary=readiness.summary,
        readiness_operational=readiness.operational_status,
        readiness_edge=readiness.edge_status,
        readiness_live=readiness.live_status,
        kill_switch=live_report.kill_switch_status,
        live_mode=live_mode,
        live_enabled=live_enabled,
        live_dry_run=live_dry_run,
        auth_status=auth_status,
        public_api_status=public_api_status,
        kraken_pair=market_symbol,
        credential_file=str(env_path),
        api_key_masked=api_key_masked,
        api_secret_masked=api_secret_masked,
        portfolio_cash=portfolio.cash,
        portfolio_realized_pnl=portfolio.realized_pnl,
        portfolio_positions=[
            {
                "market_id": market_id,
                "size": position.size,
                "average_price": position.average_price,
                "side": "buy" if position.size >= 0 else "sell",
            }
            for market_id, position in portfolio.positions.items()
        ],
        balances=balances,
        balances_error=balances_error,
        open_orders=open_orders,
        open_orders_error=open_orders_error,
        candidate_preview=candidate_preview,
        recent_runs=recent_runs,
        recent_fills=recent_fills,
        mt5_session_state=mt5_status.session_state,
        mt5_window_local=mt5_status.session_window_local,
        mt5_next_event_local=mt5_status.next_event_local,
        mt5_setup_detected=mt5_status.setup_detected,
        mt5_setup_reason=mt5_status.setup_reason,
        mt5_buy_layers=mt5_status.buy_layers,
        mt5_sell_layers=mt5_status.sell_layers,
        mt5_live_win_rate=mt5_status.live_win_rate.win_rate if mt5_status.live_win_rate else None,
        mt5_live_trade_count=mt5_status.live_win_rate.trades if mt5_status.live_win_rate else 0,
        mt5_benchmark_win_rate=mt5_status.benchmark.win_rate if mt5_status.benchmark else None,
        mt5_benchmark_profit_factor=mt5_status.benchmark.profit_factor if mt5_status.benchmark else None,
        mt5_status_error=mt5_status.error,
    )


def _run_action(bot_main, action: str, form: dict[str, list[str]], source: str) -> tuple[str, str]:
    config = bot_main.load_app_config()
    spot_venue = bot_main.resolve_spot_venue(config)
    if source == "mt5":
        actions = {
            "run_once_paper": ("Revisar setup", lambda: bot_main.run_once(source="mt5", live=False, panel_mode=True)),
            "run_once_live": ("Ejecutar ciclo demo", lambda: bot_main.run_once(source="mt5", live=True, panel_mode=True)),
            "preview": ("Ver orden candidata", lambda: bot_main.preview_order(source="mt5", validate_live=False)),
            "validate": ("Validar MT5", lambda: bot_main.preview_order(source="mt5", validate_live=True)),
            "live_check": ("Estado MT5", lambda: bot_main.live_check(venue="mt5")),
            "mt5_status": ("Radar XAU", lambda: bot_main.mt5_session_status()),
            "mt5_connect": ("Puente MT5", lambda: bot_main.mt5_connect(open_browser=True)),
            "kill_on": ("Kill switch ON", lambda: bot_main.kill_switch(action="on")),
            "kill_off": ("Kill switch OFF", lambda: bot_main.kill_switch(action="off")),
            "portfolio_show": ("Posiciones MT5", lambda: bot_main.portfolio_command(action="show", source="mt5")),
            "submit_validate": ("Validar envio", lambda: bot_main.submit_order(source="mt5", live=False)),
            "submit_live": ("Enviar orden demo", lambda: bot_main.submit_order(source="mt5", live=True)),
            "force_buy": ("Comprar demo ahora", lambda: bot_main.force_demo_order(side="buy")),
            "force_sell": ("Vender demo ahora", lambda: bot_main.force_demo_order(side="sell")),
        }
    else:
        actions = {
            "run_once_paper": ("Correr paper", lambda: bot_main.run_once(source="btcusd", live=False, panel_mode=True)),
            "run_once_live": ("Correr live", lambda: bot_main.run_once(source="btcusd", live=True, panel_mode=True)),
            "preview": ("Preview order", lambda: bot_main.preview_order(source="btcusd", validate_live=False)),
            "validate": ("Validar orden", lambda: bot_main.preview_order(source="btcusd", validate_live=True)),
            "live_check": ("Live check", lambda: bot_main.live_check(venue=spot_venue)),
            "alpaca_connect": ("Puente Alpaca", lambda: bot_main.alpaca_connect(open_browser=True)),
            "kill_on": ("Kill switch ON", lambda: bot_main.kill_switch(action="on")),
            "kill_off": ("Kill switch OFF", lambda: bot_main.kill_switch(action="off")),
            "portfolio_reset": ("Reset portfolio", lambda: bot_main.portfolio_command(action="reset", source="btcusd")),
            "balances": ("Refrescar balances", lambda: bot_main.kraken_balance()),
            "open_orders": ("Refrescar ordenes", lambda: bot_main.kraken_open_orders()),
            "submit_validate": ("Submit validate", lambda: bot_main.submit_order(source="btcusd", live=False)),
            "submit_live": ("Submit live", lambda: bot_main.submit_order(source="btcusd", live=True)),
            "cancel_all": ("Cancel all", lambda: bot_main.cancel_all_orders()),
            "dead_man": ("Dead-man switch", lambda: bot_main.dead_man_switch(timeout_seconds=None)),
            "arm_live": ("Armar live", lambda: _update_live_flags(bot_main, live_enabled=True, dry_run=False)),
            "safe_mode": ("Volver a seguro", lambda: _update_live_flags(bot_main, live_enabled=False, dry_run=True)),
            "save_credentials": ("Guardar credenciales", lambda: _save_credentials(bot_main, form, source="btcusd")),
        }
    chosen = actions.get(action)
    if chosen is None:
        return "Accion desconocida", f"No reconozco la accion `{action}`."

    title, callback = chosen
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            exit_code = callback()
    except Exception as exc:
        return title, f"error={exc}"

    output = buffer.getvalue().strip() or "(sin salida)"
    if exit_code not in (0, None):
        output = f"{output}\nexit_code={exit_code}"
    return title, output


def _save_credentials(bot_main, form: dict[str, list[str]], source: str = "btcusd") -> int:
    config = bot_main.load_app_config()
    spot_venue = bot_main.resolve_spot_venue(config)
    if source == "mt5":
        print("credentials_error=La cabina MT5 no edita credenciales desde aqui; usa .env.mt5.local o mt5-connect.")
        return 1
    env_path = bot_main.ALPACA_ENV_PATH if spot_venue == "alpaca" else bot_main.KRAKEN_ENV_PATH
    key_name = config.alpaca_paper.api_key_env if spot_venue == "alpaca" else "KRAKEN_API_KEY"
    secret_name = config.alpaca_paper.api_secret_env if spot_venue == "alpaca" else "KRAKEN_API_SECRET"
    api_key = form.get("api_key", [""])[0].strip()
    api_secret = form.get("api_secret", [""])[0].strip()
    if not api_key and not api_secret:
        print("credentials_error=No enviaste ningun valor nuevo.")
        return 1
    save_api_credentials(env_path, key_name, secret_name, api_key or None, api_secret or None)
    bot_main.load_app_config()
    env_data = read_env_file(env_path)
    print(f"credentials_saved={env_path}")
    print(f"{key_name}={mask_secret(env_data.get(key_name))}")
    print(f"{secret_name}={mask_secret(env_data.get(secret_name))}")
    return 0


def _update_live_flags(bot_main, *, live_enabled: bool, dry_run: bool) -> int:
    config = bot_main.load_app_config()
    if bot_main.resolve_spot_venue(config) == "alpaca":
        update_live_flags(
            bot_main.CONFIG_PATH,
            alpaca_paper_enabled=live_enabled,
        )
        print(f"config_updated={bot_main.CONFIG_PATH}")
        print(f"alpaca_paper.enabled={live_enabled}")
        return 0
    update_live_flags(
        bot_main.CONFIG_PATH,
        kraken_enable_live_trading=live_enabled,
        kraken_live_enabled=live_enabled,
        kraken_live_dry_run=dry_run,
    )
    print(f"config_updated={bot_main.CONFIG_PATH}")
    print(f"kraken.enable_live_trading={live_enabled}")
    print(f"kraken_live.enabled={live_enabled}")
    print(f"kraken_live.dry_run={dry_run}")
    return 0


def build_operator_panel_html(snapshot: OperatorSnapshot, last_action: str, last_output: str) -> str:
    return _build_operator_panel_html_v3(snapshot, last_action, last_output)

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cabina de Operador</title>
  <style>
    :root {{
      --bg: #061019;
      --bg2: #0b1622;
      --panel: rgba(11, 22, 34, 0.92);
      --panel2: rgba(16, 31, 46, 0.92);
      --line: rgba(129, 171, 201, 0.15);
      --text: #edf6fd;
      --muted: #88a8bd;
      --green: #22c55e;
      --cyan: #22d3ee;
      --amber: #f59e0b;
      --red: #ef4444;
      --shadow: 0 20px 60px rgba(0,0,0,0.35);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "Segoe UI", Arial, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(34,211,238,0.10), transparent 22%),
        radial-gradient(circle at 85% 10%, rgba(245,158,11,0.07), transparent 22%),
        linear-gradient(180deg, var(--bg), var(--bg2));
      min-height: 100vh;
    }}
    .shell {{
      width: min(1380px, calc(100% - 24px));
      margin: 18px auto 28px;
      display: grid;
      gap: 14px;
    }}
    .hero, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }}
    .hero {{
      padding: 22px;
    }}
    .eyebrow {{
      color: var(--cyan);
      text-transform: uppercase;
      letter-spacing: .14em;
      font-size: 12px;
    }}
    h1 {{
      margin: 10px 0 8px;
      font-size: clamp(30px, 5vw, 52px);
      line-height: .95;
      letter-spacing: -.05em;
    }}
    .sub {{
      color: var(--muted);
      max-width: 840px;
      line-height: 1.55;
      margin: 0;
    }}
    .top-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: 1.3fr .9fr;
      margin-top: 18px;
    }}
    .metrics {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(4, minmax(0,1fr));
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(14,29,43,0.98), rgba(9,21,33,0.92));
    }}
    .metric .label {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .12em;
    }}
    .metric .value {{
      margin-top: 10px;
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -.04em;
    }}
    .green {{ color: var(--green); }}
    .red {{ color: var(--red); }}
    .amber {{ color: var(--amber); }}
    .cyan {{ color: var(--cyan); }}
    .status-box {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: linear-gradient(180deg, rgba(12,28,42,.98), rgba(8,19,29,.92));
      display: grid;
      gap: 12px;
    }}
    .badge {{
      display: inline-flex;
      width: fit-content;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(34,211,238,0.10);
      color: var(--cyan);
      text-transform: uppercase;
      letter-spacing: .12em;
      font-size: 12px;
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap: 10px;
    }}
    .status-card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.02);
    }}
    .status-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 18px;
    }}
    .grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: 1fr 1fr;
    }}
    .panel-head {{
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }}
    .panel-head h2 {{
      margin: 0;
      font-size: 22px;
      letter-spacing: -.03em;
    }}
    .panel-head p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .panel-body {{
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .actions {{
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(3, minmax(0,1fr));
    }}
    form {{ margin: 0; }}
    button {{
      width: 100%;
      border: 1px solid rgba(34,211,238,0.18);
      border-radius: 14px;
      padding: 12px 14px;
      background: linear-gradient(180deg, rgba(16,31,46,.96), rgba(11,22,34,.94));
      color: var(--text);
      font-weight: 600;
      cursor: pointer;
    }}
    button:hover {{
      border-color: rgba(34,211,238,0.45);
      transform: translateY(-1px);
    }}
    .danger {{
      border-color: rgba(239,68,68,0.28);
    }}
    .danger:hover {{
      border-color: rgba(239,68,68,0.54);
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: var(--panel2);
    }}
    .meta {{
      display: flex;
      gap: 10px 16px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
      margin-top: 10px;
    }}
    .list {{
      display: grid;
      gap: 10px;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "Consolas", "Courier New", monospace;
      font-size: 12px;
      color: #d7e7f4;
    }}
    .small {{
      color: var(--muted);
      font-size: 12px;
    }}
    .field {{
      display: grid;
      gap: 6px;
      margin-top: 12px;
    }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background: rgba(5, 13, 20, 0.85);
      color: var(--text);
    }}
    @media (max-width: 1080px) {{
      .top-grid, .grid {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .actions {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .status-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 720px) {{
      .shell {{ width: min(100% - 14px, 1380px); }}
      .metrics, .actions {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Cabina de Operador</div>
      <h1>Aqui ya si hay botones, palancas y frenos.</h1>
      <p class="sub">Esta cabina local no sustituye el motor del bot: lo conduce. El lado izquierdo te deja operar; el derecho te deja entender si la mesa esta lista o bloqueada.</p>
      <div class="top-grid">
        <div class="metrics">
          <article class="metric">
            <div class="label">Readiness</div>
            <div class="value">{escape(snapshot.readiness_verdict)}</div>
          </article>
          <article class="metric">
            <div class="label">Modo live</div>
            <div class="value {'amber' if snapshot.live_mode == 'DRY_RUN' else 'green'}">{escape(snapshot.live_mode)}</div>
          </article>
          <article class="metric">
            <div class="label">Kill switch</div>
            <div class="value {'red' if snapshot.kill_switch == 'ACTIVO' else 'green'}">{escape(snapshot.kill_switch)}</div>
          </article>
          <article class="metric">
            <div class="label">Auth Kraken</div>
            <div class="value {'green' if snapshot.auth_status == 'AUTH_OK' else 'amber' if snapshot.auth_status != 'SIN_AUTH' else 'red'}">{escape(snapshot.auth_status)}</div>
          </article>
        </div>
        <aside class="status-box">
          <div class="badge">{escape(snapshot.readiness_verdict)}</div>
          <div>{escape(snapshot.readiness_summary)}</div>
          <div class="status-grid">
            {render_status_card("Operativa", snapshot.readiness_operational)}
            {render_status_card("Edge", snapshot.readiness_edge)}
            {render_status_card("Live", snapshot.readiness_live)}
          </div>
          <div class="small">API publica Kraken: {escape(snapshot.public_api_status)}</div>
        </aside>
      </div>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Armado Live</h2>
            <p>Estado de credenciales y seguro de live para Kraken.</p>
          </div>
        </div>
        <div class="panel-body">
          <div class="card">
            <div><strong>Par:</strong> {escape(snapshot.kraken_pair)}</div>
            <div class="meta">
              <span>Live enabled {'SI' if snapshot.live_enabled else 'NO'}</span>
              <span>Dry run {'SI' if snapshot.live_dry_run else 'NO'}</span>
            </div>
            <div class="meta">
              <span>Archivo {escape(snapshot.credential_file)}</span>
            </div>
          </div>
          <div class="card">
            <div><strong>KRAKEN_API_KEY:</strong> {escape(snapshot.api_key_masked)}</div>
            <div class="meta"><span><strong>KRAKEN_API_SECRET:</strong> {escape(snapshot.api_secret_masked)}</span></div>
          </div>
          <form method="post" action="/action" class="card">
            <div class="small">Guarda o reemplaza credenciales localmente. Nunca se muestran completas despues.</div>
            <div class="field">
              <label class="small" for="api_key">API key</label>
              <input id="api_key" name="api_key" type="text" placeholder="KRAKEN_API_KEY">
            </div>
            <div class="field">
              <label class="small" for="api_secret">API secret</label>
              <input id="api_secret" name="api_secret" type="password" placeholder="KRAKEN_API_SECRET">
            </div>
            <div class="actions">
              <button type="submit" name="action" value="save_credentials">Guardar Credenciales</button>
              <button type="submit" name="action" value="arm_live" class="danger">Armar Live</button>
              <button type="submit" name="action" value="safe_mode">Volver a Seguro</button>
            </div>
          </form>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Controles</h2>
            <p>Los mandos principales de la mesa, ordenados por frecuencia real de uso.</p>
          </div>
        </div>
        <div class="panel-body">
          <div class="actions">
            {action_button("run_once_paper", "Run Once Paper")}
            {action_button("preview", "Preview Order")}
            {action_button("validate", "Validar Orden")}
            {action_button("live_check", "Live Check")}
            {action_button("balances", "Balances Kraken")}
            {action_button("open_orders", "Open Orders")}
            {action_button("kill_on", "Kill Switch ON", danger=True)}
            {action_button("kill_off", "Kill Switch OFF")}
            {action_button("dead_man", "Dead-Man Switch")}
            {action_button("submit_validate", "Submit Validate")}
            {action_button("submit_live", "Submit Live", danger=True)}
            {action_button("run_once_live", "Run Once Live", danger=True)}
            {action_button("cancel_all", "Cancel All", danger=True)}
            {action_button("portfolio_reset", "Reset Portfolio", danger=True)}
          </div>
        </div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Consola</h2>
            <p>Lo ultimo que ejecutaste desde la cabina, con salida textual completa.</p>
          </div>
          <span class="badge">{escape(last_action)}</span>
        </div>
        <div class="panel-body">
          <div class="card">
            <pre>{escape(last_output)}</pre>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Orden Candidata</h2>
            <p>Lo que el bot haria ahora mismo si le dieras permiso.</p>
          </div>
        </div>
        <div class="panel-body">
          {render_candidate(snapshot.candidate_preview)}
        </div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Portfolio</h2>
            <p>Tu inventario paper actual en BTC/USD.</p>
          </div>
        </div>
        <div class="panel-body">
          <div class="card">
            <div><strong>Cash:</strong> {snapshot.portfolio_cash:.2f}</div>
            <div class="meta">
              <span>Realized PnL {snapshot.portfolio_realized_pnl:.2f}</span>
              <span>Posiciones {len(snapshot.portfolio_positions)}</span>
            </div>
          </div>
          <div class="list">
            {render_positions(snapshot.portfolio_positions)}
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Cuenta Kraken</h2>
            <p>Balances autenticados cuando haya credenciales.</p>
          </div>
        </div>
        <div class="panel-body">
          {render_balances(snapshot.balances, snapshot.balances_error)}
        </div>
      </section>
    </section>

    <section class="grid">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Ordenes Abiertas</h2>
            <p>Lo que ya quedo colgado en Kraken para que no dupliques riesgo.</p>
          </div>
        </div>
        <div class="panel-body">
          {render_open_orders(snapshot.open_orders, snapshot.open_orders_error)}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Corridas y Fills</h2>
            <p>Memoria operativa corta para leer el comportamiento reciente.</p>
          </div>
        </div>
        <div class="panel-body list">
          {render_runs(snapshot.recent_runs)}
          {render_fills(snapshot.recent_fills)}
        </div>
      </section>
    </section>
  </main>
</body>
</html>"""


def _build_operator_panel_html_v2(snapshot: OperatorSnapshot, last_action: str, last_output: str) -> str:
    verdict_tone = {
        "PAPER OPERATIVO": "cyan",
        "NO GO": "red",
        "LISTO PARA LIVE": "green",
    }.get(snapshot.readiness_verdict, "amber")
    live_tone = "amber" if snapshot.live_mode == "DRY_RUN" else "green"
    kill_tone = "red" if snapshot.kill_switch == "ACTIVO" else "green"
    auth_tone = "green" if snapshot.auth_status == "AUTH_OK" else "red" if snapshot.auth_status == "SIN_AUTH" else "amber"
    realized_tone = "green" if snapshot.portfolio_realized_pnl >= 0 else "red"
    using_alpaca = snapshot.live_mode.startswith("PAPER_BROKER")
    venue_label = "Alpaca Paper" if using_alpaca else "Kraken Spot"
    auth_label = "Auth Alpaca" if using_alpaca else "Auth Kraken"
    account_label = "Cuenta Alpaca" if using_alpaca else "Cuenta Kraken"
    orders_label = "Ordenes Broker" if using_alpaca else "Ordenes Abiertas"
    orders_subtitle = (
        "Riesgo ya vivo en el broker paper antes de mandar otra cosa."
        if using_alpaca
        else "Riesgo ya vivo en la cuenta antes de mandar otra cosa."
    )
    api_key_placeholder = "APCA_API_KEY_ID" if using_alpaca else "KRAKEN_API_KEY"
    api_secret_placeholder = "APCA_API_SECRET_KEY" if using_alpaca else "KRAKEN_API_SECRET"
    balances_button = "Cuenta Alpaca" if using_alpaca else "Balances Kraken"
    onboarding_note = (
        "El alta y la generacion de claves siguen siendo manuales. Usa el puente Alpaca y vuelve aqui para pegarlas."
        if using_alpaca and snapshot.auth_status == "SIN_AUTH"
        else "Las claves viven solo en local; esta cabina nunca las publica ni las expone completas."
    )
    onboarding_actions = (
        f'<div class="control-group"><div class="control-label">Onboarding</div><div class="actions">'
        f'{action_button("alpaca_connect", "Abrir Puente Alpaca")}'
        f'{action_button("live_check", "Revisar Auth Alpaca")}'
        "</div></div>"
        if using_alpaca and snapshot.auth_status == "SIN_AUTH"
        else ""
    )
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cabina de Operador</title>
  <style>{_operator_panel_styles()}</style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="topline">
        <div>
          <div class="kicker">Cabina de Operador</div>
          <h1>Mesa de ejecucion local</h1>
          <p class="lede">Arriba tienes el semaforo maestro, al centro el ticket y el Radar XAU, y a la derecha el inventario y la cuenta. El objetivo es leer la operativa en segundos.</p>
        </div>
        <div class="market-strip">
          <span class="tag">Par <strong>{escape(snapshot.kraken_pair)}</strong></span>
          <span class="tag">Venue <strong>{venue_label}</strong></span>
          <span class="tag">API <strong>{escape(snapshot.public_api_status)}</strong></span>
        </div>
      </div>
      <section class="status-ribbon">
        <article class="ribbon-main">
          <h2>Semaforo maestro</h2>
          <p>{escape(snapshot.readiness_summary)}</p>
          <div class="mini-grid">
            <div class="mini-cell"><span>Operativa</span><strong class="tone-{_status_tone(snapshot.readiness_operational)}">{escape(snapshot.readiness_operational)}</strong></div>
            <div class="mini-cell"><span>Edge</span><strong class="tone-{_status_tone(snapshot.readiness_edge)}">{escape(snapshot.readiness_edge)}</strong></div>
            <div class="mini-cell"><span>Live</span><strong class="tone-{_status_tone(snapshot.readiness_live)}">{escape(snapshot.readiness_live)}</strong></div>
          </div>
        </article>
        <article class="ribbon-box"><div class="ribbon-label">Readiness</div><div class="ribbon-value tone-{verdict_tone}">{escape(snapshot.readiness_verdict)}</div><div class="subtle">Dictamen general.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">Modo live</div><div class="ribbon-value tone-{live_tone}">{escape(snapshot.live_mode)}</div><div class="subtle">Seguro principal.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">Kill switch</div><div class="ribbon-value tone-{kill_tone}">{escape(snapshot.kill_switch)}</div><div class="subtle">Freno duro del executor.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">{auth_label}</div><div class="ribbon-value tone-{auth_tone}">{escape(snapshot.auth_status)}</div><div class="subtle">Estado de credenciales.</div></article>
      </section>
    </section>

    <section class="desk">
      <aside class="rail left-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>Armado Live</h2><p>Claves, flags y seguro de fuego real.</p></div>
            <span class="panel-pill {'alert' if not snapshot.live_enabled else 'live'}">estado<strong>{'seguro' if not snapshot.live_enabled else 'armado'}</strong></span>
          </div>
          <div class="panel-body">
            <div class="desk-note">
              <strong>Stack</strong>
              <div class="value">Live enabled {'SI' if snapshot.live_enabled else 'NO'} · Dry run {'SI' if snapshot.live_dry_run else 'NO'}</div>
              <div class="code-path">{escape(snapshot.credential_file)}</div>
            </div>
            <div class="desk-note">
              <strong>Credenciales</strong>
              <div class="subtle">API key {escape(snapshot.api_key_masked)}</div>
              <div class="subtle">API secret {escape(snapshot.api_secret_masked)}</div>
            </div>
            <div class="desk-note">
              <strong>Puente</strong>
              <div class="subtle">{escape(onboarding_note)}</div>
            </div>
            <form method="post" action="/action" class="fields">
              <div class="field"><label for="api_key">API key</label><input id="api_key" name="api_key" type="text" placeholder="{api_key_placeholder}"></div>
              <div class="field"><label for="api_secret">API secret</label><input id="api_secret" name="api_secret" type="password" placeholder="{api_secret_placeholder}"></div>
              <div class="actions stack">
                <button type="submit" name="action" value="save_credentials">Guardar Credenciales</button>
                <button type="submit" name="action" value="arm_live" class="danger">Armar Live</button>
                <button type="submit" name="action" value="safe_mode">Volver a Seguro</button>
              </div>
            </form>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Mandos</h2><p>Organizados por ciclo, cuenta, riesgo y fuego real.</p></div>
          </div>
          <div class="panel-body">
            {onboarding_actions}
            <div class="control-group"><div class="control-label">Ciclo</div><div class="actions">{action_button("run_once_paper", "Run Once Paper")}{action_button("preview", "Preview Order")}{action_button("validate", "Validar Orden")}{action_button("live_check", "Live Check")}</div></div>
            <div class="control-group"><div class="control-label">Cuenta</div><div class="actions">{action_button("balances", balances_button)}{action_button("open_orders", "Open Orders")}{action_button("dead_man", "Dead-Man Switch")}{action_button("submit_validate", "Submit Validate")}</div></div>
            <div class="control-group"><div class="control-label">Riesgo</div><div class="actions">{action_button("kill_on", "Kill Switch ON", danger=True)}{action_button("kill_off", "Kill Switch OFF")}{action_button("cancel_all", "Cancel All", danger=True)}{action_button("portfolio_reset", "Reset Portfolio", danger=True)}</div></div>
            <div class="control-group"><div class="control-label">Fuego real</div><div class="actions">{action_button("run_once_live", "Run Once Live", danger=True)}{action_button("submit_live", "Submit Live", danger=True)}</div></div>
          </div>
        </section>
      </aside>

      <section class="rail center-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>Ticket de Operacion</h2><p>Lo que el motor quiere mandar ahora mismo.</p></div>
            <span class="panel-pill">par<strong>{escape(snapshot.kraken_pair)}</strong></span>
          </div>
          <div class="panel-body">{render_candidate(snapshot.candidate_preview)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Radar XAU</h2><p>Ventana operativa, setup actual, capas abiertas y benchmark activo.</p></div>
            <span class="panel-pill {'live' if snapshot.mt5_session_state == 'ACTIVA' else 'alert'}">sesion<strong>{escape(snapshot.mt5_session_state)}</strong></span>
          </div>
          <div class="panel-body">{render_mt5_radar(snapshot)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Consola</h2><p>Salida textual exacta de la ultima accion disparada.</p></div>
            <span class="panel-pill alert">ultima<strong>{escape(last_action)}</strong></span>
          </div>
          <div class="panel-body">
            <div class="card console">
              <div class="console-bar"><span class="dot red"></span><span class="dot amber"></span><span class="dot green"></span><span class="row-mono">{escape(last_action)}</span></div>
              <pre>{escape(last_output)}</pre>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Tape Operativo</h2><p>Corridas y fills recientes para leer el pulso del sistema.</p></div>
          </div>
          <div class="panel-body">
            <div class="tape-grid">
              <section class="tape-col"><h3>Corridas recientes</h3>{render_runs(snapshot.recent_runs)}</section>
              <section class="tape-col"><h3>Fills recientes</h3>{render_fills(snapshot.recent_fills)}</section>
            </div>
          </div>
        </section>
      </section>

      <aside class="rail right-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>Inventario</h2><p>Estado local del libro paper en BTC/USD.</p></div>
          </div>
          <div class="panel-body">
            <div class="stats-bar">
              <div class="card"><span>Cash</span><strong>{snapshot.portfolio_cash:.2f}</strong></div>
              <div class="card"><span>Realized PnL</span><strong class="tone-{realized_tone}">{snapshot.portfolio_realized_pnl:.2f}</strong></div>
              <div class="card"><span>Posiciones</span><strong>{len(snapshot.portfolio_positions)}</strong></div>
            </div>
            <div class="list">{render_positions(snapshot.portfolio_positions)}</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>{account_label}</h2><p>Balances autenticados disponibles para sizing.</p></div>
          </div>
          <div class="panel-body list">{render_balances(snapshot.balances, snapshot.balances_error)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>{orders_label}</h2><p>{orders_subtitle}</p></div>
          </div>
          <div class="panel-body list">{render_open_orders(snapshot.open_orders, snapshot.open_orders_error)}</div>
        </section>
      </aside>
    </section>
  </main>
</body>
</html>"""


def _build_operator_panel_html_v3(snapshot: OperatorSnapshot, last_action: str, last_output: str) -> str:
    verdict_tone = {
        "PAPER OPERATIVO": "cyan",
        "NO GO": "red",
        "LISTO PARA LIVE": "green",
    }.get(snapshot.readiness_verdict, "amber")
    live_tone = "amber" if snapshot.live_mode == "DRY_RUN" else "green"
    kill_tone = "red" if snapshot.kill_switch == "ACTIVO" else "green"
    auth_tone = "green" if snapshot.auth_status == "AUTH_OK" else "red" if snapshot.auth_status == "SIN_AUTH" else "amber"
    realized_tone = "green" if snapshot.portfolio_realized_pnl >= 0 else "red"
    using_mt5 = snapshot.panel_source == "mt5"
    using_alpaca = snapshot.panel_source != "mt5" and snapshot.live_mode.startswith("PAPER_BROKER")
    venue_label = "MT5 Demo" if using_mt5 else ("Alpaca Paper" if using_alpaca else "Kraken Spot")
    auth_label = "Auth MT5" if using_mt5 else ("Auth Alpaca" if using_alpaca else "Auth Kraken")
    account_label = "Cuenta MT5" if using_mt5 else ("Cuenta Alpaca" if using_alpaca else "Cuenta Kraken")
    account_subtitle = (
        "Balance, equity, margen libre y PnL leidos del terminal demo."
        if using_mt5
        else "Balances autenticados disponibles para sizing."
    )
    orders_label = "Ordenes MT5" if using_mt5 else ("Ordenes Broker" if using_alpaca else "Ordenes Abiertas")
    orders_subtitle = (
        "Ordenes pendientes vivas en el terminal para no pisarte con otra capa."
        if using_mt5
        else (
            "Riesgo ya vivo en el broker paper antes de mandar otra cosa."
            if using_alpaca
            else "Riesgo ya vivo en la cuenta antes de mandar otra cosa."
        )
    )
    hero_title = "Mesa de ejecucion local para XAUUSD" if using_mt5 else "Mesa de ejecucion local"
    hero_lede = (
        "Arriba tienes el semaforo maestro; al centro el ticket, el Radar XAU y la consola; a la derecha las capas demo y el estado del terminal. Todo queda orientado a MT5 y oro."
        if using_mt5
        else "Arriba tienes el semaforo maestro, al centro el ticket y el Radar XAU, y a la derecha el inventario y la cuenta. El objetivo es leer la operativa en segundos."
    )
    left_title = "Puente MT5" if using_mt5 else "Armado Live"
    left_subtitle = (
        "Estado del terminal, credenciales locales y seguro de ejecucion demo."
        if using_mt5
        else "Claves, flags y seguro de fuego real."
    )
    left_state_label = "demo" if using_mt5 else ("armado" if snapshot.live_enabled else "seguro")
    stack_value = (
        f"Demo trading {'SI' if snapshot.live_enabled else 'NO'} - Fuente MT5"
        if using_mt5
        else f"Live enabled {'SI' if snapshot.live_enabled else 'NO'} - Dry run {'SI' if snapshot.live_dry_run else 'NO'}"
    )
    credentials_title = "Terminal" if using_mt5 else "Credenciales"
    credentials_line_1 = f"Login {escape(snapshot.api_key_masked)}" if using_mt5 else f"API key {escape(snapshot.api_key_masked)}"
    credentials_line_2 = f"Server {escape(snapshot.api_secret_masked)}" if using_mt5 else f"API secret {escape(snapshot.api_secret_masked)}"
    onboarding_note = (
        "El terminal demo vive en local. Si cambias de cuenta, actualiza .env.mt5.local o usa el puente MT5 para abrir instalacion y ayuda."
        if using_mt5
        else (
            "El alta y la generacion de claves siguen siendo manuales. Usa el puente Alpaca y vuelve aqui para pegarlas."
            if using_alpaca and snapshot.auth_status == "SIN_AUTH"
            else "Las claves viven solo en local; esta cabina nunca las publica ni las expone completas."
        )
    )
    onboarding_actions = (
        f'<div class="control-group"><div class="control-label">Puente</div><div class="actions">'
        f'{action_button("mt5_connect", "Abrir Puente MT5")}'
        f'{action_button("live_check", "Revisar MT5")}'
        "</div></div>"
        if using_mt5
        else (
            f'<div class="control-group"><div class="control-label">Onboarding</div><div class="actions">'
            f'{action_button("alpaca_connect", "Abrir Puente Alpaca")}'
            f'{action_button("live_check", "Revisar Auth Alpaca")}'
            "</div></div>"
            if using_alpaca and snapshot.auth_status == "SIN_AUTH"
            else ""
        )
    )
    controls_html = (
        f'<div class="control-group"><div class="control-label">Ciclo</div><div class="actions">'
        f'{action_button("run_once_paper", "Revisar Setup", tone="ghost", wide=True)}'
        f'{action_button("preview", "Ver Orden Candidata", tone="warning", wide=True)}'
        f'{action_button("validate", "Validar Orden", tone="ghost")}'
        f'{action_button("live_check", "Estado del Terminal", tone="ghost")}'
        "</div></div>"
        f'<div class="control-group"><div class="control-label">Mesa</div><div class="actions">'
        f'{action_button("portfolio_show", "Posiciones Abiertas", tone="ghost")}'
        f'{action_button("mt5_status", "Radar XAU", tone="ghost")}'
        f'{action_button("submit_validate", "Validar Envio", tone="warning")}'
        f'{action_button("force_buy", "Comprar Demo Ahora", tone="success", wide=True)}'
        f'{action_button("force_sell", "Vender Demo Ahora", danger=True, wide=True)}'
        "</div></div>"
        f'<div class="control-group"><div class="control-label">Riesgo</div><div class="actions">'
        f'{action_button("kill_on", "Kill Switch ON", danger=True, wide=True)}'
        f'{action_button("kill_off", "Kill Switch OFF", tone="ghost", wide=True)}'
        "</div></div>"
        f'<div class="control-group"><div class="control-label">Demo</div><div class="actions">'
        f'{action_button("submit_live", "Enviar Orden Demo", danger=True, wide=True)}'
        f'{action_button("run_once_live", "Ejecutar Ciclo Demo", tone="success", wide=True)}'
        "</div></div>"
        if using_mt5
        else (
            f'<div class="control-group"><div class="control-label">Ciclo</div><div class="actions">'
            f'{action_button("run_once_paper", "Run Once Paper", tone="ghost")}'
            f'{action_button("preview", "Preview Order", tone="warning")}'
            f'{action_button("validate", "Validar Orden", tone="ghost")}'
            f'{action_button("live_check", "Live Check", tone="ghost")}'
            "</div></div>"
            f'<div class="control-group"><div class="control-label">Cuenta</div><div class="actions">'
            f'{action_button("balances", "Cuenta Alpaca" if using_alpaca else "Balances Kraken", tone="ghost")}'
            f'{action_button("open_orders", "Open Orders", tone="ghost")}'
            f'{action_button("dead_man", "Dead-Man Switch", tone="warning")}'
            f'{action_button("submit_validate", "Submit Validate", tone="warning")}'
            "</div></div>"
            f'<div class="control-group"><div class="control-label">Riesgo</div><div class="actions">'
            f'{action_button("kill_on", "Kill Switch ON", danger=True, wide=True)}'
            f'{action_button("kill_off", "Kill Switch OFF", tone="ghost", wide=True)}'
            f'{action_button("cancel_all", "Cancel All", danger=True)}'
            f'{action_button("portfolio_reset", "Reset Portfolio", danger=True)}'
            "</div></div>"
            f'<div class="control-group"><div class="control-label">Fuego real</div><div class="actions">'
            f'{action_button("run_once_live", "Run Once Live", tone="success", wide=True)}'
            f'{action_button("submit_live", "Submit Live", danger=True, wide=True)}'
            "</div></div>"
        )
    )
    credentials_block = (
        '<div class="desk-note">'
        f"<strong>{credentials_title}</strong>"
        f'<div class="subtle">{credentials_line_1}</div>'
        f'<div class="subtle">{credentials_line_2}</div>'
        "</div>"
    )
    credential_form = (
        ""
        if using_mt5
        else (
            '<form method="post" action="/action" class="fields">'
            '<div class="field"><label for="api_key">API key</label>'
            f'<input id="api_key" name="api_key" type="text" placeholder={"APCA_API_KEY_ID" if using_alpaca else "KRAKEN_API_KEY"}></div>'
            '<div class="field"><label for="api_secret">API secret</label>'
            f'<input id="api_secret" name="api_secret" type="password" placeholder={"APCA_API_SECRET_KEY" if using_alpaca else "KRAKEN_API_SECRET"}></div>'
            '<div class="actions stack">'
            '<button type="submit" name="action" value="save_credentials">Guardar Credenciales</button>'
            '<button type="submit" name="action" value="arm_live" class="danger">Armar Live</button>'
            '<button type="submit" name="action" value="safe_mode">Volver a Seguro</button>'
            "</div>"
            "</form>"
        )
    )
    inventory_title = "Posiciones MT5" if using_mt5 else "Inventario"
    inventory_subtitle = "Capas y posiciones demo abiertas en XAUUSD." if using_mt5 else "Estado local del libro paper en BTC/USD."
    ticket_pill = "XAUUSD" if using_mt5 else escape(snapshot.kraken_pair)
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cabina de Operador</title>
  <style>{_operator_panel_styles()}</style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="topline">
        <div>
          <div class="kicker">Cabina de Operador</div>
          <h1>{hero_title}</h1>
          <p class="lede">{hero_lede}</p>
        </div>
        <div class="market-strip">
          <span class="tag">Par <strong>{escape(snapshot.kraken_pair)}</strong></span>
          <span class="tag">Venue <strong>{venue_label}</strong></span>
          <span class="tag">API <strong>{escape(snapshot.public_api_status)}</strong></span>
        </div>
      </div>
      <section class="status-ribbon">
        <article class="ribbon-main">
          <h2>Semaforo maestro</h2>
          <p>{escape(snapshot.readiness_summary)}</p>
          <div class="mini-grid">
            <div class="mini-cell"><span>Operativa</span><strong class="tone-{_status_tone(snapshot.readiness_operational)}">{escape(snapshot.readiness_operational)}</strong></div>
            <div class="mini-cell"><span>Edge</span><strong class="tone-{_status_tone(snapshot.readiness_edge)}">{escape(snapshot.readiness_edge)}</strong></div>
            <div class="mini-cell"><span>Live</span><strong class="tone-{_status_tone(snapshot.readiness_live)}">{escape(snapshot.readiness_live)}</strong></div>
          </div>
        </article>
        <article class="ribbon-box"><div class="ribbon-label">Readiness</div><div class="ribbon-value tone-{verdict_tone}">{escape(snapshot.readiness_verdict)}</div><div class="subtle">Dictamen general.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">Modo live</div><div class="ribbon-value tone-{live_tone}">{escape(snapshot.live_mode)}</div><div class="subtle">Seguro principal.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">Kill switch</div><div class="ribbon-value tone-{kill_tone}">{escape(snapshot.kill_switch)}</div><div class="subtle">Freno duro del executor.</div></article>
        <article class="ribbon-box"><div class="ribbon-label">{auth_label}</div><div class="ribbon-value tone-{auth_tone}">{escape(snapshot.auth_status)}</div><div class="subtle">Estado de credenciales.</div></article>
      </section>
    </section>

    <section class="desk">
      <aside class="rail left-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>{left_title}</h2><p>{left_subtitle}</p></div>
            <span class="panel-pill {'alert' if not snapshot.live_enabled else 'live'}">estado<strong>{left_state_label}</strong></span>
          </div>
          <div class="panel-body">
            <div class="desk-note">
              <strong>Stack</strong>
              <div class="value">{stack_value}</div>
              <div class="code-path">{escape(snapshot.credential_file)}</div>
            </div>
            {credentials_block}
            <div class="desk-note">
              <strong>Puente</strong>
              <div class="subtle">{escape(onboarding_note)}</div>
            </div>
            {credential_form}
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Mandos</h2><p>Organizados por ciclo, cuenta, riesgo y fuego real.</p></div>
          </div>
          <div class="panel-body">
            {onboarding_actions}
            {controls_html}
          </div>
        </section>
      </aside>

      <section class="rail center-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>Pulso y Tendencias</h2><p>Lectura rapida del comportamiento reciente con curvas, actividad y sesgo.</p></div>
            <span class="panel-pill {'live' if snapshot.mt5_setup_detected else 'alert'}">setup<strong>{'activo' if snapshot.mt5_setup_detected else 'quieto'}</strong></span>
          </div>
          <div class="panel-body">{render_trend_board(snapshot)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Ticket de Operacion</h2><p>Lo que el motor quiere mandar ahora mismo.</p></div>
            <span class="panel-pill">par<strong>{ticket_pill}</strong></span>
          </div>
          <div class="panel-body">{render_candidate(snapshot.candidate_preview)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Radar XAU</h2><p>Ventana operativa, setup actual, capas abiertas y benchmark activo.</p></div>
            <span class="panel-pill {'live' if snapshot.mt5_session_state == 'ACTIVA' else 'alert'}">sesion<strong>{escape(snapshot.mt5_session_state)}</strong></span>
          </div>
          <div class="panel-body">{render_mt5_radar(snapshot)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Consola</h2><p>Salida textual exacta de la ultima accion disparada.</p></div>
            <span class="panel-pill alert">ultima<strong>{escape(last_action)}</strong></span>
          </div>
          <div class="panel-body">
            <div class="card console">
              <div class="console-bar"><span class="dot red"></span><span class="dot amber"></span><span class="dot green"></span><span class="row-mono">{escape(last_action)}</span></div>
              <pre>{escape(last_output)}</pre>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>Tape Operativo</h2><p>Corridas y fills recientes para leer el pulso del sistema.</p></div>
          </div>
          <div class="panel-body">
            <div class="tape-grid">
              <section class="tape-col"><h3>Corridas recientes</h3>{render_runs(snapshot.recent_runs[:6])}</section>
              <section class="tape-col"><h3>Fills recientes</h3>{render_fills(snapshot.recent_fills[:6])}</section>
            </div>
          </div>
        </section>
      </section>

      <aside class="rail right-rail">
        <section class="panel">
          <div class="panel-head">
            <div><h2>{inventory_title}</h2><p>{inventory_subtitle}</p></div>
          </div>
          <div class="panel-body">
            <div class="stats-bar">
              <div class="card"><span>Cash</span><strong>{snapshot.portfolio_cash:.2f}</strong></div>
              <div class="card"><span>Realized PnL</span><strong class="tone-{realized_tone}">{snapshot.portfolio_realized_pnl:.2f}</strong></div>
              <div class="card"><span>Posiciones</span><strong>{len(snapshot.portfolio_positions)}</strong></div>
            </div>
            <div class="list">{render_positions(snapshot.portfolio_positions)}</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>{account_label}</h2><p>{account_subtitle}</p></div>
          </div>
          <div class="panel-body list">{render_balances(snapshot.balances, snapshot.balances_error)}</div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>{orders_label}</h2><p>{orders_subtitle}</p></div>
          </div>
          <div class="panel-body list">{render_open_orders(snapshot.open_orders, snapshot.open_orders_error)}</div>
        </section>
      </aside>
    </section>
  </main>
</body>
</html>"""


def _status_tone(status: str) -> str:
    return {
        "LISTO": "green",
        "FALLA": "red",
        "BLOQUEADO": "red",
        "PENDIENTE": "amber",
    }.get(status, "cyan")


def _operator_panel_styles() -> str:
    return """
    :root {
      --bg: #03060b; --bg-soft: #071018; --surface: rgba(7,16,24,.94); --line: rgba(128,165,193,.14);
      --text: #edf4fb; --muted: #88a2b8; --muted-2: #6b8397; --cyan: #38d9ff; --green: #37d67a;
      --amber: #ffb547; --red: #ff6b6b; --shadow: 0 22px 80px rgba(0,0,0,.48);
      --mono: "Cascadia Mono", "IBM Plex Mono", "Consolas", monospace; --sans: "Bahnschrift", "Segoe UI Variable Text", "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; } html { color-scheme: dark; }
    body { margin: 0; min-height: 100vh; color: var(--text); font-family: var(--sans); background: radial-gradient(circle at 12% 8%, rgba(56,217,255,.11), transparent 22%), radial-gradient(circle at 86% 9%, rgba(255,181,71,.08), transparent 18%), linear-gradient(180deg, #020408 0%, #07111a 38%, #04080d 100%); }
    .shell { width: min(1680px, calc(100% - 18px)); margin: 10px auto 18px; display: grid; gap: 12px; }
    .topbar, .panel { border: 1px solid var(--line); background: linear-gradient(180deg, rgba(8,16,24,.98), rgba(4,9,14,.96)); box-shadow: var(--shadow); backdrop-filter: blur(14px); }
    .topbar { border-radius: 24px; padding: 18px 18px 16px; display: grid; gap: 14px; }
    .topline { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; flex-wrap: wrap; }
    .kicker { font-size: 11px; letter-spacing: .18em; text-transform: uppercase; color: var(--cyan); margin-bottom: 8px; }
    h1 { margin: 0; font-size: clamp(24px, 3.6vw, 40px); letter-spacing: -.05em; line-height: .95; }
    .lede { margin: 8px 0 0; max-width: 760px; color: var(--muted); line-height: 1.45; font-size: 14px; }
    .market-strip, .chip-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .market-strip { align-items: center; justify-content: flex-end; }
    .tag, .chip, .panel-pill { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 7px 11px; color: var(--muted); background: rgba(255,255,255,.02); font-size: 11px; text-transform: uppercase; letter-spacing: .14em; }
    .tag strong, .chip strong, .panel-pill strong { color: var(--text); text-transform: none; letter-spacing: 0; font-weight: 700; }
    .panel-pill.alert { border-color: rgba(255,181,71,.34); color: var(--amber); background: rgba(255,181,71,.08); }
    .panel-pill.live { border-color: rgba(56,217,255,.24); color: var(--cyan); background: rgba(56,217,255,.08); }
    .status-ribbon { display: grid; gap: 10px; grid-template-columns: 1.35fr repeat(4, minmax(0,1fr)); }
    .ribbon-main, .ribbon-box, .card, .row-card, .empty-state, .desk-note { border: 1px solid var(--line); border-radius: 16px; background: linear-gradient(180deg, rgba(10,22,33,.98), rgba(6,12,18,.96)); }
    .ribbon-main, .ribbon-box, .card, .empty-state, .desk-note { padding: 14px; }
    .ribbon-main { display: grid; gap: 10px; } .ribbon-main h2, .panel-head h2, .control-label, .field label, .tape-col h3 { margin: 0; font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--muted-2); }
    .ribbon-main p, .panel-head p, .subtle { margin: 0; color: var(--muted); line-height: 1.45; font-size: 13px; }
    .mini-grid, .stats-bar, .candidate-grid, .actions, .tape-grid { display: grid; gap: 10px; }
    .mini-grid { grid-template-columns: repeat(3, minmax(0,1fr)); } .mini-cell { border-top: 1px solid var(--line); padding-top: 10px; } .mini-cell span, .ribbon-label, .metric-cell span, .stats-bar .card span { display: block; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted-2); }
    .mini-cell strong, .ribbon-value, .stats-bar .card strong { display: block; margin-top: 6px; font-size: 18px; letter-spacing: -.04em; } .ribbon-value { font-size: 28px; } .stats-bar .card strong { font-size: 25px; }
    .tone-cyan { color: var(--cyan); } .tone-green { color: var(--green); } .tone-amber { color: var(--amber); } .tone-red { color: var(--red); }
    .desk { display: grid; gap: 12px; grid-template-columns: minmax(300px,340px) minmax(560px,1fr) minmax(320px,380px); align-items: start; } .rail, .panel-body, .control-group, .fields, .list, .tape-col, .candidate { display: grid; gap: 12px; }
    .panel { border-radius: 20px; overflow: hidden; } .panel-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; padding: 14px 16px 12px; border-bottom: 1px solid var(--line); background: linear-gradient(180deg, rgba(13,26,39,.9), rgba(8,17,26,.32)); } .panel-head p { margin-top: 6px; }
    .desk-note strong { font-size: 13px; text-transform: uppercase; letter-spacing: .14em; color: var(--muted-2); } .desk-note .value { font-size: 15px; color: var(--text); } .code-path, .row-mono { font-family: var(--mono); font-size: 11px; color: var(--muted-2); word-break: break-all; }
    input { width: 100%; border: 1px solid var(--line); border-radius: 14px; padding: 12px 13px; background: rgba(4,10,16,.88); color: var(--text); font-family: var(--mono); } input:focus { outline: none; border-color: rgba(56,217,255,.52); box-shadow: 0 0 0 3px rgba(56,217,255,.08); }
    form { margin: 0; } .actions { grid-template-columns: repeat(2, minmax(0,1fr)); } .actions.stack { grid-template-columns: 1fr; }
    button { width: 100%; min-height: 46px; border-radius: 14px; border: 1px solid rgba(56,217,255,.18); background: linear-gradient(180deg, rgba(13,27,40,.98), rgba(8,16,24,.94)); color: var(--text); font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer; transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease; }
    button:hover { transform: translateY(-1px); border-color: rgba(56,217,255,.52); box-shadow: 0 8px 20px rgba(0,0,0,.24); }
    button.danger { border-color: rgba(255,107,107,.26); background: linear-gradient(180deg, rgba(55,18,20,.95), rgba(21,8,10,.94)); }
    button.danger:hover { border-color: rgba(255,107,107,.56); }
    button.success { border-color: rgba(55,214,122,.28); background: linear-gradient(180deg, rgba(15,49,31,.98), rgba(8,24,14,.94)); }
    button.success:hover { border-color: rgba(55,214,122,.58); }
    button.warning { border-color: rgba(255,181,71,.28); background: linear-gradient(180deg, rgba(54,33,10,.98), rgba(28,16,6,.94)); }
    button.warning:hover { border-color: rgba(255,181,71,.58); }
    button.ghost { border-color: rgba(128,165,193,.18); background: linear-gradient(180deg, rgba(10,18,26,.98), rgba(6,10,16,.94)); }
    button.wide { min-height: 54px; font-size: 14px; }
    .console { padding: 0; overflow: hidden; } .console-bar { display: flex; align-items: center; gap: 8px; padding: 12px 14px; border-bottom: 1px solid var(--line); background: rgba(255,255,255,.02); }
    .dot { width: 9px; height: 9px; border-radius: 999px; display: inline-block; } .dot.red { background: var(--red); } .dot.amber { background: var(--amber); } .dot.green { background: var(--green); }
    pre { margin: 0; padding: 14px; min-height: 220px; max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size: 12px; line-height: 1.55; color: #d6e4f0; }
    .candidate-hero, .row-top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; } .candidate-hero { padding-bottom: 14px; border-bottom: 1px solid var(--line); }
    .candidate-hero h3 { margin: 0; font-size: 34px; letter-spacing: -.05em; line-height: .92; } .candidate-hero p { margin: 8px 0 0; color: var(--muted); max-width: 540px; font-size: 14px; line-height: 1.45; }
    .side-pill { display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px; border-radius: 999px; border: 1px solid var(--line); background: rgba(255,255,255,.02); font-size: 11px; text-transform: uppercase; letter-spacing: .16em; }
    .side-pill.buy { color: var(--green); border-color: rgba(55,214,122,.28); background: rgba(55,214,122,.08); } .side-pill.sell { color: var(--red); border-color: rgba(255,107,107,.28); background: rgba(255,107,107,.08); }
    .candidate-grid { grid-template-columns: repeat(4, minmax(0,1fr)); } .metric-cell { border: 1px solid var(--line); border-radius: 14px; padding: 12px; background: rgba(255,255,255,.02); } .metric-cell strong { display: block; margin-top: 6px; font-size: 18px; letter-spacing: -.04em; }
    .trend-stack { display: grid; gap: 12px; }
    .trend-stats { display: grid; gap: 10px; grid-template-columns: repeat(4, minmax(0,1fr)); }
    .trend-stat, .chart-card, .flow-card { border: 1px solid var(--line); border-radius: 16px; background: linear-gradient(180deg, rgba(10,22,33,.98), rgba(6,12,18,.96)); }
    .trend-stat { padding: 12px 14px; }
    .trend-stat span { display: block; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted-2); }
    .trend-stat strong { display: block; margin-top: 8px; font-size: 22px; letter-spacing: -.04em; }
    .trend-grid { display: grid; gap: 12px; grid-template-columns: repeat(3, minmax(0,1fr)); }
    .chart-card { padding: 14px; display: grid; gap: 12px; }
    .chart-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
    .chart-head h3 { margin: 0; font-size: 15px; letter-spacing: -.03em; }
    .chart-head p { margin: 5px 0 0; font-size: 12px; color: var(--muted); line-height: 1.45; max-width: 240px; }
    .chart-wrap { display: grid; gap: 10px; }
    .chart-svg { width: 100%; height: auto; display: block; border-radius: 14px; background: rgba(255,255,255,.015); }
    .axis-line { stroke: rgba(128,165,193,.18); stroke-width: 1; }
    .trend-line { fill: none; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
    .area-fill { fill: rgba(56,217,255,.12); }
    .chart-node { opacity: .92; }
    .chart-node-last { filter: drop-shadow(0 0 8px rgba(56,217,255,.35)); }
    .chart-foot, .chart-legend, .flow-head { display: flex; justify-content: space-between; gap: 10px; align-items: center; flex-wrap: wrap; }
    .chart-foot span, .chart-legend span { color: var(--muted); font-size: 12px; }
    .chart-foot strong { font-size: 16px; letter-spacing: -.03em; }
    .bar-primary { fill: rgba(56,217,255,.68); }
    .bar-secondary { fill: rgba(55,214,122,.78); }
    .flow-card { padding: 12px; display: grid; gap: 10px; }
    .side-tag { display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 7px 10px; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; border: 1px solid var(--line); }
    .side-tag.buy { color: var(--green); border-color: rgba(55,214,122,.28); background: rgba(55,214,122,.08); }
    .side-tag.sell { color: var(--red); border-color: rgba(255,107,107,.28); background: rgba(255,107,107,.08); }
    .split-bar { height: 14px; width: 100%; border-radius: 999px; overflow: hidden; background: rgba(255,255,255,.04); display: flex; }
    .split-buy { background: linear-gradient(90deg, rgba(55,214,122,.82), rgba(55,214,122,.52)); }
    .split-sell { background: linear-gradient(90deg, rgba(255,107,107,.52), rgba(255,107,107,.82)); }
    .row-card { padding: 12px 14px; display: grid; gap: 8px; } .row-title { font-size: 15px; letter-spacing: -.03em; font-weight: 700; color: var(--text); } .meta { display: flex; flex-wrap: wrap; gap: 8px 14px; color: var(--muted); font-size: 12px; } .meta strong { color: var(--text); } .empty-state { color: var(--muted); line-height: 1.45; }
    .tape-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } .market-strip, .chip-row { align-items: center; }
    @media (max-width: 1360px) { .desk { grid-template-columns: 300px 1fr; } .right-rail { grid-column: 1 / -1; display: grid; gap: 12px; grid-template-columns: repeat(3, minmax(0,1fr)); } .trend-grid { grid-template-columns: 1fr; } }
    @media (max-width: 1120px) { .status-ribbon { grid-template-columns: 1fr 1fr; } .ribbon-main { grid-column: 1 / -1; } .desk, .right-rail { grid-template-columns: 1fr; } .candidate-grid, .tape-grid, .trend-stats { grid-template-columns: 1fr 1fr; } }
    @media (max-width: 760px) { .shell { width: min(100% - 12px, 1680px); } .status-ribbon, .candidate-grid, .stats-bar, .tape-grid, .mini-grid, .actions, .trend-stats { grid-template-columns: 1fr; } .market-strip { justify-content: flex-start; } }
    """


def action_button(action: str, label: str, danger: bool = False, tone: str = "", wide: bool = False) -> str:
    class_tokens: list[str] = []
    if danger:
        class_tokens.append("danger")
    if tone:
        class_tokens.append(tone)
    if wide:
        class_tokens.append("wide")
    class_name = " ".join(class_tokens)
    return (
        '<form method="post" action="/action">'
        f'<input type="hidden" name="action" value="{escape(action)}">'
        f'<button class="{class_name}" type="submit">{escape(label)}</button>'
        "</form>"
    )


def render_status_card(label: str, status: str) -> str:
    class_name = {
        "LISTO": "green",
        "BLOQUEADO": "red",
        "FALLA": "red",
        "PENDIENTE": "amber",
    }.get(status, "cyan")
    return (
        '<article class="status-card">'
        f'<span class="small">{escape(label)}</span>'
        f'<strong class="{class_name}">{escape(status)}</strong>'
        "</article>"
    )


def render_candidate(candidate: dict[str, object] | None) -> str:
    if candidate is None:
        return '<div class="empty-state">No hay orden candidata ahora mismo. El motor no ve una entrada limpia o el filtro de riesgo la esta rechazando antes de llegar al ticket.</div>'
    side = str(candidate.get("side", "")).lower()
    side_class = "buy" if side == "buy" else "sell" if side == "sell" else ""
    symbol = escape(str(candidate.get("symbol") or candidate.get("market_id") or "BTC/USD"))
    reason = escape(str(candidate.get("reason") or "Sin razon disponible."))
    metrics = [
        ("Precio", candidate.get("price")),
        ("Tamano", candidate.get("size")),
        ("Confianza", candidate.get("confidence")),
        ("Edge esp.", candidate.get("expected_edge")),
        ("Profundidad", candidate.get("book_depth")),
        ("Imbalance", candidate.get("liquidity_imbalance")),
        ("Min size", candidate.get("min_order_size")),
        ("Tick", candidate.get("tick_size")),
    ]
    metrics_html = "".join(
        (
            '<div class="metric-cell">'
            f"<span>{escape(label)}</span>"
            f"<strong>{escape(str(value))}</strong>"
            "</div>"
        )
        for label, value in metrics
    )
    return (
        '<article class="card candidate">'
        '<div class="candidate-hero">'
        "<div>"
        f'<div class="side-pill {side_class}">{escape(side or "setup")}</div>'
        f"<h3>{symbol}</h3>"
        f"<p>{reason}</p>"
        "</div>"
        '<div class="chip-row">'
        f'<span class="chip">tipo <strong>{escape(str(candidate.get("order_type", "-")))}</strong></span>'
        f'<span class="chip">fuente <strong>{escape(str(candidate.get("source", "-")))}</strong></span>'
        f'<span class="chip">mercado <strong>{escape(str(candidate.get("market_type", "-")))}</strong></span>'
        f'<span class="chip">kill <strong>{escape(str(candidate.get("kill_switch", "-")))}</strong></span>'
        "</div>"
        "</div>"
        f'<div class="candidate-grid">{metrics_html}</div>'
        '<div class="chip-row">'
        f'<span class="chip">market_id <strong>{escape(str(candidate.get("market_id", "-")))}</strong></span>'
        f'<span class="chip">token <strong>{escape(str(candidate.get("token_id", "-")))}</strong></span>'
        f'<span class="chip">neg_risk <strong>{escape(str(candidate.get("neg_risk", "-")))}</strong></span>'
        "</div>"
        "</article>"
    )


def render_mt5_radar(snapshot: OperatorSnapshot) -> str:
    setup_value = "SI" if snapshot.mt5_setup_detected else "NO"
    setup_tone = "green" if snapshot.mt5_setup_detected else "amber"
    live_win = (
        f"{snapshot.mt5_live_win_rate:.1f}%"
        if snapshot.mt5_live_win_rate is not None
        else "s/d"
    )
    benchmark_win = (
        f"{snapshot.mt5_benchmark_win_rate:.1f}%"
        if snapshot.mt5_benchmark_win_rate is not None
        else "s/d"
    )
    benchmark_pf = (
        f"{snapshot.mt5_benchmark_profit_factor:.2f}"
        if snapshot.mt5_benchmark_profit_factor is not None
        else "s/d"
    )
    error_note = (
        f'<div class="empty-state">Radar MT5 parcial: {escape(snapshot.mt5_status_error)}</div>'
        if snapshot.mt5_status_error
        else ""
    )
    return (
        '<article class="candidate-card">'
        '<div class="candidate-head">'
        '<div>'
        '<div class="row-mono">xauusd scalping</div>'
        '<div class="row-title">Estado tactico del oro</div>'
        "</div>"
        '<div class="chip-row">'
        f'<span class="chip">ventana <strong>{escape(snapshot.mt5_window_local)}</strong></span>'
        f'<span class="chip">proximo <strong>{escape(snapshot.mt5_next_event_local)}</strong></span>'
        "</div>"
        "</div>"
        '<div class="candidate-grid">'
        f'<div class="card"><span>Setup</span><strong class="tone-{setup_tone}">{setup_value}</strong></div>'
        f'<div class="card"><span>Capas Buy</span><strong>{snapshot.mt5_buy_layers}</strong></div>'
        f'<div class="card"><span>Capas Sell</span><strong>{snapshot.mt5_sell_layers}</strong></div>'
        f'<div class="card"><span>Win Rate Live</span><strong>{live_win}</strong></div>'
        f'<div class="card"><span>Trades Live</span><strong>{snapshot.mt5_live_trade_count}</strong></div>'
        f'<div class="card"><span>Win Rate Benchmark</span><strong>{benchmark_win}</strong></div>'
        f'<div class="card"><span>PF Benchmark</span><strong>{benchmark_pf}</strong></div>'
        "</div>"
        '<div class="candidate-reason">'
        f'<strong>Lectura</strong><span>{escape(snapshot.mt5_setup_reason)}</span>'
        "</div>"
        f"{error_note}"
        "</article>"
    )


def render_trend_board(snapshot: OperatorSnapshot) -> str:
    runs = list(reversed(snapshot.recent_runs[:18]))
    fills = list(reversed(snapshot.recent_fills[:24]))

    cumulative: list[float] = []
    run_pnls: list[float] = []
    signal_counts: list[float] = []
    fill_counts: list[float] = []
    labels: list[str] = []
    rolling = 0.0
    for row in runs:
        pnl = float(row.get("pnl") or 0.0)
        rolling += pnl
        cumulative.append(rolling)
        run_pnls.append(pnl)
        signal_counts.append(float(row.get("signals_count") or 0.0))
        fill_counts.append(float(row.get("fills_count") or 0.0))
        labels.append(f"#{row.get('id')}")

    positive_runs = sum(1 for value in run_pnls if value > 0)
    avg_pnl = (sum(run_pnls) / len(run_pnls)) if run_pnls else 0.0
    avg_signals = (sum(signal_counts) / len(signal_counts)) if signal_counts else 0.0
    buy_fills = sum(1 for row in fills if str(row.get("side") or "").lower() == "buy")
    sell_fills = sum(1 for row in fills if str(row.get("side") or "").lower() == "sell")
    fill_split = render_fill_split(buy_fills, sell_fills)
    price_series = [float(row.get("price") or 0.0) for row in fills if float(row.get("price") or 0.0) > 0]

    summary_cards = (
        f'<div class="trend-stats">'
        f'<article class="trend-stat"><span>Runs verdes</span><strong>{positive_runs}/{len(run_pnls) if run_pnls else 0}</strong></article>'
        f'<article class="trend-stat"><span>PnL medio/run</span><strong>{avg_pnl:.2f}</strong></article>'
        f'<article class="trend-stat"><span>Signals/run</span><strong>{avg_signals:.1f}</strong></article>'
        f'<article class="trend-stat"><span>Live vs benchmark</span><strong>{_live_vs_benchmark(snapshot)}</strong></article>'
        "</div>"
    )
    return (
        '<section class="trend-stack">'
        f"{summary_cards}"
        '<div class="trend-grid">'
        '<article class="chart-card">'
        '<div class="chart-head"><div><h3>Curva de PnL</h3><p>Evolucion acumulada de las ultimas corridas del motor.</p></div>'
        f'<span class="chip">runs <strong>{len(runs)}</strong></span></div>'
        f"{render_line_chart(cumulative, labels, stroke='var(--cyan)', positive_fill=True)}"
        "</article>"
        '<article class="chart-card">'
        '<div class="chart-head"><div><h3>Actividad</h3><p>Signals y fills por corrida para ver ritmo y conversion.</p></div>'
        f'<span class="chip">fills <strong>{int(sum(fill_counts))}</strong></span></div>'
        f"{render_dual_bar_chart(labels, signal_counts, fill_counts)}"
        "</article>"
        '<article class="chart-card">'
        '<div class="chart-head"><div><h3>Tape y sesgo</h3><p>Flujo reciente de ejecuciones y balance entre buy y sell.</p></div>'
        f'<span class="chip">buys/sells <strong>{buy_fills}/{sell_fills}</strong></span></div>'
        f"{fill_split}"
        f"{render_line_chart(price_series, [str(index + 1) for index in range(len(price_series))], stroke='var(--amber)', positive_fill=False, height=116, compact=True)}"
        "</article>"
        "</div>"
        "</section>"
    )


def _live_vs_benchmark(snapshot: OperatorSnapshot) -> str:
    if snapshot.mt5_live_win_rate is None and snapshot.mt5_benchmark_win_rate is None:
        return "s/d"
    if snapshot.mt5_live_win_rate is None:
        return f"bench {snapshot.mt5_benchmark_win_rate:.1f}%"
    if snapshot.mt5_benchmark_win_rate is None:
        return f"live {snapshot.mt5_live_win_rate:.1f}%"
    delta = snapshot.mt5_live_win_rate - snapshot.mt5_benchmark_win_rate
    prefix = "+" if delta >= 0 else ""
    return f"{prefix}{delta:.1f}%"


def render_fill_split(buy_count: int, sell_count: int) -> str:
    total = max(buy_count + sell_count, 1)
    buy_ratio = (buy_count / total) * 100.0
    sell_ratio = 100.0 - buy_ratio
    return (
        '<div class="flow-card">'
        '<div class="flow-head">'
        f'<span class="side-tag buy">Buy {buy_count}</span>'
        f'<span class="side-tag sell">Sell {sell_count}</span>'
        "</div>"
        '<div class="split-bar">'
        f'<div class="split-buy" style="width:{buy_ratio:.2f}%"></div>'
        f'<div class="split-sell" style="width:{sell_ratio:.2f}%"></div>'
        "</div>"
        "</div>"
    )


def render_line_chart(
    values: list[float],
    labels: list[str],
    *,
    stroke: str,
    positive_fill: bool,
    height: int = 160,
    compact: bool = False,
) -> str:
    if len(values) < 2:
        return '<div class="empty-state">Todavia no hay suficientes puntos para pintar tendencia real.</div>'

    width = 560 if not compact else 520
    padding_x = 14
    padding_top = 14
    padding_bottom = 22
    usable_width = max(width - (padding_x * 2), 10)
    usable_height = max(height - (padding_top + padding_bottom), 10)
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum
    if not isfinite(span) or span == 0:
        span = max(abs(maximum), 1.0)
        minimum = minimum - (span / 2.0)
        maximum = maximum + (span / 2.0)
    coords: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = padding_x + (usable_width * index / max(len(values) - 1, 1))
        normalized = (value - minimum) / (maximum - minimum)
        y = padding_top + ((1.0 - normalized) * usable_height)
        coords.append((x, y))

    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    area = (
        f"{padding_x:.2f},{height - padding_bottom:.2f} "
        + polyline
        + f" {coords[-1][0]:.2f},{height - padding_bottom:.2f}"
    )
    first_label = escape(labels[0]) if labels else "-"
    last_label = escape(labels[-1]) if labels else "-"
    return (
        '<div class="chart-wrap">'
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="none">'
        f'<line x1="{padding_x}" y1="{height - padding_bottom}" x2="{width - padding_x}" y2="{height - padding_bottom}" class="axis-line" />'
        f'<line x1="{padding_x}" y1="{padding_top}" x2="{padding_x}" y2="{height - padding_bottom}" class="axis-line" />'
        + (
            f'<polygon points="{area}" class="area-fill"></polygon>'
            if positive_fill
            else ""
        )
        + f'<polyline points="{polyline}" class="trend-line" style="stroke:{stroke}"></polyline>'
        + f'<circle cx="{coords[0][0]:.2f}" cy="{coords[0][1]:.2f}" r="3.8" class="chart-node" style="fill:{stroke}"></circle>'
        + f'<circle cx="{coords[-1][0]:.2f}" cy="{coords[-1][1]:.2f}" r="4.4" class="chart-node chart-node-last" style="fill:{stroke}"></circle>'
        + "</svg>"
        + f'<div class="chart-foot"><span>{first_label}</span><strong>{values[-1]:.2f}</strong><span>{last_label}</span></div>'
        + "</div>"
    )


def render_dual_bar_chart(labels: list[str], primary: list[float], secondary: list[float]) -> str:
    if not labels or not primary:
        return '<div class="empty-state">Sin corridas suficientes para dibujar actividad.</div>'
    width = 560
    height = 160
    gap = 6
    count = min(len(labels), len(primary), len(secondary))
    bar_group_width = max((width - 28) / max(count, 1), 12)
    single_bar = max((bar_group_width - gap) / 2, 4)
    max_value = max(max(primary[:count], default=0.0), max(secondary[:count], default=0.0), 1.0)
    bars: list[str] = []
    for index in range(count):
        base_x = 14 + (index * bar_group_width)
        primary_h = ((primary[index] / max_value) * 112) if max_value else 0
        secondary_h = ((secondary[index] / max_value) * 112) if max_value else 0
        bars.append(
            f'<rect x="{base_x:.2f}" y="{132 - primary_h:.2f}" width="{single_bar:.2f}" height="{primary_h:.2f}" class="bar-primary"></rect>'
        )
        bars.append(
            f'<rect x="{base_x + single_bar + gap:.2f}" y="{132 - secondary_h:.2f}" width="{single_bar:.2f}" height="{secondary_h:.2f}" class="bar-secondary"></rect>'
        )
    return (
        '<div class="chart-wrap">'
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg" preserveAspectRatio="none">'
        f'<line x1="14" y1="132" x2="{width - 14}" y2="132" class="axis-line" />'
        + "".join(bars)
        + "</svg>"
        + '<div class="chart-legend"><span><i class="dot cyan"></i>Signals</span><span><i class="dot green"></i>Fills</span></div>'
        + "</div>"
    )


def render_positions(positions: list[dict[str, object]]) -> str:
    if not positions:
        return '<div class="empty-state">Sin posiciones abiertas. La mesa esta plana y todo el capital sigue en caja.</div>'
    return "".join(
        (
            '<article class="row-card">'
            '<div class="row-top">'
            f'<div class="row-title">{escape(short_market(str(item["market_id"])))}</div>'
            f'<div class="row-mono">{escape(str(item.get("side") or "posicion"))}</div>'
            "</div>"
            f'<div class="meta"><span>size <strong>{abs(float(item["size"])):.8f}</strong></span>'
            f'<span>avg <strong>{float(item["average_price"]):.4f}</strong></span></div>'
            "</article>"
        )
        for item in positions
    )


def render_balances(balances: list[tuple[str, float]], error: str | None) -> str:
    if error:
        return f'<div class="empty-state">Sin balances disponibles: {escape(error)}</div>'
    if not balances:
        return '<div class="empty-state">No hay balances no-cero para mostrar.</div>'
    return "".join(
        (
            '<article class="row-card">'
            '<div class="row-top">'
            f'<div class="row-title">{escape(asset)}</div>'
            '<div class="row-mono">balance</div>'
            "</div>"
            f'<div class="meta"><span>disponible <strong>{amount:.8f}</strong></span></div>'
            "</article>"
        )
        for asset, amount in balances
    )


def render_open_orders(rows: list[dict[str, object]], error: str | None) -> str:
    if error:
        return f'<div class="empty-state">Sin acceso a ordenes abiertas: {escape(error)}</div>'
    if not rows:
        return '<div class="empty-state">No hay ordenes abiertas. La mesa esta limpia para este simbolo.</div>'
    return "".join(
        (
            '<article class="row-card">'
            '<div class="row-top">'
            f'<div class="row-title">{escape(str(row["pair"]))}</div>'
            f'<div class="row-mono">{escape(short_market(str(row["txid"])))}</div>'
            "</div>"
            f'<div class="meta"><span>{escape(str(row["type"]))} / {escape(str(row["ordertype"]))}</span>'
            f'<span>price <strong>{escape(str(row["price"]))}</strong></span>'
            f'<span>vol <strong>{escape(str(row["volume"]))}</strong></span></div>'
            "</article>"
        )
        for row in rows
    )


def render_runs(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty-state">Sin corridas registradas todavia.</div>'
    return "".join(
        (
            '<article class="row-card">'
            '<div class="row-top">'
            f'<div class="row-title">Run #{row["id"]}</div>'
            f'<div class="row-mono">{escape(str(row["data_source"]))}</div>'
            "</div>"
            f'<div class="meta"><span>mode <strong>{escape(str(row["mode"]))}</strong></span>'
            f'<span>signals <strong>{row["signals_count"]}</strong></span>'
            f'<span>fills <strong>{row["fills_count"]}</strong></span>'
            f'<span>pnl <strong>{float(row["pnl"] or 0.0):.2f}</strong></span></div>'
            "</article>"
        )
        for row in rows
    )


def render_fills(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty-state">Sin fills recientes. Aun no hay cinta de ejecucion para leer.</div>'
    return "".join(
        (
            '<article class="row-card">'
            '<div class="row-top">'
            f'<div class="row-title">{escape(short_market(str(row["market_id"])))}</div>'
            f'<div class="row-mono">{escape(str(row["side"]))}</div>'
            "</div>"
            f'<div class="meta"><span>price <strong>{float(row["price"]):.4f}</strong></span>'
            f'<span>size <strong>{float(row["size"]):.6f}</strong></span>'
            f'<span>fee <strong>{float(row["fee_paid"]):.2f}</strong></span></div>'
            "</article>"
        )
        for row in rows
    )
