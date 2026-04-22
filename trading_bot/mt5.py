from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
import os
from typing import Any

from trading_bot.config import Mt5Config


def _import_mt5_module():
    try:
        import MetaTrader5 as module
    except Exception as exc:  # pragma: no cover - depends on local binary package
        raise RuntimeError(f"No se pudo importar MetaTrader5: {exc}") from exc
    return module


def _coerce_record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if hasattr(value, "_asdict") and callable(value._asdict):
        return dict(value._asdict())
    if hasattr(value, "__dict__"):
        return {
            key: raw
            for key, raw in vars(value).items()
            if not key.startswith("_")
        }
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        candidate = getattr(value, name)
        if callable(candidate):
            continue
        result[name] = candidate
    return result


def _coerce_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if isinstance(rows, list):
        return [_coerce_record(row) for row in rows]
    dtype = getattr(rows, "dtype", None)
    names = getattr(dtype, "names", None)
    if names:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            normalized.append({name: row[name].item() if hasattr(row[name], "item") else row[name] for name in names})
        return normalized
    return [_coerce_record(row) for row in list(rows)]


class Mt5Client(AbstractContextManager["Mt5Client"]):
    def __init__(self, config: Mt5Config, module: Any | None = None) -> None:
        self.config = config
        self.module = module or _import_mt5_module()
        self.connected = False

    def connect(self, require_auth: bool = False) -> "Mt5Client":
        kwargs: dict[str, Any] = {}
        terminal_path = os.getenv(self.config.terminal_path_env)
        if terminal_path:
            kwargs["path"] = terminal_path

        login = os.getenv(self.config.login_env)
        password = os.getenv(self.config.password_env)
        server = os.getenv(self.config.server_env)
        if login and password and server:
            try:
                kwargs["login"] = int(login)
            except ValueError as exc:
                raise RuntimeError("MT5_LOGIN debe ser numerico.") from exc
            kwargs["password"] = password
            kwargs["server"] = server
        elif login or password or server:
            raise RuntimeError("La configuracion MT5 esta incompleta; revisa login, password y server.")

        if not self.module.initialize(**kwargs):
            code, detail = self.module.last_error()
            raise RuntimeError(f"No se pudo inicializar MetaTrader 5 ({code}: {detail}).")
        self.connected = True
        if require_auth and not self.account_info():
            raise RuntimeError("MT5 no devolvio una cuenta activa; inicia sesion en el terminal o carga credenciales.")
        return self

    def close(self) -> None:
        if self.connected:
            self.module.shutdown()
            self.connected = False

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None

    def terminal_info(self) -> dict[str, Any]:
        return _coerce_record(self.module.terminal_info())

    def account_info(self) -> dict[str, Any]:
        return _coerce_record(self.module.account_info())

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        info = _coerce_record(self.module.symbol_info(symbol))
        if not info:
            raise RuntimeError(f"MT5 no devolvio metadata para {symbol}.")
        if not info.get("visible", False):
            self.module.symbol_select(symbol, True)
            info = _coerce_record(self.module.symbol_info(symbol))
        return info

    def symbol_tick(self, symbol: str) -> dict[str, Any]:
        tick = _coerce_record(self.module.symbol_info_tick(symbol))
        if not tick:
            raise RuntimeError(f"MT5 no devolvio tick para {symbol}.")
        tick_time = tick.get("time")
        if isinstance(tick_time, (int, float)):
            tick["time"] = datetime.fromtimestamp(float(tick_time), tz=UTC)
        return tick

    def copy_rates(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        timeframe_name = f"TIMEFRAME_{timeframe.upper()}"
        timeframe_value = getattr(self.module, timeframe_name, None)
        if timeframe_value is None:
            raise RuntimeError(f"Timeframe MT5 no soportado: {timeframe}.")
        rows = _coerce_rows(self.module.copy_rates_from_pos(symbol, timeframe_value, 0, count))
        if not rows:
            raise RuntimeError(f"MT5 no devolvio velas para {symbol} en {timeframe}.")
        for row in rows:
            if isinstance(row.get("time"), (int, float)):
                row["time"] = datetime.fromtimestamp(float(row["time"]), tz=UTC)
        return rows

    def positions_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.module.positions_get(symbol=symbol) if symbol else self.module.positions_get()
        return _coerce_rows(rows)

    def orders_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.module.orders_get(symbol=symbol) if symbol else self.module.orders_get()
        return _coerce_rows(rows)

    def history_deals(self, start: datetime, end: datetime, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.module.history_deals_get(start, end, group=symbol) if symbol else self.module.history_deals_get(start, end)
        payload = _coerce_rows(rows)
        for row in payload:
            if isinstance(row.get("time"), (int, float)):
                row["time"] = datetime.fromtimestamp(float(row["time"]), tz=UTC)
        return payload

    def order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self.module.order_check(request)
        payload = _coerce_record(result)
        if not payload:
            code, detail = self.module.last_error()
            raise RuntimeError(f"MT5 order_check fallo ({code}: {detail}).")
        return payload

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self.module.order_send(request)
        payload = _coerce_record(result)
        if not payload:
            code, detail = self.module.last_error()
            raise RuntimeError(f"MT5 order_send fallo ({code}: {detail}).")
        return payload

    def order_type(self, side: str) -> int:
        mapping = {
            "buy": getattr(self.module, "ORDER_TYPE_BUY"),
            "sell": getattr(self.module, "ORDER_TYPE_SELL"),
        }
        return mapping[side]

    def order_action_deal(self) -> int:
        return getattr(self.module, "TRADE_ACTION_DEAL")

    def order_action_sltp(self) -> int:
        return getattr(self.module, "TRADE_ACTION_SLTP")

    def time_gtc(self) -> int:
        return getattr(self.module, "ORDER_TIME_GTC")

    def filling_type(self, name: str) -> int:
        value = getattr(self.module, f"ORDER_FILLING_{name.upper()}", None)
        if value is None:
            raise RuntimeError(f"Tipo de filling MT5 no soportado: {name}.")
        return value
