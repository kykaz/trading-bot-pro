from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


UTC = timezone.utc
DEFAULT_SESSION_WINDOWS_UTC = (
    "Londres|07:00-09:30",
    "Fixing AM|09:25-09:40",
    "Solape NY|12:20-15:30",
)


@dataclass(slots=True)
class SessionWindow:
    label: str
    start_minute_utc: int
    end_minute_utc: int

    @property
    def utc_range(self) -> str:
        return f"{self.start_minute_utc // 60:02d}:{self.start_minute_utc % 60:02d}-{self.end_minute_utc // 60:02d}:{self.end_minute_utc % 60:02d}"


@dataclass(slots=True)
class OhlcBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class XauScalpConfig:
    session_start_utc: int = 14
    session_end_utc: int = 18
    fast_ema_period: int = 20
    slow_ema_period: int = 50
    pullback_ema_period: int = 9
    rsi_period: int = 2
    rsi_threshold: float = 35.0
    atr_period: int = 14
    take_profit_atr: float = 0.4
    stop_loss_atr: float = 2.5
    spread: float = 0.25
    lot_size: float = 0.01
    contract_size: float = 100.0
    initial_balance: float = 100.0
    session_windows_utc: tuple[str, ...] = DEFAULT_SESSION_WINDOWS_UTC


@dataclass(slots=True)
class ClosedTrade:
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    pnl: float
    exit_reason: str


@dataclass(slots=True)
class BacktestStats:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    net_pnl: float
    max_drawdown: float
    expectancy: float
    ending_balance: float


@dataclass(slots=True)
class BacktestReport:
    label: str
    config: XauScalpConfig
    stats: BacktestStats
    trades: list[ClosedTrade]


PRESETS: dict[str, XauScalpConfig] = {
    "high-win": XauScalpConfig(
        session_start_utc=14,
        session_end_utc=17,
        rsi_threshold=38.0,
        take_profit_atr=0.5,
        stop_loss_atr=1.8,
        session_windows_utc=DEFAULT_SESSION_WINDOWS_UTC,
    ),
    "balanced": XauScalpConfig(
        session_start_utc=12,
        session_end_utc=17,
        rsi_threshold=25.0,
        take_profit_atr=0.6,
        stop_loss_atr=2.0,
        session_windows_utc=DEFAULT_SESSION_WINDOWS_UTC,
    ),
    "defensive": XauScalpConfig(
        session_start_utc=12,
        session_end_utc=16,
        rsi_threshold=25.0,
        take_profit_atr=0.5,
        stop_loss_atr=1.5,
        session_windows_utc=DEFAULT_SESSION_WINDOWS_UTC,
    ),
}


def load_dukascopy_bars(path: str | Path) -> list[OhlcBar]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    bars = [
        OhlcBar(
            timestamp=datetime.fromtimestamp(float(row["timestamp"]) / 1000.0, tz=UTC),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        for row in payload
    ]
    bars.sort(key=lambda item: item.timestamp)
    return bars


def aggregate_bars(bars: list[OhlcBar], minutes: int) -> list[OhlcBar]:
    if minutes <= 1:
        return list(bars)

    aggregated: list[OhlcBar] = []
    bucket: list[OhlcBar] = []
    current_key: datetime | None = None
    for bar in bars:
        key = bar.timestamp.replace(minute=(bar.timestamp.minute // minutes) * minutes, second=0, microsecond=0)
        if current_key is None or key != current_key:
            if bucket:
                aggregated.append(_merge_bucket(current_key, bucket))
            bucket = [bar]
            current_key = key
            continue
        bucket.append(bar)

    if bucket and current_key is not None:
        aggregated.append(_merge_bucket(current_key, bucket))
    return aggregated


def run_pullback_backtest(bars_m1: list[OhlcBar], config: XauScalpConfig) -> BacktestReport:
    m5_bars = aggregate_bars(bars_m1, 5)
    closes_m1 = [bar.close for bar in bars_m1]
    closes_m5 = [bar.close for bar in m5_bars]
    m1_ema = ema(closes_m1, config.pullback_ema_period)
    m1_rsi = rsi(closes_m1, config.rsi_period)
    m1_atr = atr(bars_m1, config.atr_period)
    m5_fast = ema(closes_m5, config.fast_ema_period)
    m5_slow = ema(closes_m5, config.slow_ema_period)
    m5_lookup = build_completed_m5_lookup(bars_m1, m5_bars)

    balance = config.initial_balance
    peak_balance = balance
    max_drawdown = 0.0
    trades: list[ClosedTrade] = []
    position: dict[str, object] | None = None

    for index, bar in enumerate(bars_m1):
        if position is not None and index >= int(position["entry_index"]):
            exit_price, exit_reason = _maybe_exit_position(position, bar)
            if exit_price is not None:
                pnl = _pnl_for_trade(
                    side=str(position["side"]),
                    entry_price=float(position["entry_price"]),
                    exit_price=exit_price,
                    lot_size=config.lot_size,
                    contract_size=config.contract_size,
                )
                balance += pnl
                trades.append(
                    ClosedTrade(
                        side=str(position["side"]),
                        entry_time=position["entry_time"],
                        exit_time=bar.timestamp,
                        entry_price=float(position["entry_price"]),
                        exit_price=exit_price,
                        stop_loss=float(position["stop_loss"]),
                        take_profit=float(position["take_profit"]),
                        pnl=pnl,
                        exit_reason=exit_reason,
                    )
                )
                peak_balance = max(peak_balance, balance)
                max_drawdown = max(max_drawdown, peak_balance - balance)
                position = None

        if position is not None or index >= len(bars_m1) - 1:
            continue
        if not in_session(bar.timestamp, config.session_start_utc, config.session_end_utc, config.session_windows_utc):
            continue

        m5_index = m5_lookup[index]
        if m5_index is None or m5_index < 1:
            continue

        fast = m5_fast[m5_index]
        slow = m5_slow[m5_index]
        prev_fast = m5_fast[m5_index - 1]
        prev_slow = m5_slow[m5_index - 1]
        pullback_ema = m1_ema[index]
        pullback_rsi = m1_rsi[index]
        pullback_atr = m1_atr[index]

        if None in {fast, slow, prev_fast, prev_slow, pullback_ema, pullback_rsi, pullback_atr}:
            continue

        trend_up = fast > slow and fast >= prev_fast and slow >= prev_slow
        trend_down = fast < slow and fast <= prev_fast and slow <= prev_slow
        touched_buy = bar.low <= pullback_ema and bar.close >= pullback_ema
        touched_sell = bar.high >= pullback_ema and bar.close <= pullback_ema
        next_open = bars_m1[index + 1].open

        if trend_up and touched_buy and pullback_rsi <= config.rsi_threshold:
            entry_price = next_open + (config.spread / 2.0)
            stop_loss = entry_price - (pullback_atr * config.stop_loss_atr)
            take_profit = entry_price + (pullback_atr * config.take_profit_atr)
            position = {
                "side": "buy",
                "entry_index": index + 1,
                "entry_time": bars_m1[index + 1].timestamp,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
            continue

        if trend_down and touched_sell and pullback_rsi >= (100.0 - config.rsi_threshold):
            entry_price = next_open - (config.spread / 2.0)
            stop_loss = entry_price + (pullback_atr * config.stop_loss_atr)
            take_profit = entry_price - (pullback_atr * config.take_profit_atr)
            position = {
                "side": "sell",
                "entry_index": index + 1,
                "entry_time": bars_m1[index + 1].timestamp,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }

    if position is not None and bars_m1:
        final_bar = bars_m1[-1]
        exit_price = final_bar.close
        pnl = _pnl_for_trade(
            side=str(position["side"]),
            entry_price=float(position["entry_price"]),
            exit_price=exit_price,
            lot_size=config.lot_size,
            contract_size=config.contract_size,
        )
        balance += pnl
        trades.append(
            ClosedTrade(
                side=str(position["side"]),
                entry_time=position["entry_time"],
                exit_time=final_bar.timestamp,
                entry_price=float(position["entry_price"]),
                exit_price=exit_price,
                stop_loss=float(position["stop_loss"]),
                take_profit=float(position["take_profit"]),
                pnl=pnl,
                exit_reason="forced_close",
            )
        )
        peak_balance = max(peak_balance, balance)
        max_drawdown = max(max_drawdown, peak_balance - balance)

    return BacktestReport(
        label="xauusd_pullback",
        config=config,
        stats=summarize_trades(trades, starting_balance=config.initial_balance, ending_balance=balance, max_drawdown=max_drawdown),
        trades=trades,
    )


def summarize_trades(
    trades: list[ClosedTrade],
    *,
    starting_balance: float,
    ending_balance: float,
    max_drawdown: float,
) -> BacktestStats:
    wins = sum(1 for trade in trades if trade.pnl > 0)
    losses = sum(1 for trade in trades if trade.pnl < 0)
    gross_profit = sum(max(trade.pnl, 0.0) for trade in trades)
    gross_loss = -sum(min(trade.pnl, 0.0) for trade in trades)
    trade_count = len(trades)
    win_rate = (wins / trade_count * 100.0) if trade_count else 0.0
    net_pnl = ending_balance - starting_balance
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = (net_pnl / trade_count) if trade_count else 0.0
    return BacktestStats(
        trades=trade_count,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        net_pnl=net_pnl,
        max_drawdown=max_drawdown,
        expectancy=expectancy,
        ending_balance=ending_balance,
    )


def run_reports_for_paths(paths: list[str | Path], config: XauScalpConfig) -> list[BacktestReport]:
    reports: list[BacktestReport] = []
    for raw_path in paths:
        path = Path(raw_path)
        report = run_pullback_backtest(load_dukascopy_bars(path), config)
        report.label = path.stem
        reports.append(report)
    return reports


def default_gold_backtest_paths(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    paths = [
        root / "download" / "xauusd-m1-bid-2026-02-15-2026-03-15.json",
        root / "download" / "xauusd-m1-bid-2026-03-15-2026-04-16.json",
    ]
    return [path for path in paths if path.exists()]


def build_completed_m5_lookup(bars_m1: list[OhlcBar], bars_m5: list[OhlcBar]) -> list[int | None]:
    lookup: list[int | None] = []
    completed_index = -1
    cursor = 0
    for bar in bars_m1:
        close_time = bar.timestamp + timedelta(minutes=1)
        while cursor < len(bars_m5) and bars_m5[cursor].timestamp + timedelta(minutes=5) <= close_time:
            completed_index = cursor
            cursor += 1
        lookup.append(completed_index if completed_index >= 0 else None)
    return lookup


def parse_session_windows(
    session_windows_utc: tuple[str, ...] | list[str] | None,
    fallback_start_utc: int,
    fallback_end_utc: int,
) -> list[SessionWindow]:
    raw_windows = tuple(session_windows_utc or ())
    if not raw_windows:
        return [
            SessionWindow(
                label="Sesion principal",
                start_minute_utc=fallback_start_utc * 60,
                end_minute_utc=fallback_end_utc * 60,
            )
        ]

    parsed: list[SessionWindow] = []
    for index, raw_window in enumerate(raw_windows, start=1):
        payload = str(raw_window).strip()
        if not payload:
            continue
        if "|" in payload:
            label, window_range = payload.split("|", 1)
            label = label.strip() or f"Ventana {index}"
        else:
            label = f"Ventana {index}"
            window_range = payload
        start_text, end_text = [piece.strip() for piece in window_range.split("-", 1)]
        start_minute = _parse_hhmm_to_minute(start_text)
        end_minute = _parse_hhmm_to_minute(end_text)
        parsed.append(
            SessionWindow(
                label=label,
                start_minute_utc=start_minute,
                end_minute_utc=end_minute,
            )
        )
    return parsed


def active_session_window(
    timestamp: datetime,
    session_start_utc: int,
    session_end_utc: int,
    session_windows_utc: tuple[str, ...] | list[str] | None = None,
) -> SessionWindow | None:
    minute_of_day = timestamp.astimezone(UTC).hour * 60 + timestamp.astimezone(UTC).minute
    for window in parse_session_windows(session_windows_utc, session_start_utc, session_end_utc):
        if window.start_minute_utc <= minute_of_day < window.end_minute_utc:
            return window
    return None


def in_session(
    timestamp: datetime,
    session_start_utc: int,
    session_end_utc: int,
    session_windows_utc: tuple[str, ...] | list[str] | None = None,
) -> bool:
    return active_session_window(timestamp, session_start_utc, session_end_utc, session_windows_utc) is not None


def describe_session_windows_utc(
    session_windows_utc: tuple[str, ...] | list[str] | None,
    fallback_start_utc: int,
    fallback_end_utc: int,
) -> str:
    windows = parse_session_windows(session_windows_utc, fallback_start_utc, fallback_end_utc)
    return ", ".join(window.utc_range for window in windows)


def _parse_hhmm_to_minute(value: str) -> int:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    return (hour * 60) + minute


def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        return [None for _ in values]
    result: list[float | None] = [None for _ in values]
    if len(values) < period:
        return result

    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    current = seed
    for index in range(period, len(values)):
        current = (values[index] * alpha) + (current * (1.0 - alpha))
        result[index] = current
    return result


def rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None for _ in values]
    if period <= 0 or len(values) <= period:
        return result

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)

    avg_gain = gains / period
    avg_loss = losses / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_from_averages(avg_gain, avg_loss)
    return result


def atr(bars: list[OhlcBar], period: int) -> list[float | None]:
    result: list[float | None] = [None for _ in bars]
    if period <= 0 or len(bars) < period:
        return result

    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))

    seed = sum(true_ranges[:period]) / period
    result[period - 1] = seed
    current = seed
    for index in range(period, len(bars)):
        current = ((current * (period - 1)) + true_ranges[index]) / period
        result[index] = current
    return result


def _merge_bucket(key: datetime, bucket: list[OhlcBar]) -> OhlcBar:
    return OhlcBar(
        timestamp=key,
        open=bucket[0].open,
        high=max(bar.high for bar in bucket),
        low=min(bar.low for bar in bucket),
        close=bucket[-1].close,
    )


def _maybe_exit_position(position: dict[str, object], bar: OhlcBar) -> tuple[float | None, str]:
    side = str(position["side"])
    stop_loss = float(position["stop_loss"])
    take_profit = float(position["take_profit"])
    if side == "buy":
        stop_hit = bar.low <= stop_loss
        target_hit = bar.high >= take_profit
        if stop_hit and target_hit:
            return stop_loss, "stop_first"
        if stop_hit:
            return stop_loss, "stop_loss"
        if target_hit:
            return take_profit, "take_profit"
        return None, ""

    stop_hit = bar.high >= stop_loss
    target_hit = bar.low <= take_profit
    if stop_hit and target_hit:
        return stop_loss, "stop_first"
    if stop_hit:
        return stop_loss, "stop_loss"
    if target_hit:
        return take_profit, "take_profit"
    return None, ""


def _pnl_for_trade(*, side: str, entry_price: float, exit_price: float, lot_size: float, contract_size: float) -> float:
    if side == "buy":
        delta = exit_price - entry_price
    else:
        delta = entry_price - exit_price
    return delta * lot_size * contract_size


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
