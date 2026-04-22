from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading_bot.config import Mt5Config, Mt5LayerConfig, Mt5StrategyConfig
from trading_bot.mt5_layers import build_mt5_layer_adjustments, decide_mt5_layer_entry
from trading_bot.strategy import Mt5XauScalpStrategy
from trading_bot.types import MarketSnapshot, Side
from trading_bot.xau_scalping import (
    OhlcBar,
    aggregate_bars,
    atr,
    build_completed_m5_lookup,
    default_gold_backtest_paths,
    ema,
    load_dukascopy_bars,
    rsi,
)


@dataclass(slots=True)
class Mt5BacktestTrade:
    ticket: int
    side: str
    trade_profile: str
    layer_index: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    pnl: float
    exit_reason: str


@dataclass(slots=True)
class Mt5BacktestStats:
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
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    core_trades: int
    opportunistic_trades: int
    core_win_rate: float
    opportunistic_win_rate: float
    max_open_layers: int
    max_long_layers: int
    max_short_layers: int
    layer_adjustments: int
    flip_closes: int


@dataclass(slots=True)
class Mt5BacktestReport:
    label: str
    stats: Mt5BacktestStats
    trades: list[Mt5BacktestTrade]


@dataclass(slots=True)
class _OpenLayer:
    ticket: int
    side: Side
    volume: float
    entry_time: datetime
    entry_index: int
    entry_price: float
    stop_loss: float
    take_profit: float
    trade_profile: str
    layer_index: int


def default_mt5_gold_backtest_paths(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    preferred = root / "download" / "xauusd-m1-bid-2025-10-22-2026-04-22.json"
    if preferred.exists():
        return [preferred]
    return default_gold_backtest_paths(root)


def load_bars_for_paths(paths: list[str | Path]) -> list[OhlcBar]:
    combined: list[OhlcBar] = []
    for raw_path in paths:
        combined.extend(load_dukascopy_bars(raw_path))
    combined.sort(key=lambda bar: bar.timestamp)
    return combined


def run_mt5_xau_backtest(
    bars_m1: list[OhlcBar],
    *,
    mt5_config: Mt5Config,
    strategy_config: Mt5StrategyConfig,
    layer_config: Mt5LayerConfig,
    balance: float,
    spread: float,
) -> Mt5BacktestReport:
    if not bars_m1:
        stats = _summarize_mt5_backtest(
            trades=[],
            starting_balance=balance,
            ending_balance=balance,
            max_drawdown=0.0,
            max_open_layers=0,
            max_long_layers=0,
            max_short_layers=0,
            layer_adjustments=0,
            flip_closes=0,
        )
        return Mt5BacktestReport(label="mt5_xau_backtest", stats=stats, trades=[])

    strategy = Mt5XauScalpStrategy(strategy_config)
    m5_bars = aggregate_bars(bars_m1, 5)
    closes_m1 = [bar.close for bar in bars_m1]
    highs_m1 = [bar.high for bar in bars_m1]
    lows_m1 = [bar.low for bar in bars_m1]
    closes_m5 = [bar.close for bar in m5_bars]
    m1_pullback = ema(closes_m1, strategy_config.pullback_period)
    m1_rsi = rsi(closes_m1, strategy_config.rsi_period)
    m1_atr = atr(bars_m1, strategy_config.atr_period)
    m5_fast = ema(closes_m5, strategy_config.fast_period)
    m5_slow = ema(closes_m5, strategy_config.slow_period)
    m5_lookup = build_completed_m5_lookup(bars_m1, m5_bars)

    balance_now = balance
    peak_equity = balance
    max_drawdown = 0.0
    trades: list[Mt5BacktestTrade] = []
    open_layers: list[_OpenLayer] = []
    next_ticket = 1
    max_open_layers = 0
    max_long_layers = 0
    max_short_layers = 0
    layer_adjustments = 0
    flip_closes = 0
    point = 0.01

    for index, bar in enumerate(bars_m1):
        exited: list[_OpenLayer] = []
        for layer in open_layers:
            if layer.entry_index >= index:
                continue
            exit_price, exit_reason = _maybe_exit_open_layer(layer, bar)
            if exit_price is None:
                continue
            balance_now += _pnl_for_layer(layer, exit_price, mt5_config)
            trades.append(
                Mt5BacktestTrade(
                    ticket=layer.ticket,
                    side=layer.side.value,
                    trade_profile=layer.trade_profile,
                    layer_index=layer.layer_index,
                    entry_time=layer.entry_time,
                    exit_time=bar.timestamp,
                    entry_price=layer.entry_price,
                    exit_price=exit_price,
                    stop_loss=layer.stop_loss,
                    take_profit=layer.take_profit,
                    pnl=_pnl_for_layer(layer, exit_price, mt5_config),
                    exit_reason=exit_reason,
                )
            )
            exited.append(layer)
        if exited:
            open_layers = [layer for layer in open_layers if layer not in exited]

        snapshot = _build_snapshot_from_bar(
            bars_m1,
            index,
            mt5_config=mt5_config,
            strategy_config=strategy_config,
            spread=spread,
            point=point,
            m1_pullback=m1_pullback,
            m1_rsi=m1_rsi,
            m1_atr=m1_atr,
            m5_fast=m5_fast,
            m5_slow=m5_slow,
            m5_lookup=m5_lookup,
        )
        if snapshot is None:
            equity = _mark_equity(balance_now, open_layers, bar.close, spread, mt5_config)
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)
            continue

        if open_layers:
            adjustment_count = _apply_layer_adjustments(layer_config, strategy_config, snapshot, open_layers)
            layer_adjustments += adjustment_count

        signal = strategy.evaluate(snapshot)
        if signal is not None:
            opposite_layers = [layer for layer in open_layers if layer.side is not signal.side]
            if opposite_layers and layer_config.close_opposite_on_signal:
                for layer in opposite_layers:
                    exit_price = snapshot.best_bid if layer.side is Side.BUY else snapshot.best_ask
                    pnl = _pnl_for_layer(layer, exit_price, mt5_config)
                    balance_now += pnl
                    trades.append(
                        Mt5BacktestTrade(
                            ticket=layer.ticket,
                            side=layer.side.value,
                            trade_profile=layer.trade_profile,
                            layer_index=layer.layer_index,
                            entry_time=layer.entry_time,
                            exit_time=snapshot.timestamp,
                            entry_price=layer.entry_price,
                            exit_price=exit_price,
                            stop_loss=layer.stop_loss,
                            take_profit=layer.take_profit,
                            pnl=pnl,
                            exit_reason="flip_close",
                        )
                    )
                    flip_closes += 1
                open_layers = [layer for layer in open_layers if layer.side is signal.side]

            positions_payload = [_layer_to_position_payload(layer, mt5_config.symbol) for layer in open_layers]
            decision = decide_mt5_layer_entry(
                layer_config,
                snapshot,
                signal.side,
                positions_payload,
                fallback_size=mt5_config.order_size_lots,
            )
            trade_profile = str(signal.features.get("trade_profile") or "core").lower()
            opportunistic_cap = max(int(strategy_config.opportunistic_max_layers_per_side or 0), 0)
            if trade_profile == "oportunista" and opportunistic_cap > 0 and decision.same_side_layers >= opportunistic_cap:
                decision.allowed = False

            order_size = _normalize_mt5_size(
                decision.requested_size if decision.requested_size > 0 else mt5_config.order_size_lots,
                snapshot,
            )
            if decision.allowed and order_size > 0:
                open_layers.append(
                    _OpenLayer(
                        ticket=next_ticket,
                        side=signal.side,
                        volume=order_size,
                        entry_time=snapshot.timestamp,
                        entry_index=index,
                        entry_price=snapshot.best_ask if signal.side is Side.BUY else snapshot.best_bid,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        trade_profile=trade_profile,
                        layer_index=decision.next_layer_index,
                    )
                )
                next_ticket += 1
                adjustment_count = _apply_layer_adjustments(layer_config, strategy_config, snapshot, open_layers)
                layer_adjustments += adjustment_count

        long_layers = sum(1 for layer in open_layers if layer.side is Side.BUY)
        short_layers = sum(1 for layer in open_layers if layer.side is Side.SELL)
        max_long_layers = max(max_long_layers, long_layers)
        max_short_layers = max(max_short_layers, short_layers)
        max_open_layers = max(max_open_layers, len(open_layers))
        equity = _mark_equity(balance_now, open_layers, bar.close, spread, mt5_config)
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

    if open_layers:
        final_bar = bars_m1[-1]
        final_bid = final_bar.close - (spread / 2.0)
        final_ask = final_bar.close + (spread / 2.0)
        for layer in open_layers:
            exit_price = final_bid if layer.side is Side.BUY else final_ask
            pnl = _pnl_for_layer(layer, exit_price, mt5_config)
            balance_now += pnl
            trades.append(
                Mt5BacktestTrade(
                    ticket=layer.ticket,
                    side=layer.side.value,
                    trade_profile=layer.trade_profile,
                    layer_index=layer.layer_index,
                    entry_time=layer.entry_time,
                    exit_time=final_bar.timestamp,
                    entry_price=layer.entry_price,
                    exit_price=exit_price,
                    stop_loss=layer.stop_loss,
                    take_profit=layer.take_profit,
                    pnl=pnl,
                    exit_reason="forced_close",
                )
            )
        final_equity = balance_now
        peak_equity = max(peak_equity, final_equity)
        max_drawdown = max(max_drawdown, peak_equity - final_equity)

    stats = _summarize_mt5_backtest(
        trades=trades,
        starting_balance=balance,
        ending_balance=balance_now,
        max_drawdown=max_drawdown,
        max_open_layers=max_open_layers,
        max_long_layers=max_long_layers,
        max_short_layers=max_short_layers,
        layer_adjustments=layer_adjustments,
        flip_closes=flip_closes,
    )
    return Mt5BacktestReport(label="mt5_xau_backtest", stats=stats, trades=trades)


def _build_snapshot_from_bar(
    bars_m1: list[OhlcBar],
    index: int,
    *,
    mt5_config: Mt5Config,
    strategy_config: Mt5StrategyConfig,
    spread: float,
    point: float,
    m1_pullback: list[float | None],
    m1_rsi: list[float | None],
    m1_atr: list[float | None],
    m5_fast: list[float | None],
    m5_slow: list[float | None],
    m5_lookup: list[int | None],
) -> MarketSnapshot | None:
    if index < 1:
        return None
    m5_index = m5_lookup[index]
    if m5_index is None or m5_index < 1:
        return None
    pullback_ema = m1_pullback[index]
    pullback_rsi = m1_rsi[index]
    atr_value = m1_atr[index]
    fast_ma = m5_fast[m5_index]
    slow_ma = m5_slow[m5_index]
    previous_fast_ma = m5_fast[m5_index - 1]
    previous_slow_ma = m5_slow[m5_index - 1]
    if None in {pullback_ema, pullback_rsi, atr_value, fast_ma, slow_ma, previous_fast_ma, previous_slow_ma}:
        return None

    bar = bars_m1[index]
    best_bid = bar.close - (spread / 2.0)
    best_ask = bar.close + (spread / 2.0)
    atr_points = float(atr_value) / point
    ma_gap_points = (float(fast_ma) - float(slow_ma)) / point
    return MarketSnapshot(
        market_id=f"mt5:{mt5_config.symbol}",
        token_id=mt5_config.symbol,
        question=f"{mt5_config.symbol} MT5",
        best_bid=best_bid,
        best_ask=best_ask,
        fair_probability=bar.close,
        volume_24h=0.0,
        timestamp=bar.timestamp.astimezone(UTC),
        source="mt5",
        market_type="mt5",
        symbol=mt5_config.symbol,
        book_depth=0.0,
        tick_size=f"{point:.2f}",
        size_precision=2,
        min_order_size=0.01,
        last_trade_price=bar.close,
        vwap_24h=bar.close,
        open_24h=bars_m1[max(index - 1440, 0)].open,
        contract_size=100.0,
        order_step_size=0.01,
        max_order_size=100.0,
        preferred_order_size=mt5_config.order_size_lots,
        context={
            "timeframe": mt5_config.timeframe,
            "entry_timeframe": strategy_config.entry_timeframe,
            "filter_timeframe": strategy_config.filter_timeframe,
            "strategy_name": strategy_config.name,
            "fast_ma": round(float(fast_ma), 2),
            "slow_ma": round(float(slow_ma), 2),
            "previous_fast_ma": round(float(previous_fast_ma), 2),
            "previous_slow_ma": round(float(previous_slow_ma), 2),
            "m5_fast_ema": round(float(fast_ma), 2),
            "m5_slow_ema": round(float(slow_ma), 2),
            "m5_prev_fast_ema": round(float(previous_fast_ma), 2),
            "m5_prev_slow_ema": round(float(previous_slow_ma), 2),
            "m1_pullback_ema": round(float(pullback_ema), 2),
            "m1_rsi": round(float(pullback_rsi), 4),
            "atr": round(float(atr_value), 2),
            "atr_points": round(atr_points, 4),
            "ma_gap_points": round(ma_gap_points, 4),
            "point": point,
            "spread_points": round(spread / point, 4),
            "last_close": round(bar.close, 2),
            "previous_close": round(bars_m1[index - 1].close, 2),
            "last_high": round(bar.high, 2),
            "last_low": round(bar.low, 2),
            "bars": index + 1,
        },
    )


def _layer_to_position_payload(layer: _OpenLayer, symbol: str) -> dict[str, object]:
    return {
        "ticket": layer.ticket,
        "type": 0 if layer.side is Side.BUY else 1,
        "volume": layer.volume,
        "price_open": layer.entry_price,
        "time": layer.entry_time,
        "sl": layer.stop_loss,
        "tp": layer.take_profit,
        "symbol": symbol,
    }


def _apply_layer_adjustments(
    layer_config: Mt5LayerConfig,
    strategy_config: Mt5StrategyConfig,
    snapshot: MarketSnapshot,
    open_layers: list[_OpenLayer],
) -> int:
    adjustments = build_mt5_layer_adjustments(
        layer_config,
        strategy_config,
        snapshot,
        [_layer_to_position_payload(layer, snapshot.symbol) for layer in open_layers],
    )
    updates = 0
    for adjustment in adjustments:
        for layer in open_layers:
            if layer.ticket != adjustment.ticket:
                continue
            if abs(layer.stop_loss - adjustment.stop_loss) > 1e-9 or abs(layer.take_profit - adjustment.take_profit) > 1e-9:
                layer.stop_loss = adjustment.stop_loss
                layer.take_profit = adjustment.take_profit
                updates += 1
    return updates


def _maybe_exit_open_layer(layer: _OpenLayer, bar: OhlcBar) -> tuple[float | None, str]:
    if layer.side is Side.BUY:
        stop_hit = bar.low <= layer.stop_loss
        target_hit = bar.high >= layer.take_profit
        if stop_hit and target_hit:
            return layer.stop_loss, "stop_first"
        if stop_hit:
            return layer.stop_loss, "stop_loss"
        if target_hit:
            return layer.take_profit, "take_profit"
        return None, ""

    stop_hit = bar.high >= layer.stop_loss
    target_hit = bar.low <= layer.take_profit
    if stop_hit and target_hit:
        return layer.stop_loss, "stop_first"
    if stop_hit:
        return layer.stop_loss, "stop_loss"
    if target_hit:
        return layer.take_profit, "take_profit"
    return None, ""


def _pnl_for_layer(layer: _OpenLayer, exit_price: float, mt5_config: Mt5Config) -> float:
    contract_size = 100.0
    if layer.side is Side.BUY:
        delta = exit_price - layer.entry_price
    else:
        delta = layer.entry_price - exit_price
    return delta * layer.volume * contract_size


def _mark_equity(
    balance: float,
    open_layers: list[_OpenLayer],
    mark_price: float,
    spread: float,
    mt5_config: Mt5Config,
) -> float:
    mark_bid = mark_price - (spread / 2.0)
    mark_ask = mark_price + (spread / 2.0)
    equity = balance
    for layer in open_layers:
        exit_price = mark_bid if layer.side is Side.BUY else mark_ask
        equity += _pnl_for_layer(layer, exit_price, mt5_config)
    return equity


def _normalize_mt5_size(size: float, snapshot: MarketSnapshot) -> float:
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


def _summarize_mt5_backtest(
    *,
    trades: list[Mt5BacktestTrade],
    starting_balance: float,
    ending_balance: float,
    max_drawdown: float,
    max_open_layers: int,
    max_long_layers: int,
    max_short_layers: int,
    layer_adjustments: int,
    flip_closes: int,
) -> Mt5BacktestStats:
    wins = sum(1 for trade in trades if trade.pnl > 0)
    losses = sum(1 for trade in trades if trade.pnl < 0)
    gross_profit = sum(max(trade.pnl, 0.0) for trade in trades)
    gross_loss = -sum(min(trade.pnl, 0.0) for trade in trades)
    trade_count = len(trades)
    win_rate = (wins / trade_count * 100.0) if trade_count else 0.0
    net_pnl = ending_balance - starting_balance
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = (net_pnl / trade_count) if trade_count else 0.0
    avg_win = (gross_profit / wins) if wins else 0.0
    avg_loss = (gross_loss / losses) if losses else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    core = [trade for trade in trades if trade.trade_profile == "core"]
    opportunistic = [trade for trade in trades if trade.trade_profile != "core"]
    core_wins = sum(1 for trade in core if trade.pnl > 0)
    opportunistic_wins = sum(1 for trade in opportunistic if trade.pnl > 0)
    return Mt5BacktestStats(
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
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff_ratio,
        core_trades=len(core),
        opportunistic_trades=len(opportunistic),
        core_win_rate=(core_wins / len(core) * 100.0) if core else 0.0,
        opportunistic_win_rate=(opportunistic_wins / len(opportunistic) * 100.0) if opportunistic else 0.0,
        max_open_layers=max_open_layers,
        max_long_layers=max_long_layers,
        max_short_layers=max_short_layers,
        layer_adjustments=layer_adjustments,
        flip_closes=flip_closes,
    )
