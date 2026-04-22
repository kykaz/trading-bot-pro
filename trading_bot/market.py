from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from trading_bot.config import (
    AlpacaConfig,
    AlpacaPaperConfig,
    DataConfig,
    KrakenConfig,
    Mt5Config,
    Mt5StrategyConfig,
    StrategyConfig,
)
from trading_bot.mt5 import Mt5Client
from trading_bot.types import MarketSnapshot


class MarketDataSource:
    def get_snapshots(self) -> list[MarketSnapshot]:
        raise NotImplementedError


@dataclass(slots=True)
class MockMarketDataSource(MarketDataSource):
    def get_snapshots(self) -> list[MarketSnapshot]:
        now = datetime.now(UTC)
        base_markets = [
            ("fed-cut-june", "Will the Fed cut rates by June?", 0.42, 0.36, 0.02),
            ("btc-100k", "Will BTC trade above 100k this quarter?", 0.36, 0.44, 0.03),
            ("election-seat", "Will party X win the special seat?", 0.58, 0.62, 0.02),
        ]
        snapshots: list[MarketSnapshot] = []
        for offset, (market_id, question, fair_probability, mid, spread) in enumerate(base_markets):
            snapshots.append(
                MarketSnapshot(
                    market_id=market_id,
                    token_id=f"{market_id}-yes",
                    question=question,
                    best_bid=max(mid - spread / 2, 0.01),
                    best_ask=min(mid + spread / 2, 0.99),
                    fair_probability=fair_probability,
                    volume_24h=50000 + offset * 15000,
                    timestamp=now + timedelta(seconds=offset),
                    source="mock",
                    market_type="binary",
                    symbol=market_id,
                    liquidity_imbalance=0.18 - offset * 0.05,
                    book_depth=9000 + offset * 2000,
                )
            )
        return snapshots


@dataclass(slots=True)
class PolymarketPublicDataSource(MarketDataSource):
    gamma_host: str
    clob_host: str
    data_config: DataConfig
    strategy_config: StrategyConfig

    def get_snapshots(self) -> list[MarketSnapshot]:
        markets = self._fetch_markets()
        token_ids = [market["yes_token_id"] for market in markets]
        books_by_token = self._fetch_books(token_ids)
        now = datetime.now(UTC)
        snapshots: list[MarketSnapshot] = []

        for market in markets:
            book = books_by_token.get(market["yes_token_id"])
            if not book:
                continue

            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                continue

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            bid_depth = sum(float(level["size"]) for level in bids[:5])
            ask_depth = sum(float(level["size"]) for level in asks[:5])
            imbalance = 0.0
            total_depth = bid_depth + ask_depth
            if total_depth > 0:
                imbalance = (bid_depth - ask_depth) / total_depth

            midpoint = (best_bid + best_ask) / 2.0
            fair_probability = max(
                min(midpoint + imbalance * self.strategy_config.imbalance_weight, 0.99),
                0.01,
            )

            snapshots.append(
                MarketSnapshot(
                    market_id=str(market["market_id"]),
                    token_id=str(market["yes_token_id"]),
                    question=str(market["question"]),
                    best_bid=best_bid,
                    best_ask=best_ask,
                    fair_probability=fair_probability,
                    volume_24h=float(market["volume_24h"]),
                    timestamp=now,
                    source="real",
                    market_type="binary",
                    symbol=str(market["market_id"]),
                    liquidity_imbalance=imbalance,
                    book_depth=total_depth,
                    tick_size=str(market["tick_size"]),
                    neg_risk=bool(market["neg_risk"]),
                )
            )

        tradable = [snapshot for snapshot in snapshots if snapshot.spread <= self.strategy_config.max_spread]
        ordered = tradable if tradable else snapshots
        ordered.sort(key=self._quality_score, reverse=True)
        return ordered[: self.data_config.market_limit]

    def _fetch_markets(self) -> list[dict[str, object]]:
        candidate_limit = max(self.data_config.market_limit * 20, 100)
        query = urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": str(candidate_limit),
            }
        )
        url = f"{self.gamma_host}/markets?{query}"
        payload = self._get_json(url)
        selected: list[dict[str, object]] = []

        for raw_market in payload:
            clob_token_ids = self._parse_token_ids(raw_market.get("clobTokenIds"))
            if not clob_token_ids:
                continue

            volume_24h = self._parse_float(
                raw_market.get("volume24hr")
                or raw_market.get("volume24Hr")
                or raw_market.get("volume24h")
                or 0.0
            )
            if volume_24h < self.data_config.min_volume_24h:
                continue

            market_id = str(raw_market.get("conditionId") or raw_market.get("id") or "")
            if not market_id:
                continue

            selected.append(
                {
                    "market_id": market_id,
                    "question": str(raw_market.get("question") or market_id),
                    "yes_token_id": clob_token_ids[0],
                    "volume_24h": volume_24h,
                    "tick_size": str(
                        raw_market.get("minimumTickSize")
                        or raw_market.get("minimum_tick_size")
                        or raw_market.get("tickSize")
                        or "0.01"
                    ),
                    "neg_risk": bool(raw_market.get("negRisk") or raw_market.get("neg_risk") or False),
                }
            )

        if not selected and self.data_config.min_volume_24h > 0:
            for raw_market in payload:
                clob_token_ids = self._parse_token_ids(raw_market.get("clobTokenIds"))
                if not clob_token_ids:
                    continue
                market_id = str(raw_market.get("conditionId") or raw_market.get("id") or "")
                if not market_id:
                    continue
                selected.append(
                    {
                        "market_id": market_id,
                        "question": str(raw_market.get("question") or market_id),
                        "yes_token_id": clob_token_ids[0],
                        "volume_24h": self._parse_float(
                            raw_market.get("volume24hr")
                            or raw_market.get("volume24Hr")
                            or raw_market.get("volume24h")
                            or 0.0
                        ),
                        "tick_size": str(
                            raw_market.get("minimumTickSize")
                            or raw_market.get("minimum_tick_size")
                            or raw_market.get("tickSize")
                            or "0.01"
                        ),
                        "neg_risk": bool(raw_market.get("negRisk") or raw_market.get("neg_risk") or False),
                    }
                )

        return selected

    def _quality_score(self, snapshot: MarketSnapshot) -> float:
        spread_scale = max(self.strategy_config.max_spread, 0.01)
        spread_score = max(0.0, min(1.0 - (snapshot.spread / spread_scale), 1.0))
        depth_score = min(snapshot.book_depth / max(self.strategy_config.depth_target, 1.0), 1.0)
        volume_score = min(snapshot.volume_24h / max(self.strategy_config.liquidity_target, 1.0), 1.0)
        imbalance_score = min(abs(snapshot.liquidity_imbalance) / max(self.strategy_config.imbalance_target, 0.01), 1.0)
        return (
            spread_score * 0.45
            + depth_score * 0.25
            + volume_score * 0.15
            + imbalance_score * 0.15
        )

    def _fetch_books(self, token_ids: list[str]) -> dict[str, dict[str, object]]:
        if not token_ids:
            return {}

        payload = self._post_json(
            f"{self.clob_host}/books",
            [{"token_id": token_id} for token_id in token_ids],
        )
        return {str(book["asset_id"]): book for book in payload}

    def _get_json(self, url: str) -> object:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=self.data_config.request_timeout_seconds) as response:
            return json.load(response)

    def _post_json(self, url: str, payload: object) -> object:
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=self.data_config.request_timeout_seconds) as response:
            return json.load(response)

    @staticmethod
    def _parse_token_ids(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return []
            if isinstance(decoded, list):
                return [str(item) for item in decoded]
        return []

    @staticmethod
    def _parse_float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass(slots=True)
class KrakenBtcUsdDataSource(MarketDataSource):
    kraken_config: KrakenConfig
    data_config: DataConfig

    def get_snapshots(self) -> list[MarketSnapshot]:
        pair_key, asset_pair = self._fetch_asset_pair()
        book = self._fetch_depth()
        ticker = self._fetch_ticker()

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return []

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        bid_size = float(bids[0][1])
        ask_size = float(asks[0][1])
        bid_depth = sum(float(level[0]) * float(level[1]) for level in bids[: self.kraken_config.book_levels])
        ask_depth = sum(float(level[0]) * float(level[1]) for level in asks[: self.kraken_config.book_levels])
        total_depth = bid_depth + ask_depth
        imbalance = ((bid_depth - ask_depth) / total_depth) if total_depth > 0 else 0.0

        last_trade = float(ticker["c"][0])
        vwap_24h = float(ticker["p"][1] if len(ticker["p"]) > 1 else ticker["p"][0])
        open_24h = float(ticker.get("o") or last_trade)
        volume_base = float(ticker["v"][1] if len(ticker["v"]) > 1 else ticker["v"][0])
        volume_quote = volume_base * last_trade
        min_order_size = max(
            self._parse_float(asset_pair.get("ordermin") or 0.0),
            self._safe_divide(self._parse_float(asset_pair.get("costmin") or 0.0), last_trade),
        )
        tick_size = str(asset_pair.get("tick_size") or "0.1")
        lot_decimals = int(asset_pair.get("lot_decimals") or 8)

        snapshot = MarketSnapshot(
            market_id=f"kraken:{pair_key}",
            token_id=pair_key,
            question=f"{asset_pair.get('wsname') or self.kraken_config.wsname} spot",
            best_bid=best_bid,
            best_ask=best_ask,
            fair_probability=(best_bid + best_ask) / 2.0,
            volume_24h=volume_quote,
            timestamp=datetime.now(UTC),
            source="btcusd",
            market_type="spot",
            symbol=str(asset_pair.get("wsname") or self.kraken_config.wsname),
            liquidity_imbalance=imbalance,
            book_depth=total_depth,
            tick_size=tick_size,
            size_precision=lot_decimals,
            min_order_size=min_order_size,
            last_trade_price=last_trade,
            vwap_24h=vwap_24h,
            open_24h=open_24h,
            bid_size=bid_size,
            ask_size=ask_size,
            neg_risk=False,
        )
        return [snapshot]

    def _fetch_asset_pair(self) -> tuple[str, dict[str, object]]:
        payload = self._get_json(
            f"{self.kraken_config.public_rest_url}/0/public/AssetPairs?pair={self.kraken_config.pair}"
        )
        result = payload.get("result") or {}
        if not result:
            raise ValueError(f"Kraken no devolvio metadata para {self.kraken_config.pair}.")
        pair_key = next(iter(result))
        return str(pair_key), dict(result[pair_key])

    def _fetch_depth(self) -> dict[str, object]:
        payload = self._get_json(
            f"{self.kraken_config.public_rest_url}/0/public/Depth?pair={self.kraken_config.pair}&count={self.kraken_config.book_levels}"
        )
        return dict(self._extract_single_result(payload))

    def _fetch_ticker(self) -> dict[str, object]:
        payload = self._get_json(
            f"{self.kraken_config.public_rest_url}/0/public/Ticker?pair={self.kraken_config.pair}"
        )
        return dict(self._extract_single_result(payload))

    def _get_json(self, url: str) -> dict[str, object]:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=self.data_config.request_timeout_seconds) as response:
            return dict(json.load(response))

    @staticmethod
    def _extract_single_result(payload: dict[str, object]) -> object:
        result = payload.get("result") or {}
        if not isinstance(result, dict) or not result:
            raise ValueError("Kraken devolvio una respuesta vacia.")
        return next(iter(result.values()))

    @staticmethod
    def _safe_divide(numerator: float, denominator: float) -> float:
        if denominator <= 0:
            return 0.0
        return numerator / denominator

    @staticmethod
    def _parse_float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass(slots=True)
class AlpacaBtcUsdDataSource(MarketDataSource):
    alpaca_config: AlpacaConfig
    alpaca_paper_config: AlpacaPaperConfig
    data_config: DataConfig

    def get_snapshots(self) -> list[MarketSnapshot]:
        symbol = self.alpaca_config.symbol
        orderbook_payload = self._get_json(
            f"{self.alpaca_config.data_url}/v1beta3/crypto/{self.alpaca_config.crypto_location}/latest/orderbooks"
            f"?symbols={quote(symbol, safe='')}"
        )
        snapshot_payload = self._get_json(
            f"{self.alpaca_config.data_url}/v1beta3/crypto/{self.alpaca_config.crypto_location}/snapshots"
            f"?symbols={quote(symbol, safe='')}"
        )
        asset = self._fetch_asset()

        orderbook = self._extract_symbol_payload(orderbook_payload, "orderbooks")
        snapshot_data = self._extract_symbol_payload(snapshot_payload, "snapshots")
        bids = list(orderbook.get("bids") or [])
        asks = list(orderbook.get("asks") or [])
        if not bids or not asks:
            return []

        best_bid_level = bids[0]
        best_ask_level = asks[0]
        best_bid = self._parse_float(best_bid_level.get("p") or best_bid_level.get("price"))
        best_ask = self._parse_float(best_ask_level.get("p") or best_ask_level.get("price"))
        bid_size = self._parse_float(best_bid_level.get("s") or best_bid_level.get("size"))
        ask_size = self._parse_float(best_ask_level.get("s") or best_ask_level.get("size"))
        bid_depth = sum(
            self._parse_float(level.get("p") or level.get("price")) * self._parse_float(level.get("s") or level.get("size"))
            for level in bids[:10]
        )
        ask_depth = sum(
            self._parse_float(level.get("p") or level.get("price")) * self._parse_float(level.get("s") or level.get("size"))
            for level in asks[:10]
        )
        total_depth = bid_depth + ask_depth
        imbalance = ((bid_depth - ask_depth) / total_depth) if total_depth > 0 else 0.0

        latest_trade = dict(snapshot_data.get("latestTrade") or snapshot_data.get("latest_trade") or {})
        daily_bar = dict(snapshot_data.get("dailyBar") or snapshot_data.get("daily_bar") or {})
        latest_quote = dict(snapshot_data.get("latestQuote") or snapshot_data.get("latest_quote") or {})
        last_trade = self._parse_float(latest_trade.get("p") or latest_trade.get("price") or ((best_bid + best_ask) / 2.0))
        vwap_24h = self._parse_float(daily_bar.get("vw") or daily_bar.get("vwap") or last_trade)
        open_24h = self._parse_float(daily_bar.get("o") or daily_bar.get("open") or last_trade)
        volume_24h = self._parse_float(daily_bar.get("v") or daily_bar.get("volume") or 0.0) * max(last_trade, 1.0)
        timestamp_text = (
            latest_quote.get("t")
            or latest_trade.get("t")
            or daily_bar.get("t")
            or datetime.now(UTC).isoformat()
        )

        min_order_size = self._parse_float(
            asset.get("min_order_size")
            or asset.get("min_trade_increment")
            or 0.0001
        )
        tick_size = str(asset.get("price_increment") or "0.01")
        size_precision = max(2, self._decimal_places(asset.get("min_trade_increment") or "0.0001"))

        snapshot = MarketSnapshot(
            market_id=f"alpaca:{symbol}",
            token_id=symbol,
            question=f"{symbol} spot",
            best_bid=best_bid,
            best_ask=best_ask,
            fair_probability=(best_bid + best_ask) / 2.0,
            volume_24h=volume_24h,
            timestamp=self._parse_timestamp(timestamp_text),
            source="btcusd",
            market_type="spot",
            symbol=symbol,
            liquidity_imbalance=imbalance,
            book_depth=total_depth,
            tick_size=tick_size,
            size_precision=size_precision,
            min_order_size=min_order_size,
            last_trade_price=last_trade,
            vwap_24h=vwap_24h,
            open_24h=open_24h,
            bid_size=bid_size,
            ask_size=ask_size,
            neg_risk=False,
        )
        return [snapshot]

    def _fetch_asset(self) -> dict[str, object]:
        symbol = quote(self.alpaca_config.symbol, safe="")
        payload = self._get_json(f"{self.alpaca_config.paper_trading_url}/v2/assets/{symbol}")
        if isinstance(payload, dict):
            return payload
        raise ValueError("Alpaca no devolvio metadata del activo BTC/USD.")

    def _get_json(self, url: str) -> object:
        request = Request(
            url,
            headers=self._auth_headers(),
        )
        with urlopen(request, timeout=self.data_config.request_timeout_seconds) as response:
            return json.load(response)

    def _auth_headers(self) -> dict[str, str]:
        api_key = os.getenv(self.alpaca_paper_config.api_key_env)
        api_secret = os.getenv(self.alpaca_paper_config.api_secret_env)
        if not api_key or not api_secret:
            raise RuntimeError("Faltan credenciales Alpaca en el entorno.")
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }

    def _extract_symbol_payload(self, payload: object, key: str) -> dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        bucket = payload.get(key) or {}
        if not isinstance(bucket, dict):
            return {}
        for candidate in (self.alpaca_config.symbol, self.alpaca_config.legacy_symbol):
            value = bucket.get(candidate)
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not value:
            return datetime.now(UTC)
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _decimal_places(value: object) -> int:
        text = str(value)
        if "." not in text:
            return 0
        return len(text.rstrip("0").split(".")[1])

    @staticmethod
    def _parse_float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass(slots=True)
class Mt5DataSource(MarketDataSource):
    mt5_config: Mt5Config
    mt5_strategy_config: Mt5StrategyConfig
    client: Mt5Client | object | None = None

    def get_snapshots(self) -> list[MarketSnapshot]:
        if self.client is None:
            with Mt5Client(self.mt5_config).connect(require_auth=False) as client:
                return [self._build_snapshot(client)]
        return [self._build_snapshot(self.client)]

    def _build_snapshot(self, client: Mt5Client | object) -> MarketSnapshot:
        symbol = self.mt5_config.symbol
        symbol_info = self._as_dict(client.symbol_info(symbol))
        tick = self._as_dict(client.symbol_tick(symbol))
        entry_rates = [
            self._as_dict(row)
            for row in client.copy_rates(
                symbol,
                self.mt5_strategy_config.entry_timeframe,
                max(self.mt5_config.bars, self.mt5_strategy_config.pullback_period + self.mt5_strategy_config.atr_period + 10),
            )
        ]
        filter_rates = [
            self._as_dict(row)
            for row in client.copy_rates(
                symbol,
                self.mt5_strategy_config.filter_timeframe,
                max(self.mt5_config.bars, self.mt5_strategy_config.slow_period + 10),
            )
        ]
        closes = [self._parse_float(row.get("close")) for row in entry_rates]
        highs = [self._parse_float(row.get("high")) for row in entry_rates]
        lows = [self._parse_float(row.get("low")) for row in entry_rates]
        opens = [self._parse_float(row.get("open")) for row in entry_rates]
        volumes = [self._parse_float(row.get("tick_volume")) for row in entry_rates]
        filter_closes = [self._parse_float(row.get("close")) for row in filter_rates]
        if not closes or not filter_closes:
            raise RuntimeError(f"MT5 no devolvio velas suficientes para {symbol}.")

        point = max(self._parse_float(symbol_info.get("point")), 1e-9)
        fast_series = self._ema_series(filter_closes, self.mt5_strategy_config.fast_period)
        slow_series = self._ema_series(filter_closes, self.mt5_strategy_config.slow_period)
        pullback_series = self._ema_series(closes, self.mt5_strategy_config.pullback_period)
        rsi_series = self._rsi_series(closes, self.mt5_strategy_config.rsi_period)
        atr = self._atr(highs, lows, closes, self.mt5_strategy_config.atr_period)
        fast_ma = self._latest_numeric(fast_series)
        slow_ma = self._latest_numeric(slow_series)
        previous_fast_ma = self._latest_numeric(fast_series[:-1])
        previous_slow_ma = self._latest_numeric(slow_series[:-1])
        pullback_ema = self._latest_numeric(pullback_series)
        pullback_rsi = self._latest_numeric(rsi_series)
        last_close = closes[-1]
        previous_close = closes[-2] if len(closes) > 1 else last_close
        last_low = lows[-1]
        last_high = highs[-1]
        ma_gap_points = (fast_ma - slow_ma) / point
        atr_points = atr / point
        spread = max(self._parse_float(tick.get("ask")) - self._parse_float(tick.get("bid")), 0.0)
        spread_points = spread / point

        digits = int(symbol_info.get("digits") or 5)
        size_precision = max(2, len(str(self.mt5_config.order_size_lots).split(".")[1]) if "." in str(self.mt5_config.order_size_lots) else 2)
        timestamp = tick.get("time")
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(UTC)
        question = str(symbol_info.get("description") or f"{symbol} MT5")

        return MarketSnapshot(
            market_id=f"mt5:{symbol}",
            token_id=symbol,
            question=question,
            best_bid=self._parse_float(tick.get("bid")),
            best_ask=self._parse_float(tick.get("ask")),
            fair_probability=last_close,
            volume_24h=sum(volumes),
            timestamp=timestamp,
            source="mt5",
            market_type="mt5",
            symbol=symbol,
            liquidity_imbalance=0.0,
            book_depth=sum(volumes[-20:]),
            tick_size=f"{point:.{digits}f}",
            size_precision=size_precision,
            min_order_size=self._parse_float(symbol_info.get("volume_min")),
            last_trade_price=self._parse_float(tick.get("last") or last_close),
            vwap_24h=sum(closes[-min(len(closes), 24):]) / max(min(len(closes), 24), 1),
            open_24h=opens[0] if opens else last_close,
            bid_size=0.0,
            ask_size=0.0,
            neg_risk=False,
            contract_size=max(self._parse_float(symbol_info.get("trade_contract_size")), 1.0),
            order_step_size=self._parse_float(symbol_info.get("volume_step")),
            max_order_size=self._parse_float(symbol_info.get("volume_max")),
            preferred_order_size=self.mt5_config.order_size_lots,
            context={
                "timeframe": self.mt5_config.timeframe,
                "entry_timeframe": self.mt5_strategy_config.entry_timeframe,
                "filter_timeframe": self.mt5_strategy_config.filter_timeframe,
                "strategy_name": self.mt5_strategy_config.name,
                "fast_ma": round(fast_ma, digits),
                "slow_ma": round(slow_ma, digits),
                "previous_fast_ma": round(previous_fast_ma, digits),
                "previous_slow_ma": round(previous_slow_ma, digits),
                "m5_fast_ema": round(fast_ma, digits),
                "m5_slow_ema": round(slow_ma, digits),
                "m5_prev_fast_ema": round(previous_fast_ma, digits),
                "m5_prev_slow_ema": round(previous_slow_ma, digits),
                "m1_pullback_ema": round(pullback_ema, digits),
                "m1_rsi": round(pullback_rsi, 4),
                "atr": round(atr, digits),
                "atr_points": round(atr_points, 4),
                "ma_gap_points": round(ma_gap_points, 4),
                "point": point,
                "spread_points": round(spread_points, 4),
                "last_close": round(last_close, digits),
                "previous_close": round(previous_close, digits),
                "last_high": round(last_high, digits),
                "last_low": round(last_low, digits),
                "bars": len(entry_rates),
            },
        )

    @staticmethod
    def _sma(values: list[float], period: int) -> float:
        window = values[-max(period, 1):]
        return sum(window) / max(len(window), 1)

    @staticmethod
    def _ema_series(values: list[float], period: int) -> list[float]:
        if not values:
            return []
        window = max(period, 1)
        if len(values) < window:
            return values[:]
        seed = sum(values[:window]) / window
        result = [seed]
        alpha = 2.0 / (window + 1.0)
        current = seed
        for value in values[window:]:
            current = (value * alpha) + (current * (1.0 - alpha))
            result.append(current)
        padding = [seed] * (window - 1)
        return padding + result

    @staticmethod
    def _rsi_series(values: list[float], period: int) -> list[float]:
        if len(values) <= period or period <= 0:
            return [50.0 for _ in values]
        result = [50.0 for _ in values]
        gains = 0.0
        losses = 0.0
        for index in range(1, period + 1):
            delta = values[index] - values[index - 1]
            gains += max(delta, 0.0)
            losses += max(-delta, 0.0)
        avg_gain = gains / period
        avg_loss = losses / period
        result[period] = Mt5DataSource._rsi_from_averages(avg_gain, avg_loss)
        for index in range(period + 1, len(values)):
            delta = values[index] - values[index - 1]
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period
            result[index] = Mt5DataSource._rsi_from_averages(avg_gain, avg_loss)
        return result

    @staticmethod
    def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
        if not highs or not lows or not closes:
            return 0.0
        true_ranges: list[float] = []
        for index in range(1, len(closes)):
            high = highs[index]
            low = lows[index]
            previous_close = closes[index - 1]
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        window = true_ranges[-max(period, 1):]
        return sum(window) / max(len(window), 1) if window else max(highs[-1] - lows[-1], 0.0)

    @staticmethod
    def _as_dict(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "_asdict"):
            return dict(value._asdict())
        return dict(value) if value else {}

    @staticmethod
    def _parse_float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _latest_numeric(values: list[float]) -> float:
        return float(values[-1]) if values else 0.0

    @staticmethod
    def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
