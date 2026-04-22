from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading_bot.config import Mt5LayerConfig, Mt5StrategyConfig
from trading_bot.types import MarketSnapshot, Side


@dataclass(slots=True)
class Mt5Layer:
    ticket: int
    side: Side
    volume: float
    open_price: float
    opened_at: datetime | None
    stop_loss: float
    take_profit: float


@dataclass(slots=True)
class Mt5LayerBook:
    side: Side
    positions: list[Mt5Layer]

    @property
    def count(self) -> int:
        return len(self.positions)

    @property
    def total_volume(self) -> float:
        return sum(position.volume for position in self.positions)

    @property
    def average_price(self) -> float:
        total_volume = self.total_volume
        if total_volume <= 0:
            return 0.0
        weighted = sum(position.open_price * position.volume for position in self.positions)
        return weighted / total_volume

    @property
    def latest(self) -> Mt5Layer | None:
        if not self.positions:
            return None
        return max(
            self.positions,
            key=lambda position: (
                position.opened_at or datetime.min.replace(tzinfo=UTC),
                position.ticket,
            ),
        )


@dataclass(slots=True)
class Mt5LayerDecision:
    allowed: bool
    reason: str
    same_side_layers: int
    opposite_side_layers: int
    next_layer_index: int
    requested_size: float


@dataclass(slots=True)
class Mt5LayerAdjustment:
    ticket: int
    side: Side
    stop_loss: float
    take_profit: float
    reason: str


def build_mt5_layer_books(positions: list[dict[str, object]]) -> dict[Side, Mt5LayerBook]:
    buckets: dict[Side, list[Mt5Layer]] = {Side.BUY: [], Side.SELL: []}
    for raw_position in positions:
        side = _position_side(raw_position)
        if side is None:
            continue
        buckets[side].append(
            Mt5Layer(
                ticket=int(raw_position.get("ticket") or raw_position.get("identifier") or 0),
                side=side,
                volume=_safe_float(raw_position.get("volume")),
                open_price=_safe_float(raw_position.get("price_open")),
                opened_at=_coerce_position_time(raw_position),
                stop_loss=_safe_float(raw_position.get("sl")),
                take_profit=_safe_float(raw_position.get("tp")),
            )
        )

    return {
        side: Mt5LayerBook(side=side, positions=sorted(layers, key=_layer_sort_key))
        for side, layers in buckets.items()
        if layers
    }


def decide_mt5_layer_entry(
    layer_config: Mt5LayerConfig,
    snapshot: MarketSnapshot,
    side: Side,
    positions: list[dict[str, object]],
    *,
    fallback_size: float,
) -> Mt5LayerDecision:
    if not layer_config.enabled:
        return Mt5LayerDecision(
            allowed=True,
            reason="Layering disabled; using base order sizing.",
            same_side_layers=0,
            opposite_side_layers=0,
            next_layer_index=1,
            requested_size=max(fallback_size, 0.0),
        )

    books = build_mt5_layer_books(positions)
    same_book = books.get(side)
    opposite_book = books.get(_opposite_side(side))
    same_layers = same_book.count if same_book else 0
    opposite_layers = opposite_book.count if opposite_book else 0
    max_layers = layer_config.max_long_layers if side is Side.BUY else layer_config.max_short_layers
    requested_size = max(
        layer_config.base_size_lots if layer_config.base_size_lots > 0 else fallback_size,
        0.0,
    ) * max(layer_config.size_multiplier, 0.0) ** same_layers

    if opposite_layers and not layer_config.close_opposite_on_signal:
        return Mt5LayerDecision(
            allowed=False,
            reason="Hay capas abiertas en el lado opuesto.",
            same_side_layers=same_layers,
            opposite_side_layers=opposite_layers,
            next_layer_index=same_layers + 1,
            requested_size=requested_size,
        )

    if max_layers > 0 and same_layers >= max_layers:
        return Mt5LayerDecision(
            allowed=False,
            reason=f"Capas maximas alcanzadas para {side.value}: {same_layers}/{max_layers}.",
            same_side_layers=same_layers,
            opposite_side_layers=opposite_layers,
            next_layer_index=same_layers,
            requested_size=0.0,
        )

    current_volume = same_book.total_volume if same_book else 0.0
    if layer_config.max_total_volume > 0 and requested_size > layer_config.max_total_volume:
        requested_size = layer_config.max_total_volume
    if same_book and layer_config.max_total_volume > 0 and (same_book.total_volume + requested_size) > layer_config.max_total_volume:
        remaining = max(layer_config.max_total_volume - current_volume, 0.0)
        if remaining <= 0:
            return Mt5LayerDecision(
                allowed=False,
                reason="Volumen total de capas agotado para este lado.",
                same_side_layers=same_layers,
                opposite_side_layers=opposite_layers,
                next_layer_index=same_layers,
                requested_size=0.0,
            )
        requested_size = remaining

    latest = same_book.latest if same_book else None
    if latest and layer_config.min_minutes_between_layers > 0 and latest.opened_at:
        next_allowed = latest.opened_at + timedelta(minutes=layer_config.min_minutes_between_layers)
        if next_allowed > snapshot.timestamp.astimezone(UTC):
            minutes_left = max((next_allowed - snapshot.timestamp.astimezone(UTC)).total_seconds() / 60.0, 0.0)
            return Mt5LayerDecision(
                allowed=False,
                reason=f"Cooldown entre capas activo; faltan {minutes_left:.1f} min.",
                same_side_layers=same_layers,
                opposite_side_layers=opposite_layers,
                next_layer_index=same_layers + 1,
                requested_size=requested_size,
            )

    atr = max(_snapshot_float(snapshot, "atr"), _snapshot_float(snapshot, "point"))
    min_distance = max(atr * layer_config.min_price_distance_atr, 0.0)
    entry_price = snapshot.best_ask if side is Side.BUY else snapshot.best_bid
    if latest and min_distance > 0:
        distance = abs(entry_price - latest.open_price)
        direction_ok = (
            entry_price <= latest.open_price - min_distance
            if side is Side.BUY
            else entry_price >= latest.open_price + min_distance
        )
        if not direction_ok:
            return Mt5LayerDecision(
                allowed=False,
                reason=(
                    f"Precio demasiado cerca para una nueva capa: "
                    f"dist={distance:.3f} requerido={min_distance:.3f}."
                ),
                same_side_layers=same_layers,
                opposite_side_layers=opposite_layers,
                next_layer_index=same_layers + 1,
                requested_size=requested_size,
            )

    return Mt5LayerDecision(
        allowed=True,
        reason="Nueva capa permitida.",
        same_side_layers=same_layers,
        opposite_side_layers=opposite_layers,
        next_layer_index=same_layers + 1,
        requested_size=requested_size,
    )


def build_mt5_layer_adjustments(
    layer_config: Mt5LayerConfig,
    strategy_config: Mt5StrategyConfig,
    snapshot: MarketSnapshot,
    positions: list[dict[str, object]],
) -> list[Mt5LayerAdjustment]:
    if not layer_config.enabled:
        return []

    atr = max(_snapshot_float(snapshot, "atr"), 0.0)
    point = max(_snapshot_float(snapshot, "point"), 0.01)
    if atr <= 0:
        return []

    adjustments: list[Mt5LayerAdjustment] = []
    books = build_mt5_layer_books(positions)
    for side, book in books.items():
        if book.count < layer_config.break_even_after_layers:
            continue

        target_tp = 0.0
        tp_multiplier = strategy_config.take_profit_atr * (1.0 + (0.12 * max(book.count - 1, 0)))
        if side is Side.BUY:
            desired_sl = book.average_price + (atr * layer_config.break_even_buffer_atr)
            max_valid_sl = snapshot.best_bid - (point * 4)
            if max_valid_sl <= desired_sl:
                continue
            target_sl = min(desired_sl, max_valid_sl)
            if layer_config.harmonize_take_profit:
                target_tp = book.average_price + (atr * tp_multiplier)
        else:
            desired_sl = book.average_price - (atr * layer_config.break_even_buffer_atr)
            min_valid_sl = snapshot.best_ask + (point * 4)
            if min_valid_sl >= desired_sl:
                continue
            target_sl = max(desired_sl, min_valid_sl)
            if layer_config.harmonize_take_profit:
                target_tp = book.average_price - (atr * tp_multiplier)

        for position in book.positions:
            if _needs_adjustment(position, side, target_sl, target_tp, point):
                adjustments.append(
                    Mt5LayerAdjustment(
                        ticket=position.ticket,
                        side=side,
                        stop_loss=round(target_sl, 5),
                        take_profit=round(target_tp, 5) if target_tp > 0 else 0.0,
                        reason=f"Layer book {side.value} #{book.count} -> break-even management.",
                    )
                )
    return adjustments


def layer_status_lines(snapshot: MarketSnapshot, positions: list[dict[str, object]]) -> list[str]:
    books = build_mt5_layer_books(positions)
    lines: list[str] = []
    for side in (Side.BUY, Side.SELL):
        book = books.get(side)
        if not book:
            lines.append(f"{side.value}_layers=0")
            continue
        latest = book.latest
        latest_price = latest.open_price if latest else 0.0
        lines.append(
            f"{side.value}_layers={book.count} volume={book.total_volume:.2f} "
            f"avg={book.average_price:.3f} last={latest_price:.3f}"
        )
    return lines


def positions_for_opposite_close(signal_side: Side, positions: list[dict[str, object]]) -> list[dict[str, object]]:
    opposite = _opposite_side(signal_side)
    return [row for row in positions if _position_side(row) is opposite]


def _needs_adjustment(position: Mt5Layer, side: Side, target_sl: float, target_tp: float, point: float) -> bool:
    sl_gap = point * 2
    tp_gap = point * 2
    if side is Side.BUY:
        sl_needs = position.stop_loss < (target_sl - sl_gap)
        tp_needs = target_tp > 0 and (position.take_profit <= 0 or position.take_profit < (target_tp - tp_gap))
        return sl_needs or tp_needs
    sl_needs = position.stop_loss <= 0 or position.stop_loss > (target_sl + sl_gap)
    tp_needs = target_tp > 0 and (position.take_profit <= 0 or position.take_profit > (target_tp + tp_gap))
    return sl_needs or tp_needs


def _position_side(raw_position: dict[str, object]) -> Side | None:
    try:
        position_type = int(raw_position.get("type") or 0)
    except (TypeError, ValueError):
        return None
    if position_type == 0:
        return Side.BUY
    if position_type == 1:
        return Side.SELL
    return None


def _coerce_position_time(raw_position: dict[str, object]) -> datetime | None:
    for key in ("time", "time_update"):
        raw_value = raw_position.get(key)
        if isinstance(raw_value, datetime):
            return raw_value.astimezone(UTC)
        if isinstance(raw_value, (int, float)) and raw_value > 0:
            return datetime.fromtimestamp(float(raw_value), tz=UTC)
    return None


def _layer_sort_key(position: Mt5Layer) -> tuple[datetime, int]:
    return (position.opened_at or datetime.min.replace(tzinfo=UTC), position.ticket)


def _snapshot_float(snapshot: MarketSnapshot, key: str) -> float:
    try:
        return float(snapshot.context.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _opposite_side(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY
