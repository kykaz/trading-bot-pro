from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_bot.config import AppConfig
from trading_bot.live import get_mt5_positions
from trading_bot.market import Mt5DataSource
from trading_bot.mt5 import Mt5Client
from trading_bot.mt5_layers import build_mt5_layer_books
from trading_bot.strategy import Mt5XauScalpStrategy
from trading_bot.xau_scalping import SessionWindow, active_session_window, parse_session_windows


@dataclass(slots=True)
class Mt5BenchmarkSnapshot:
    preset: str
    win_rate: float
    profit_factor: float
    net_pnl: float
    trades: int
    updated_at: str


@dataclass(slots=True)
class Mt5LiveWinRate:
    trades: int
    wins: int
    losses: int
    win_rate: float
    pnl: float


@dataclass(slots=True)
class Mt5SessionStatus:
    session_state: str
    session_window_utc: str
    session_window_local: str
    next_event_local: str
    setup_detected: bool
    setup_reason: str
    buy_layers: int
    sell_layers: int
    live_win_rate: Mt5LiveWinRate | None
    benchmark: Mt5BenchmarkSnapshot | None
    error: str | None = None


def build_mt5_session_status(
    config: AppConfig,
    *,
    timezone_name: str,
    benchmark_path: Path,
) -> Mt5SessionStatus:
    local_zone = ZoneInfo(timezone_name)
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone(local_zone)
    windows = parse_session_windows(
        config.mt5_strategy.session_windows_utc,
        config.mt5_strategy.session_start_utc,
        config.mt5_strategy.session_end_utc,
    )
    current_window = active_session_window(
        now_utc,
        config.mt5_strategy.session_start_utc,
        config.mt5_strategy.session_end_utc,
        config.mt5_strategy.session_windows_utc,
    )
    session_state = "ACTIVA" if current_window is not None else "VIGILANCIA" if config.mt5_strategy.trading_mode == "mixed" else "FUERA"
    session_window_utc = ", ".join(window.utc_range for window in windows)
    session_window_local = ", ".join(_window_to_local_range(window, now_local.date(), local_zone) for window in windows)
    next_event_local = _next_session_event(now_local, local_zone, windows, current_window)
    benchmark = load_mt5_benchmark(benchmark_path)

    try:
        snapshot = Mt5DataSource(config.mt5, config.mt5_strategy).get_snapshots()[0]
        signal = Mt5XauScalpStrategy(config.mt5_strategy).evaluate(snapshot)
        if signal is not None and str(signal.features.get("trade_profile") or "").lower() == "oportunista":
            session_state = "OPORTUNISTA"
        positions = get_mt5_positions(config)
        books = build_mt5_layer_books(positions)
        live_win_rate = compute_mt5_live_win_rate(config)
        return Mt5SessionStatus(
            session_state=session_state,
            session_window_utc=session_window_utc,
            session_window_local=session_window_local,
            next_event_local=next_event_local,
            setup_detected=signal is not None,
            setup_reason=signal.reason if signal is not None else "Sin setup valido en este minuto.",
            buy_layers=books.get(snapshot_side_buy(), None).count if books.get(snapshot_side_buy(), None) else 0,
            sell_layers=books.get(snapshot_side_sell(), None).count if books.get(snapshot_side_sell(), None) else 0,
            live_win_rate=live_win_rate,
            benchmark=benchmark,
        )
    except Exception as exc:
        return Mt5SessionStatus(
            session_state=session_state,
            session_window_utc=session_window_utc,
            session_window_local=session_window_local,
            next_event_local=next_event_local,
            setup_detected=False,
            setup_reason="No pude leer MT5 en este momento.",
            buy_layers=0,
            sell_layers=0,
            live_win_rate=None,
            benchmark=benchmark,
            error=str(exc),
        )


def load_mt5_benchmark(path: Path) -> Mt5BenchmarkSnapshot | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        return Mt5BenchmarkSnapshot(
            preset=str(payload["preset"]),
            win_rate=float(payload["win_rate"]),
            profit_factor=float(payload["profit_factor"]),
            net_pnl=float(payload["net_pnl"]),
            trades=int(payload["trades"]),
            updated_at=str(payload["updated_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_mt5_benchmark(path: Path, *, preset: str, win_rate: float, profit_factor: float, net_pnl: float, trades: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "preset": preset,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_pnl": net_pnl,
        "trades": trades,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def compute_mt5_live_win_rate(config: AppConfig, *, days: int = 14) -> Mt5LiveWinRate | None:
    start = datetime.now(UTC) - timedelta(days=days)
    end = datetime.now(UTC)
    with Mt5Client(config.mt5).connect(require_auth=True) as client:
        deals = client.history_deals(start, end)

    grouped: dict[int, float] = {}
    for row in deals:
        if str(row.get("symbol") or "") != config.mt5.symbol:
            continue
        entry = int(row.get("entry") or -1)
        if entry not in {1, 3}:
            continue
        position_id = int(row.get("position_id") or row.get("position") or row.get("order") or row.get("ticket") or 0)
        if position_id <= 0:
            continue
        pnl = float(row.get("profit") or 0.0) + float(row.get("commission") or 0.0) + float(row.get("swap") or 0.0) + float(row.get("fee") or 0.0)
        grouped[position_id] = grouped.get(position_id, 0.0) + pnl

    if not grouped:
        return None

    wins = sum(1 for pnl in grouped.values() if pnl > 0)
    losses = sum(1 for pnl in grouped.values() if pnl < 0)
    trades = len(grouped)
    pnl = sum(grouped.values())
    return Mt5LiveWinRate(
        trades=trades,
        wins=wins,
        losses=losses,
        win_rate=(wins / trades) * 100.0 if trades else 0.0,
        pnl=pnl,
    )


def _window_to_local_range(window: SessionWindow, local_date, local_zone: ZoneInfo) -> str:
    start_local = _minute_to_local_datetime(local_date, local_zone, window.start_minute_utc)
    end_local = _minute_to_local_datetime(local_date, local_zone, window.end_minute_utc)
    return f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')}"


def _minute_to_local_datetime(local_date, local_zone: ZoneInfo, minute_of_day_utc: int) -> datetime:
    hour = minute_of_day_utc // 60
    minute = minute_of_day_utc % 60
    return datetime.combine(local_date, time(hour, minute), tzinfo=UTC).astimezone(local_zone)


def _next_session_event(
    now_local: datetime,
    local_zone: ZoneInfo,
    windows: list[SessionWindow],
    current_window: SessionWindow | None,
) -> str:
    today = now_local.date()
    if current_window is not None:
        end_local = _minute_to_local_datetime(today, local_zone, current_window.end_minute_utc)
        return f"{current_window.label} cierra {end_local.strftime('%H:%M')}"

    future_opens = [
        (window, _minute_to_local_datetime(today, local_zone, window.start_minute_utc))
        for window in windows
        if now_local < _minute_to_local_datetime(today, local_zone, window.start_minute_utc)
    ]
    if future_opens:
        window, start_local = min(future_opens, key=lambda item: item[1])
        return f"{window.label} abre {start_local.strftime('%H:%M')}"

    next_open = _minute_to_local_datetime(today + timedelta(days=1), local_zone, windows[0].start_minute_utc)
    return f"{windows[0].label} abre {next_open.strftime('%d/%m %H:%M')}"


def snapshot_side_buy():
    from trading_bot.types import Side

    return Side.BUY


def snapshot_side_sell():
    from trading_bot.types import Side

    return Side.SELL
