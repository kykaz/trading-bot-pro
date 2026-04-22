from __future__ import annotations

from datetime import UTC
from math import exp

from trading_bot.config import BtcStrategyConfig, Mt5StrategyConfig, StrategyConfig
from trading_bot.types import MarketSnapshot, Side, Signal
from trading_bot.xau_scalping import active_session_window


class EventValueStrategy:
    def __init__(self, config: StrategyConfig, min_edge: float) -> None:
        self.config = config
        self.min_edge = min_edge

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        market_price = snapshot.mid_price
        edge = snapshot.fair_probability - market_price
        abs_edge = abs(edge)
        min_edge = self.min_edge
        min_confidence = self.config.min_confidence

        if snapshot.source == "real":
            min_edge = max(self.config.min_edge_floor, self.min_edge * self.config.real_min_edge_multiplier)
            min_confidence = self.config.real_min_confidence

        if abs_edge < min_edge:
            return None
        if snapshot.spread > self.config.max_spread:
            return None

        spread_score = self._bounded_ratio(self.config.max_spread - snapshot.spread, self.config.max_spread)
        liquidity_score = self._bounded_ratio(snapshot.volume_24h, self.config.liquidity_target)
        depth_score = self._bounded_ratio(snapshot.book_depth, self.config.depth_target)
        imbalance_score = self._bounded_ratio(abs(snapshot.liquidity_imbalance), self.config.imbalance_target)
        extreme_distance = min(market_price, 1.0 - market_price)
        extreme_floor = min(self.config.extreme_price_cutoff, 1.0 - self.config.extreme_price_cutoff)
        extreme_score = self._bounded_ratio(extreme_distance, max(extreme_floor, 0.01))
        edge_score = self._bounded_ratio(abs_edge, max(min_edge * 2.5, 0.01))

        score = (
            edge_score * self.config.edge_weight
            + liquidity_score * self.config.liquidity_weight
            + spread_score * self.config.spread_weight
            + extreme_score * self.config.extreme_weight
            + depth_score * self.config.depth_weight
            + imbalance_score * self.config.imbalance_feature_weight
        )
        confidence = self._sigmoid(score, midpoint=self.config.default_confidence)
        if confidence < min_confidence:
            return None

        side = Side.BUY if edge > 0 else Side.SELL
        features = {
            "edge_score": round(edge_score, 4),
            "liquidity_score": round(liquidity_score, 4),
            "spread_score": round(spread_score, 4),
            "extreme_score": round(extreme_score, 4),
            "depth_score": round(depth_score, 4),
            "imbalance_score": round(imbalance_score, 4),
            "spread": round(snapshot.spread, 4),
            "volume_24h": round(snapshot.volume_24h, 2),
            "book_depth": round(snapshot.book_depth, 2),
            "liquidity_imbalance": round(snapshot.liquidity_imbalance, 4),
            "composite_score": round(score, 4),
        }
        reason = (
            f"fair_probability={snapshot.fair_probability:.3f} "
            f"vs market_price={market_price:.3f}, edge={edge:.3f}, "
            f"score={score:.3f}, spread={snapshot.spread:.3f}, "
            f"volume_24h={snapshot.volume_24h:.0f}, depth={snapshot.book_depth:.0f}, "
            f"imbalance={snapshot.liquidity_imbalance:.3f}"
        )
        return Signal(
            market_id=snapshot.market_id,
            side=side,
            confidence=confidence,
            expected_edge=abs_edge,
            fair_probability=snapshot.fair_probability,
            market_price=market_price,
            reason=reason,
            features=features,
            timestamp=snapshot.timestamp,
        )

    @staticmethod
    def _bounded_ratio(value: float, scale: float) -> float:
        if scale <= 0:
            return 0.0
        return max(0.0, min(value / scale, 1.0))

    @staticmethod
    def _sigmoid(score: float, midpoint: float) -> float:
        probability = 1.0 / (1.0 + exp(-6.0 * (score - 0.5)))
        return max(midpoint, min(0.99, probability))


class BtcUsdMicrostructureStrategy:
    def __init__(self, config: BtcStrategyConfig) -> None:
        self.config = config

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if snapshot.market_type != "spot":
            return None

        market_price = snapshot.mid_price
        if market_price <= 0:
            return None

        spread_bps = (snapshot.spread / market_price) * 10_000
        if spread_bps > self.config.max_spread_bps:
            return None

        microprice = self._microprice(snapshot)
        last_trade = snapshot.last_trade_price or market_price
        vwap_24h = snapshot.vwap_24h or market_price
        momentum = (last_trade - vwap_24h) / max(vwap_24h, 1e-9)
        fair_value = (
            microprice * self.config.microprice_weight
            + market_price * self.config.mid_weight
            + vwap_24h * self.config.vwap_weight
            + last_trade * self.config.last_trade_weight
        )
        fair_value *= 1 + (momentum * self.config.momentum_weight)
        fair_value *= 1 + (snapshot.liquidity_imbalance * self.config.imbalance_target * 0.5)

        edge = fair_value - market_price
        edge_bps = (edge / market_price) * 10_000
        abs_edge_bps = abs(edge_bps)
        if abs_edge_bps < self.config.min_edge_bps:
            return None

        depth_score = self._bounded_ratio(snapshot.book_depth, self.config.depth_target_usd)
        imbalance_score = self._bounded_ratio(abs(snapshot.liquidity_imbalance), self.config.imbalance_target)
        spread_score = max(0.0, min(1.0 - (spread_bps / max(self.config.max_spread_bps, 0.01)), 1.0))
        edge_score = self._bounded_ratio(abs_edge_bps, max(self.config.confidence_edge_bps, self.config.min_edge_bps))
        score = (
            edge_score
            + depth_score * self.config.confidence_depth_weight
            + imbalance_score * self.config.confidence_imbalance_weight
            + spread_score * self.config.confidence_spread_weight
        )
        confidence = max(self.config.min_confidence, min(0.99, self._sigmoid(score, 0.52)))
        if confidence < self.config.min_confidence:
            return None

        side = Side.BUY if edge > 0 else Side.SELL
        features = {
            "edge_bps": round(edge_bps, 4),
            "spread_bps": round(spread_bps, 4),
            "microprice": round(microprice, 4),
            "vwap_24h": round(vwap_24h, 4),
            "last_trade": round(last_trade, 4),
            "momentum_pct": round(momentum * 100.0, 4),
            "depth_usd": round(snapshot.book_depth, 2),
            "liquidity_imbalance": round(snapshot.liquidity_imbalance, 4),
            "composite_score": round(score, 4),
        }
        reason = (
            f"fair_value={fair_value:.2f} vs market_price={market_price:.2f}, "
            f"edge_bps={edge_bps:.2f}, spread_bps={spread_bps:.3f}, "
            f"microprice={microprice:.2f}, vwap_24h={vwap_24h:.2f}, depth={snapshot.book_depth:.0f}"
        )
        return Signal(
            market_id=snapshot.market_id,
            side=side,
            confidence=confidence,
            expected_edge=abs_edge_bps,
            fair_probability=fair_value,
            market_price=market_price,
            reason=reason,
            features=features,
            timestamp=snapshot.timestamp,
        )

    @staticmethod
    def _microprice(snapshot: MarketSnapshot) -> float:
        if snapshot.bid_size <= 0 or snapshot.ask_size <= 0:
            return snapshot.mid_price
        return (
            (snapshot.best_ask * snapshot.bid_size)
            + (snapshot.best_bid * snapshot.ask_size)
        ) / (snapshot.bid_size + snapshot.ask_size)

    @staticmethod
    def _bounded_ratio(value: float, scale: float) -> float:
        if scale <= 0:
            return 0.0
        return max(0.0, min(value / scale, 1.0))

    @staticmethod
    def _sigmoid(score: float, midpoint: float) -> float:
        probability = 1.0 / (1.0 + exp(-4.5 * (score - 0.9)))
        return max(midpoint, min(0.99, probability))


class Mt5TrendStrategy:
    def __init__(self, config: Mt5StrategyConfig) -> None:
        self.config = config

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if snapshot.market_type != "mt5":
            return None

        point = self._float_context(snapshot, "point")
        if point <= 0:
            return None

        spread_points = self._float_context(snapshot, "spread_points")
        if spread_points > self.config.max_spread_points:
            return None

        fast_ma = self._float_context(snapshot, "fast_ma")
        slow_ma = self._float_context(snapshot, "slow_ma")
        atr_points = self._float_context(snapshot, "atr_points")
        last_close = self._float_context(snapshot, "last_close") or snapshot.last_trade_price or snapshot.mid_price
        previous_close = self._float_context(snapshot, "previous_close") or last_close
        ma_gap_points = (fast_ma - slow_ma) / point
        slope_points = (last_close - previous_close) / point
        abs_ma_gap_points = abs(ma_gap_points)

        if atr_points < self.config.min_atr_points:
            return None
        if abs_ma_gap_points < self.config.min_ma_gap_points:
            return None

        bullish = ma_gap_points > 0 and slope_points >= 0
        bearish = ma_gap_points < 0 and slope_points <= 0
        if not bullish and not bearish:
            return None

        confidence_gap = min(abs_ma_gap_points / max(self.config.confidence_gap_points, 1.0), 1.0)
        confidence_atr = min(atr_points / max(self.config.min_atr_points, 1.0), 1.0)
        confidence_spread = max(0.0, min(1.0 - (spread_points / max(self.config.max_spread_points, 1.0)), 1.0))
        score = confidence_gap + (confidence_atr * self.config.confidence_atr_weight) + (
            confidence_spread * self.config.confidence_spread_weight
        )
        confidence = max(self.config.min_confidence, min(0.99, self._sigmoid(score, 0.52)))
        if confidence < self.config.min_confidence:
            return None

        side = Side.BUY if bullish else Side.SELL
        stop_distance = max(atr_points * point, point * 10)
        take_distance = stop_distance * 1.5
        entry_price = snapshot.best_ask if side is Side.BUY else snapshot.best_bid
        stop_loss = entry_price - stop_distance if side is Side.BUY else entry_price + stop_distance
        take_profit = entry_price + take_distance if side is Side.BUY else entry_price - take_distance

        features = {
            "ma_gap_points": round(ma_gap_points, 4),
            "atr_points": round(atr_points, 4),
            "spread_points": round(spread_points, 4),
            "slope_points": round(slope_points, 4),
            "composite_score": round(score, 4),
        }
        reason = (
            f"fast_ma={fast_ma:.5f} vs slow_ma={slow_ma:.5f}, "
            f"ma_gap_points={ma_gap_points:.2f}, atr_points={atr_points:.2f}, "
            f"spread_points={spread_points:.2f}, slope_points={slope_points:.2f}"
        )
        return Signal(
            market_id=snapshot.market_id,
            side=side,
            confidence=confidence,
            expected_edge=abs_ma_gap_points,
            fair_probability=fast_ma,
            market_price=snapshot.mid_price,
            reason=reason,
            features=features,
            timestamp=snapshot.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    @staticmethod
    def _float_context(snapshot: MarketSnapshot, key: str) -> float:
        value = snapshot.context.get(key, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _sigmoid(score: float, midpoint: float) -> float:
        probability = 1.0 / (1.0 + exp(-4.0 * (score - 0.85)))
        return max(midpoint, min(0.99, probability))


class Mt5XauScalpStrategy:
    def __init__(self, config: Mt5StrategyConfig) -> None:
        self.config = config

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if snapshot.market_type != "mt5":
            return None

        point = self._float_context(snapshot, "point")
        if point <= 0:
            return None

        timestamp = snapshot.timestamp.astimezone(UTC)
        session_window = active_session_window(
            timestamp,
            self.config.session_start_utc,
            self.config.session_end_utc,
            self.config.session_windows_utc,
        )

        trade_profile = "core"
        session_label = session_window.label if session_window is not None else "Fuera de ventana"
        max_spread_points = self.config.max_spread_points
        min_atr_points = self.config.min_atr_points
        min_ma_gap_points = self.config.min_ma_gap_points
        min_confidence = self.config.min_confidence
        rsi_threshold = self.config.rsi_threshold
        take_profit_atr = self.config.take_profit_atr
        stop_loss_atr = self.config.stop_loss_atr
        reclaim_points = max(self.config.core_reclaim_points, 0.0)
        confidence_center = 0.95

        if session_window is None:
            if self.config.trading_mode not in {"mixed", "always_on"}:
                return None
            trade_profile = "oportunista"
            max_spread_points = min(self.config.max_spread_points, self.config.opportunistic_max_spread_points)
            min_atr_points = max(self.config.min_atr_points, self.config.opportunistic_min_atr_points)
            min_ma_gap_points = max(self.config.min_ma_gap_points, self.config.opportunistic_min_ma_gap_points)
            min_confidence = max(self.config.min_confidence, self.config.opportunistic_min_confidence)
            rsi_threshold = min(self.config.rsi_threshold, self.config.opportunistic_rsi_threshold)
            take_profit_atr = self.config.opportunistic_take_profit_atr
            stop_loss_atr = self.config.opportunistic_stop_loss_atr
            reclaim_points = max(self.config.opportunistic_reclaim_points, 0.0)
            confidence_center = 1.08

        spread_points = self._float_context(snapshot, "spread_points")
        if spread_points > max_spread_points:
            return None

        m5_fast = self._float_context(snapshot, "m5_fast_ema")
        m5_slow = self._float_context(snapshot, "m5_slow_ema")
        m5_prev_fast = self._float_context(snapshot, "m5_prev_fast_ema")
        m5_prev_slow = self._float_context(snapshot, "m5_prev_slow_ema")
        pullback_ema = self._float_context(snapshot, "m1_pullback_ema")
        pullback_rsi = self._float_context(snapshot, "m1_rsi")
        atr = self._float_context(snapshot, "atr")
        atr_points = self._float_context(snapshot, "atr_points")
        last_close = self._float_context(snapshot, "last_close") or snapshot.last_trade_price or snapshot.mid_price
        last_high = self._float_context(snapshot, "last_high")
        last_low = self._float_context(snapshot, "last_low")
        ma_gap_points = (m5_fast - m5_slow) / point
        abs_ma_gap_points = abs(ma_gap_points)

        if atr_points < min_atr_points:
            return None
        if abs_ma_gap_points < min_ma_gap_points:
            return None

        trend_up = m5_fast > m5_slow and m5_fast >= m5_prev_fast and m5_slow >= m5_prev_slow
        trend_down = m5_fast < m5_slow and m5_fast <= m5_prev_fast and m5_slow <= m5_prev_slow
        touched_buy = last_low <= pullback_ema and last_close >= pullback_ema
        touched_sell = last_high >= pullback_ema and last_close <= pullback_ema
        reclaim_buy_points = max((last_close - pullback_ema) / point, 0.0)
        reclaim_sell_points = max((pullback_ema - last_close) / point, 0.0)

        if trend_up and touched_buy and pullback_rsi <= rsi_threshold:
            side = Side.BUY
            rsi_extreme = max(rsi_threshold - pullback_rsi, 0.0)
            reclaim_value = reclaim_buy_points
        elif trend_down and touched_sell and pullback_rsi >= (100.0 - rsi_threshold):
            side = Side.SELL
            rsi_extreme = max(pullback_rsi - (100.0 - rsi_threshold), 0.0)
            reclaim_value = reclaim_sell_points
        else:
            return None

        if reclaim_points > 0 and reclaim_value < reclaim_points:
            return None

        confidence_gap = min(abs_ma_gap_points / max(self.config.confidence_gap_points, min_ma_gap_points, 1.0), 1.0)
        confidence_atr = min(atr_points / max(min_atr_points, 1.0), 1.0)
        confidence_spread = max(0.0, min(1.0 - (spread_points / max(max_spread_points, 1.0)), 1.0))
        confidence_rsi = min(rsi_extreme / max(rsi_threshold, 1.0), 1.0)
        confidence_reclaim = min(reclaim_value / max(reclaim_points, 1.0), 1.0) if reclaim_points > 0 else 0.0
        score = (
            confidence_gap
            + (confidence_atr * self.config.confidence_atr_weight)
            + (confidence_spread * self.config.confidence_spread_weight)
            + (confidence_rsi * 0.25)
            + (confidence_reclaim * 0.22)
        )
        confidence = self._sigmoid(score, confidence_center)
        if confidence < min_confidence:
            return None

        stop_distance = max(atr * stop_loss_atr, point * 10)
        take_distance = max(atr * take_profit_atr, point * 4)
        entry_price = snapshot.best_ask if side is Side.BUY else snapshot.best_bid
        stop_loss = entry_price - stop_distance if side is Side.BUY else entry_price + stop_distance
        take_profit = entry_price + take_distance if side is Side.BUY else entry_price - take_distance

        features = {
            "session_window": session_label,
            "trade_profile": trade_profile,
            "ma_gap_points": round(ma_gap_points, 4),
            "atr_points": round(atr_points, 4),
            "spread_points": round(spread_points, 4),
            "pullback_ema": round(pullback_ema, 4),
            "pullback_rsi": round(pullback_rsi, 4),
            "reclaim_points": round(reclaim_value, 4),
            "last_close": round(last_close, 4),
            "composite_score": round(score, 4),
        }
        reason = (
            f"profile={trade_profile}, window={session_label}, "
            f"m5_fast={m5_fast:.3f} vs m5_slow={m5_slow:.3f}, "
            f"ma_gap_points={ma_gap_points:.2f}, atr_points={atr_points:.2f}, "
            f"spread_points={spread_points:.2f}, pullback_rsi={pullback_rsi:.2f}, "
            f"reclaim_points={reclaim_value:.2f}"
        )
        return Signal(
            market_id=snapshot.market_id,
            side=side,
            confidence=confidence,
            expected_edge=abs_ma_gap_points,
            fair_probability=m5_fast,
            market_price=snapshot.mid_price,
            reason=reason,
            features=features,
            timestamp=snapshot.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    @staticmethod
    def _float_context(snapshot: MarketSnapshot, key: str) -> float:
        value = snapshot.context.get(key, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _sigmoid(score: float, center: float) -> float:
        probability = 1.0 / (1.0 + exp(-4.2 * (score - center)))
        return max(0.01, min(0.99, probability))
