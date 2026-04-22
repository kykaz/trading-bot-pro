from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from trading_bot.config import AppConfig
from trading_bot.mt5 import Mt5Client
from trading_bot.types import MarketSnapshot, OrderIntent, Signal


@dataclass(slots=True)
class LiveCheckReport:
    venue: str
    live_mode: str
    kill_switch_status: str
    public_api_status: str
    auth_status: str
    details: list[str]


def check_live_stack(config: AppConfig, venue: str = "polymarket") -> LiveCheckReport:
    if venue == "mt5":
        return _check_mt5_live_stack(config)
    if venue == "alpaca":
        return _check_alpaca_live_stack(config)
    if venue == "kraken":
        return _check_kraken_live_stack(config)
    return _check_polymarket_live_stack(config)


def build_order_preview(
    config: AppConfig,
    snapshot: MarketSnapshot,
    signal: Signal,
    order: OrderIntent,
) -> dict[str, object]:
    kill_switch_active = Path(config.live.kill_switch_path).exists()
    payload = {
        "kill_switch": "ON" if kill_switch_active else "OFF",
        "question": snapshot.question,
        "market_id": snapshot.market_id,
        "token_id": snapshot.token_id,
        "symbol": snapshot.symbol or order.symbol,
        "source": snapshot.source,
        "market_type": snapshot.market_type,
        "side": order.side.value,
        "order_type": order.order_type,
        "price": round(order.price, 6),
        "size": round(order.size, max(snapshot.size_precision, 2)),
        "tick_size": order.tick_size,
        "min_order_size": snapshot.min_order_size,
        "neg_risk": order.neg_risk,
        "confidence": round(signal.confidence, 4),
        "expected_edge": round(signal.expected_edge, 4),
        "book_depth": round(snapshot.book_depth, 2),
        "liquidity_imbalance": round(snapshot.liquidity_imbalance, 4),
        "reason": signal.reason,
    }
    if order.stop_loss > 0:
        payload["stop_loss"] = round(order.stop_loss, 6)
    if order.take_profit > 0:
        payload["take_profit"] = round(order.take_profit, 6)
    return payload


def get_mt5_account(config: AppConfig) -> dict[str, object]:
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        return dict(client.account_info())


def get_mt5_positions(config: AppConfig) -> list[dict[str, object]]:
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        return [dict(row) for row in client.positions_get(symbol=config.mt5.symbol)]


def get_mt5_open_orders(config: AppConfig) -> list[dict[str, object]]:
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        return [dict(row) for row in client.orders_get(symbol=config.mt5.symbol)]


def update_mt5_position_risk(
    config: AppConfig,
    *,
    ticket: int,
    stop_loss: float,
    take_profit: float,
) -> dict[str, object]:
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        request = {
            "action": client.order_action_sltp(),
            "symbol": config.mt5.symbol,
            "position": int(ticket),
            "sl": float(stop_loss),
            "tp": float(take_profit) if take_profit > 0 else 0.0,
            "magic": config.mt5.magic,
            "comment": config.mt5.comment,
        }
        result = client.order_send(request)
        return {
            "venue": "mt5",
            "ticket": int(ticket),
            "submitted": True,
            "retcode": int(result.get("retcode") or 0),
            "comment": str(result.get("comment") or ""),
            "stop_loss": float(request["sl"]),
            "take_profit": float(request["tp"]),
        }


def close_mt5_position(config: AppConfig, position: dict[str, object]) -> dict[str, object]:
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        symbol = str(position.get("symbol") or config.mt5.symbol)
        volume = float(position.get("volume") or 0.0)
        if volume <= 0:
            raise RuntimeError("La posicion MT5 no trae volumen valido para cerrar.")
        position_type = int(position.get("type") or 0)
        side = "sell" if position_type == 0 else "buy"
        tick = client.symbol_tick(symbol)
        price = float(tick.get("bid") if side == "sell" else tick.get("ask"))
        request = {
            "action": client.order_action_deal(),
            "symbol": symbol,
            "volume": volume,
            "type": client.order_type(side),
            "position": int(position.get("ticket") or position.get("identifier") or 0),
            "price": price,
            "deviation": config.mt5.deviation_points,
            "magic": config.mt5.magic,
            "comment": f"{config.mt5.comment}-close",
            "type_time": client.time_gtc(),
            "type_filling": client.filling_type(config.mt5.fill_type),
        }
        validation = client.order_check(request)
        validation_retcode = int(validation.get("retcode") or 0)
        if validation_retcode not in {0, 10009}:
            raise RuntimeError(f"MT5 rechazo el cierre de la capa {request['position']}: {validation.get('comment') or validation_retcode}")
        result = client.order_send(request)
        return {
            "venue": "mt5",
            "ticket": int(request["position"]),
            "submitted": True,
            "retcode": int(result.get("retcode") or 0),
            "deal": int(result.get("deal") or 0),
            "order_id": int(result.get("order") or 0),
            "comment": str(result.get("comment") or ""),
        }


def submit_mt5_order(config: AppConfig, order: OrderIntent, *, live: bool) -> dict[str, object]:
    _assert_mt5_order_is_supported(config, order, live=live)
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        request = _build_mt5_order_request(config, client, order)
        validation = client.order_check(request)
        retcode = int(validation.get("retcode") or 0)
        validated = retcode in {0, 10009}
        payload = {
            "venue": "mt5",
            "validated": validated,
            "submitted": False,
            "retcode": retcode,
            "comment": str(validation.get("comment") or ""),
            "order": request,
        }
        if not live:
            return payload
        result = client.order_send(request)
        payload.update(
            {
                "submitted": True,
                "send_retcode": int(result.get("retcode") or 0),
                "deal": int(result.get("deal") or 0),
                "order_id": int(result.get("order") or 0),
                "volume": float(result.get("volume") or request["volume"]),
                "price": float(result.get("price") or request["price"]),
                "comment": str(result.get("comment") or payload["comment"]),
            }
        )
        return payload


def _build_mt5_order_request(config: AppConfig, client: Mt5Client, order: OrderIntent) -> dict[str, object]:
    return {
        "action": client.order_action_deal(),
        "symbol": config.mt5.symbol,
        "volume": order.size,
        "type": client.order_type(order.side.value),
        "price": order.price,
        "deviation": config.mt5.deviation_points,
        "magic": config.mt5.magic,
        "comment": config.mt5.comment,
        "type_time": client.time_gtc(),
        "type_filling": client.filling_type(config.mt5.fill_type),
        "sl": order.stop_loss if order.stop_loss > 0 else 0.0,
        "tp": order.take_profit if order.take_profit > 0 else 0.0,
    }


def validate_kraken_order(config: AppConfig, order: OrderIntent) -> dict[str, object]:
    return submit_kraken_order(config, order, validate=True)


def submit_kraken_order(config: AppConfig, order: OrderIntent, *, validate: bool) -> dict[str, object]:
    _assert_kraken_order_is_supported(order)
    if not validate:
        _assert_kraken_live_submission_allowed(config)

    payload = {
        "pair": config.kraken.pair,
        "type": order.side.value,
        "ordertype": "limit",
        "price": _format_decimal(order.price, tick_size=order.tick_size),
        "volume": _format_size(order.size),
        "validate": "true" if validate else "false",
    }
    response = _kraken_private_request(config, "/0/private/AddOrder", payload)
    errors = response.get("error") or []
    result = response.get("result") or {}
    return {
        "venue": "kraken",
        "submitted": not errors and not validate,
        "validated": not errors,
        "errors": errors,
        "description": result.get("descr") or {},
        "txid": result.get("txid") or [],
    }


def arm_kraken_dead_man_switch(config: AppConfig, timeout_seconds: int) -> dict[str, object]:
    response = _kraken_private_request(
        config,
        "/0/private/CancelAllOrdersAfter",
        {"timeout": str(timeout_seconds)},
    )
    errors = response.get("error") or []
    result = response.get("result") or {}
    return {
        "venue": "kraken",
        "armed": not errors,
        "errors": errors,
        "current_time": result.get("currentTime"),
        "trigger_time": result.get("triggerTime"),
        "timeout_seconds": timeout_seconds,
    }


def get_kraken_balances(config: AppConfig) -> dict[str, float]:
    response = _kraken_private_request(config, "/0/private/Balance", {})
    errors = response.get("error") or []
    if errors:
        raise RuntimeError(f"Kraken devolvio errores en balance: {errors}")
    raw_result = response.get("result") or {}
    balances: dict[str, float] = {}
    for asset, value in dict(raw_result).items():
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if abs(amount) > 1e-12:
            balances[str(asset)] = amount
    return balances


def get_kraken_open_orders(config: AppConfig) -> list[dict[str, object]]:
    response = _kraken_private_request(config, "/0/private/OpenOrders", {})
    errors = response.get("error") or []
    if errors:
        raise RuntimeError(f"Kraken devolvio errores en open orders: {errors}")
    result = response.get("result") or {}
    open_orders = dict(result.get("open") or {})
    rows: list[dict[str, object]] = []
    for txid, payload in open_orders.items():
        data = dict(payload)
        descr = dict(data.get("descr") or {})
        rows.append(
            {
                "txid": str(txid),
                "pair": descr.get("pair") or "",
                "order": descr.get("order") or "",
                "type": descr.get("type") or "",
                "ordertype": descr.get("ordertype") or "",
                "price": descr.get("price") or "",
                "volume": data.get("vol") or "",
                "volume_exec": data.get("vol_exec") or "",
                "status": data.get("status") or "",
                "userref": data.get("userref") or "",
            }
        )
    rows.sort(key=lambda row: row["txid"])
    return rows


def cancel_kraken_order(config: AppConfig, txid: str) -> dict[str, object]:
    response = _kraken_private_request(config, "/0/private/CancelOrder", {"txid": txid})
    errors = response.get("error") or []
    result = response.get("result") or {}
    return {
        "venue": "kraken",
        "cancelled": not errors,
        "errors": errors,
        "count": result.get("count"),
        "pending": result.get("pending"),
    }


def cancel_all_kraken_orders(config: AppConfig) -> dict[str, object]:
    response = _kraken_private_request(config, "/0/private/CancelAll", {})
    errors = response.get("error") or []
    result = response.get("result") or {}
    return {
        "venue": "kraken",
        "cancelled": not errors,
        "errors": errors,
        "count": result.get("count"),
        "pending": result.get("pending"),
    }


def get_alpaca_account(config: AppConfig) -> dict[str, object]:
    payload = _alpaca_request(config, "/v2/account", method="GET", live=False)
    if not isinstance(payload, dict):
        raise RuntimeError("Alpaca devolvio una respuesta invalida en /v2/account.")
    return payload


def get_alpaca_positions(config: AppConfig) -> list[dict[str, object]]:
    payload = _alpaca_request(config, "/v2/positions", method="GET", live=False)
    if not isinstance(payload, list):
        raise RuntimeError("Alpaca devolvio una respuesta invalida en /v2/positions.")
    return [dict(item) for item in payload if isinstance(item, dict)]


def get_alpaca_balances(config: AppConfig) -> dict[str, float]:
    account = get_alpaca_account(config)
    balances: dict[str, float] = {}
    cash = _parse_float(account.get("cash"))
    buying_power = _parse_float(account.get("buying_power"))
    if cash:
        balances["USD"] = cash
    if buying_power:
        balances["USD_buying_power"] = buying_power
    for position in get_alpaca_positions(config):
        symbol = str(position.get("symbol") or "")
        qty = _parse_float(position.get("qty"))
        if qty and symbol:
            balances[symbol] = qty
    return balances


def get_alpaca_open_orders(config: AppConfig) -> list[dict[str, object]]:
    query = urlencode(
        {
            "status": "open",
            "symbols": config.alpaca.symbol,
            "direction": "desc",
            "limit": "50",
        }
    )
    payload = _alpaca_request(config, f"/v2/orders?{query}", method="GET", live=False)
    if not isinstance(payload, list):
        raise RuntimeError("Alpaca devolvio una respuesta invalida en /v2/orders.")
    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "client_order_id": str(item.get("client_order_id") or ""),
                "symbol": str(item.get("symbol") or ""),
                "side": str(item.get("side") or ""),
                "type": str(item.get("type") or ""),
                "time_in_force": str(item.get("time_in_force") or ""),
                "limit_price": str(item.get("limit_price") or ""),
                "qty": str(item.get("qty") or ""),
                "filled_qty": str(item.get("filled_qty") or ""),
                "status": str(item.get("status") or ""),
            }
        )
    return rows


def submit_alpaca_order(config: AppConfig, order: OrderIntent, *, live: bool) -> dict[str, object]:
    _assert_alpaca_order_is_supported(config, order, live=live)
    payload = {
        "symbol": config.alpaca.symbol,
        "side": order.side.value,
        "type": "limit",
        "time_in_force": "gtc",
        "qty": _format_size(order.size),
        "limit_price": _format_decimal(order.price, tick_size=order.tick_size),
    }
    response = _alpaca_request(config, "/v2/orders", method="POST", payload=payload, live=live)
    if not isinstance(response, dict):
        raise RuntimeError("Alpaca devolvio una respuesta invalida al enviar la orden.")
    return {
        "venue": "alpaca",
        "submitted": True,
        "validated": True,
        "id": response.get("id") or "",
        "client_order_id": response.get("client_order_id") or "",
        "status": response.get("status") or "",
        "symbol": response.get("symbol") or config.alpaca.symbol,
        "qty": response.get("qty") or payload["qty"],
        "filled_qty": response.get("filled_qty") or "0",
        "limit_price": response.get("limit_price") or payload["limit_price"],
        "filled_avg_price": response.get("filled_avg_price") or "",
        "raw": response,
    }


def cancel_alpaca_order(config: AppConfig, order_id: str) -> dict[str, object]:
    _alpaca_request(config, f"/v2/orders/{quote(order_id, safe='')}", method="DELETE", live=False)
    return {
        "venue": "alpaca",
        "cancelled": True,
        "errors": [],
        "count": 1,
        "pending": 0,
    }


def cancel_all_alpaca_orders(config: AppConfig) -> dict[str, object]:
    response = _alpaca_request(config, "/v2/orders", method="DELETE", live=False)
    count = len(response) if isinstance(response, list) else 0
    return {
        "venue": "alpaca",
        "cancelled": True,
        "errors": [],
        "count": count,
        "pending": 0,
    }


def close_alpaca_position(config: AppConfig, symbol: str) -> dict[str, object]:
    response = _alpaca_request(
        config,
        f"/v2/positions/{quote(symbol, safe='')}",
        method="DELETE",
        live=False,
    )
    if not isinstance(response, dict):
        response = {}
    return {
        "venue": "alpaca",
        "symbol": symbol,
        "status": response.get("status") or "",
        "id": response.get("id") or "",
        "qty": response.get("qty") or "",
        "filled_qty": response.get("filled_qty") or "",
    }


def set_kill_switch(path: str, enabled: bool | None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if enabled is True:
        target.write_text("kill-switch=on\n", encoding="utf-8")
    elif enabled is False and target.exists():
        target.unlink()


def _check_polymarket_live_stack(config: AppConfig) -> LiveCheckReport:
    kill_switch_status = _kill_switch_status(config.live.kill_switch_path)
    live_mode = "DRY_RUN" if config.live.dry_run else ("LIVE_ON" if config.live.enabled else "LIVE_OFF")

    details: list[str] = [
        f"signature_type={config.live.signature_type}",
        f"kill_switch_path={config.live.kill_switch_path}",
    ]
    public_api_status = "OK"
    try:
        request = Request(
            f"{config.polymarket.host}/ok",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urlopen(request, timeout=config.data.request_timeout_seconds) as response:
            response.read()
    except Exception as exc:
        public_api_status = "FALLA"
        details.append(f"public_api_error={exc}")

    required = {
        config.live.private_key_env: bool(os.getenv(config.live.private_key_env)),
        config.live.api_key_env: bool(os.getenv(config.live.api_key_env)),
        config.live.api_secret_env: bool(os.getenv(config.live.api_secret_env)),
        config.live.api_passphrase_env: bool(os.getenv(config.live.api_passphrase_env)),
        config.live.funder_env: bool(os.getenv(config.live.funder_env)),
    }
    details.extend(f"{name}={'set' if present else 'missing'}" for name, present in required.items())

    if required[config.live.api_key_env] and required[config.live.api_secret_env] and required[config.live.api_passphrase_env]:
        auth_status = "L2_LISTO"
    elif required[config.live.private_key_env]:
        auth_status = "LLAVE_PRESENTE"
        details.append("Hay llave privada, pero faltan credenciales L2 completas para trading seguro.")
    else:
        auth_status = "SIN_AUTH"
        details.append("No hay credenciales live cargadas en el entorno.")

    if kill_switch_status == "ACTIVO":
        details.append("El kill switch esta activo; cualquier live trading debe quedar bloqueado.")
    if not config.live.enabled:
        details.append("Live trading sigue deshabilitado en config, que es lo correcto para esta etapa.")
    return LiveCheckReport(
        venue="polymarket",
        live_mode=live_mode,
        kill_switch_status=kill_switch_status,
        public_api_status=public_api_status,
        auth_status=auth_status,
        details=details,
    )


def _check_alpaca_live_stack(config: AppConfig) -> LiveCheckReport:
    kill_switch_status = _kill_switch_status(config.live.kill_switch_path)
    live_mode = "PAPER_BROKER_ON" if config.alpaca_paper.enabled else "PAPER_BROKER_OFF"
    details: list[str] = [
        f"symbol={config.alpaca.symbol}",
        f"paper_url={config.alpaca.paper_trading_url}",
        f"kill_switch_path={config.live.kill_switch_path}",
    ]

    public_api_status = "OK"
    try:
        request = Request(
            f"{config.alpaca.data_url}/v1beta3/crypto/{config.alpaca.crypto_location}/latest/orderbooks"
            f"?symbols={quote(config.alpaca.symbol, safe='')}",
            headers=_alpaca_auth_headers(config),
        )
        with urlopen(request, timeout=config.data.request_timeout_seconds) as response:
            response.read()
    except Exception as exc:
        public_api_status = "FALLA"
        details.append(f"public_api_error={exc}")

    api_key_present = bool(os.getenv(config.alpaca_paper.api_key_env))
    api_secret_present = bool(os.getenv(config.alpaca_paper.api_secret_env))
    details.append(f"{config.alpaca_paper.api_key_env}={'set' if api_key_present else 'missing'}")
    details.append(f"{config.alpaca_paper.api_secret_env}={'set' if api_secret_present else 'missing'}")

    if api_key_present and api_secret_present:
        try:
            account = get_alpaca_account(config)
            auth_status = "AUTH_OK"
            details.append(f"account_status={account.get('status') or 'unknown'}")
            details.append(f"trading_blocked={account.get('trading_blocked')}")
        except Exception as exc:
            auth_status = "AUTH_ERROR"
            details.append(f"account_error={exc}")
    else:
        auth_status = "SIN_AUTH"
        details.append("Faltan credenciales Alpaca para acceder a la cuenta paper.")

    if kill_switch_status == "ACTIVO":
        details.append("El kill switch local sigue activo; no deberias enviar ordenes aunque sea paper broker.")
    if not config.alpaca_paper.enabled:
        details.append("El broker paper de Alpaca esta deshabilitado en config.")
    return LiveCheckReport(
        venue="alpaca",
        live_mode=live_mode,
        kill_switch_status=kill_switch_status,
        public_api_status=public_api_status,
        auth_status=auth_status,
        details=details,
    )


def _check_mt5_live_stack(config: AppConfig) -> LiveCheckReport:
    kill_switch_status = _kill_switch_status(config.live.kill_switch_path)
    live_mode = "DEMO_ON" if config.mt5.enable_demo_trading else "DEMO_OFF"
    details: list[str] = [
        f"symbol={config.mt5.symbol}",
        f"timeframe={config.mt5.timeframe}",
        f"bars={config.mt5.bars}",
        f"terminal_path_env={config.mt5.terminal_path_env}",
    ]
    details.append(f"{config.mt5.login_env}={'set' if os.getenv(config.mt5.login_env) else 'missing'}")
    details.append(f"{config.mt5.password_env}={'set' if os.getenv(config.mt5.password_env) else 'missing'}")
    details.append(f"{config.mt5.server_env}={'set' if os.getenv(config.mt5.server_env) else 'missing'}")
    details.append(
        f"{config.mt5.terminal_path_env}={'set' if os.getenv(config.mt5.terminal_path_env) else 'missing'}"
    )

    public_api_status = "OK"
    auth_status = "SIN_AUTH"
    try:
        with Mt5Client(config.mt5).connect(require_auth=False) as client:
            terminal = client.terminal_info()
            details.append(f"terminal_name={terminal.get('name') or 'MetaTrader 5'}")
            details.append(f"terminal_connected={terminal.get('connected')}")
            account = client.account_info()
            if account:
                auth_status = "AUTH_OK"
                details.append(f"login={account.get('login')}")
                details.append(f"server={account.get('server')}")
                details.append(f"trade_allowed={account.get('trade_allowed')}")
                details.append(f"trade_expert={account.get('trade_expert')}")
                if config.mt5.require_demo_account and "demo" not in str(account.get("server") or "").lower():
                    auth_status = "AUTH_ERROR"
                    details.append("La cuenta conectada no parece demo; el bot la bloquea por seguridad.")
            else:
                details.append("El terminal esta abierto, pero no devolvio una cuenta activa.")
    except Exception as exc:
        public_api_status = "FALLA"
        details.append(f"terminal_error={exc}")

    if kill_switch_status == "ACTIVO":
        details.append("El kill switch local esta activo; no se debe enviar ninguna orden demo.")
    if not config.mt5.enable_demo_trading:
        details.append("El envio de ordenes MT5 demo sigue deshabilitado en config.")
    return LiveCheckReport(
        venue="mt5",
        live_mode=live_mode,
        kill_switch_status=kill_switch_status,
        public_api_status=public_api_status,
        auth_status=auth_status,
        details=details,
    )


def _check_kraken_live_stack(config: AppConfig) -> LiveCheckReport:
    kill_switch_status = _kill_switch_status(config.live.kill_switch_path)
    live_mode = (
        "DRY_RUN"
        if config.kraken_live.dry_run
        else ("LIVE_ON" if config.kraken_live.enabled else "LIVE_OFF")
    )
    details: list[str] = [
        f"pair={config.kraken.pair}",
        f"wsname={config.kraken.wsname}",
        f"kill_switch_path={config.live.kill_switch_path}",
    ]

    public_api_status = "OK"
    try:
        request = Request(
            f"{config.kraken.public_rest_url}/0/public/Time",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urlopen(request, timeout=config.data.request_timeout_seconds) as response:
            response.read()
    except Exception as exc:
        public_api_status = "FALLA"
        details.append(f"public_api_error={exc}")

    api_key_present = bool(os.getenv(config.kraken_live.api_key_env))
    api_secret_present = bool(os.getenv(config.kraken_live.api_secret_env))
    details.append(f"{config.kraken_live.api_key_env}={'set' if api_key_present else 'missing'}")
    details.append(f"{config.kraken_live.api_secret_env}={'set' if api_secret_present else 'missing'}")

    if api_key_present and api_secret_present:
        try:
            response = _kraken_private_request(config, "/0/private/Balance", {})
            errors = response.get("error") or []
            if errors:
                auth_status = "AUTH_ERROR"
                details.append(f"balance_error={','.join(errors)}")
            else:
                auth_status = "AUTH_OK"
                details.append("Balance query autenticada correctamente.")
        except Exception as exc:
            auth_status = "AUTH_ERROR"
            details.append(f"auth_exception={exc}")
    else:
        auth_status = "SIN_AUTH"
        details.append("Faltan credenciales Kraken para validar endpoints privados.")

    if kill_switch_status == "ACTIVO":
        details.append("El kill switch local esta activo; cualquier live debe seguir bloqueado.")
    if not config.kraken.enable_live_trading or not config.kraken_live.enabled:
        details.append("Kraken live sigue deshabilitado en config.")
    details.append(
        f"dead_man_timeout_seconds={config.kraken_live.dead_man_timeout_seconds}"
    )
    return LiveCheckReport(
        venue="kraken",
        live_mode=live_mode,
        kill_switch_status=kill_switch_status,
        public_api_status=public_api_status,
        auth_status=auth_status,
        details=details,
    )


def _kraken_private_request(
    config: AppConfig,
    path: str,
    payload: dict[str, str],
) -> dict[str, object]:
    api_key = os.getenv(config.kraken_live.api_key_env)
    api_secret = os.getenv(config.kraken_live.api_secret_env)
    if not api_key or not api_secret:
        raise RuntimeError("Faltan credenciales Kraken en el entorno.")

    nonce = str(time.time_ns())
    encoded_payload = urlencode({"nonce": nonce, **payload})
    signature = _kraken_signature(path, nonce, {"nonce": nonce, **payload}, api_secret)
    request = Request(
        f"{config.kraken.private_rest_url}{path}",
        data=encoded_payload.encode(),
        headers={
            "API-Key": api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=config.data.request_timeout_seconds) as response:
        return dict(json.load(response))


def _alpaca_request(
    config: AppConfig,
    path: str,
    *,
    method: str,
    payload: dict[str, object] | None = None,
    live: bool,
) -> object:
    if live and not config.alpaca.enable_live_trading:
        raise RuntimeError("Alpaca live sigue deshabilitado en config.")
    body = None
    headers = _alpaca_auth_headers(config)
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    base_url = config.alpaca.live_trading_url if live else config.alpaca.paper_trading_url
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=config.data.request_timeout_seconds) as response:
        response_body = response.read()
    if not response_body:
        return {}
    return json.loads(response_body)


def _alpaca_auth_headers(config: AppConfig) -> dict[str, str]:
    api_key = os.getenv(config.alpaca_paper.api_key_env)
    api_secret = os.getenv(config.alpaca_paper.api_secret_env)
    if not api_key or not api_secret:
        raise RuntimeError("Faltan credenciales Alpaca en el entorno.")
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }


def _kraken_signature(path: str, nonce: str, payload: dict[str, str], secret: str) -> str:
    encoded_payload = urlencode(payload)
    sha256 = hashlib.sha256()
    sha256.update((nonce + encoded_payload).encode())
    message = path.encode() + sha256.digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _kill_switch_status(path: str) -> str:
    return "ACTIVO" if Path(path).exists() else "INACTIVO"


def _assert_kraken_live_submission_allowed(config: AppConfig) -> None:
    if Path(config.live.kill_switch_path).exists():
        raise RuntimeError("El kill switch esta activo; no se puede enviar una orden live.")
    if not config.kraken.enable_live_trading or not config.kraken_live.enabled:
        raise RuntimeError("Kraken live sigue deshabilitado en config.")
    if config.kraken_live.dry_run:
        raise RuntimeError("Kraken live esta en DRY_RUN; desactivalo antes de enviar una orden real.")


def _assert_kraken_order_is_supported(order: OrderIntent) -> None:
    if order.market_type != "spot":
        raise RuntimeError("Solo se soportan ordenes spot de Kraken en esta capa live.")
    if order.symbol not in {"XBT/USD", "BTC/USD"}:
        raise RuntimeError(f"El símbolo {order.symbol} no esta soportado por este flujo live.")


def _assert_alpaca_order_is_supported(config: AppConfig, order: OrderIntent, *, live: bool) -> None:
    if Path(config.live.kill_switch_path).exists():
        raise RuntimeError("El kill switch esta activo; no se puede enviar una orden al broker.")
    if order.market_type != "spot":
        raise RuntimeError("Solo se soportan ordenes spot en esta capa Alpaca.")
    if order.symbol not in {config.alpaca.symbol, config.alpaca.legacy_symbol, "BTC/USD", "XBT/USD"}:
        raise RuntimeError(f"El simbolo {order.symbol} no esta soportado por este flujo Alpaca.")
    if not live and not config.alpaca_paper.enabled:
        raise RuntimeError("Alpaca paper broker sigue deshabilitado en config.")
    if live and not config.alpaca.enable_live_trading:
        raise RuntimeError("Alpaca live sigue deshabilitado en config.")


def _assert_mt5_order_is_supported(config: AppConfig, order: OrderIntent, *, live: bool) -> None:
    if Path(config.live.kill_switch_path).exists():
        raise RuntimeError("El kill switch esta activo; no se puede enviar una orden MT5.")
    if order.market_type != "mt5":
        raise RuntimeError("Solo se soportan ordenes MT5 en esta capa.")
    if order.symbol != config.mt5.symbol:
        raise RuntimeError(f"El simbolo {order.symbol} no coincide con la configuracion MT5 activa.")
    if live and not config.mt5.enable_demo_trading:
        raise RuntimeError("MT5 demo sigue deshabilitado en config.")


def _format_decimal(value: float, tick_size: str) -> str:
    tick = float(tick_size or 0.0)
    if tick <= 0:
        return f"{value:.2f}"
    decimals = max(0, len(tick_size.split(".")[1]) if "." in tick_size else 0)
    rounded = round(round(value / tick) * tick, decimals)
    return f"{rounded:.{decimals}f}"


def _format_size(value: float) -> str:
    text = f"{value:.8f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
