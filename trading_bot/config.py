from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(slots=True)
class BotConfig:
    mode: str
    starting_cash: float
    max_open_positions: int
    max_position_notional: float
    max_total_exposure: float
    max_daily_loss: float
    min_edge: float
    cooldown_minutes: int


@dataclass(slots=True)
class StrategyConfig:
    name: str
    default_confidence: float
    price_improvement_bps: int
    imbalance_weight: float
    imbalance_feature_weight: float
    imbalance_target: float
    min_confidence: float
    real_min_confidence: float
    max_spread: float
    liquidity_target: float
    depth_target: float
    extreme_price_cutoff: float
    edge_weight: float
    liquidity_weight: float
    spread_weight: float
    extreme_weight: float
    depth_weight: float
    real_min_edge_multiplier: float
    min_edge_floor: float


@dataclass(slots=True)
class PaperConfig:
    fee_bps: int
    slippage_bps: int
    persist_portfolio: bool
    portfolio_state_dir: str


@dataclass(slots=True)
class StorageConfig:
    backend: str
    sqlite_path: str


@dataclass(slots=True)
class DataConfig:
    source: str
    market_limit: int
    min_volume_24h: float
    request_timeout_seconds: int


@dataclass(slots=True)
class SpotConfig:
    venue: str


@dataclass(slots=True)
class PolymarketConfig:
    host: str
    chain_id: int
    enable_live_trading: bool
    gamma_host: str


@dataclass(slots=True)
class BtcStrategyConfig:
    name: str
    min_edge_bps: float
    min_confidence: float
    max_spread_bps: float
    depth_target_usd: float
    imbalance_target: float
    microprice_weight: float
    mid_weight: float
    vwap_weight: float
    last_trade_weight: float
    momentum_weight: float
    confidence_edge_bps: float
    confidence_depth_weight: float
    confidence_imbalance_weight: float
    confidence_spread_weight: float


@dataclass(slots=True)
class KrakenConfig:
    public_rest_url: str
    private_rest_url: str
    pair: str
    wsname: str
    book_levels: int
    enable_live_trading: bool


@dataclass(slots=True)
class KrakenLiveConfig:
    enabled: bool
    dry_run: bool
    api_key_env: str
    api_secret_env: str
    validate_orders: bool
    dead_man_timeout_seconds: int
    auto_arm_dead_man_switch: bool
    skip_if_open_orders: bool
    cancel_existing_before_submit: bool


@dataclass(slots=True)
class AlpacaConfig:
    data_url: str
    paper_trading_url: str
    live_trading_url: str
    symbol: str
    legacy_symbol: str
    crypto_location: str
    enable_live_trading: bool


@dataclass(slots=True)
class AlpacaPaperConfig:
    enabled: bool
    api_key_env: str
    api_secret_env: str
    cancel_existing_before_submit: bool
    skip_if_open_orders: bool
    close_positions_on_reset: bool


@dataclass(slots=True)
class Mt5Config:
    symbol: str
    timeframe: str
    bars: int
    order_size_lots: float
    deviation_points: int
    magic: int
    comment: str
    enable_demo_trading: bool
    require_demo_account: bool
    terminal_path_env: str
    login_env: str
    password_env: str
    server_env: str
    fill_type: str


@dataclass(slots=True)
class Mt5StrategyConfig:
    name: str
    fast_period: int
    slow_period: int
    pullback_period: int
    atr_period: int
    rsi_period: int
    rsi_threshold: float
    take_profit_atr: float
    stop_loss_atr: float
    entry_timeframe: str
    filter_timeframe: str
    session_start_utc: int
    session_end_utc: int
    min_atr_points: float
    min_ma_gap_points: float
    max_spread_points: float
    min_confidence: float
    confidence_gap_points: float
    confidence_atr_weight: float
    confidence_spread_weight: float
    core_reclaim_points: float = 0.0
    session_windows_utc: tuple[str, ...] = ()
    trading_mode: str = "core"
    opportunistic_min_confidence: float = 0.74
    opportunistic_min_atr_points: float = 16.0
    opportunistic_min_ma_gap_points: float = 26.0
    opportunistic_max_spread_points: float = 22.0
    opportunistic_rsi_threshold: float = 24.0
    opportunistic_take_profit_atr: float = 0.45
    opportunistic_stop_loss_atr: float = 1.6
    opportunistic_reclaim_points: float = 8.0
    opportunistic_max_layers_per_side: int = 1


@dataclass(slots=True)
class Mt5LayerConfig:
    enabled: bool
    base_size_lots: float
    max_long_layers: int
    max_short_layers: int
    min_minutes_between_layers: int
    min_price_distance_atr: float
    size_multiplier: float
    max_total_volume: float
    break_even_after_layers: int
    break_even_buffer_atr: float
    harmonize_take_profit: bool
    close_opposite_on_signal: bool


@dataclass(slots=True)
class VercelConfig:
    auto_publish_dashboard: bool
    scope: str
    dashboard_output: str
    production: bool
    publish_on_activity_only: bool
    min_publish_interval_minutes: int


@dataclass(slots=True)
class AlertsConfig:
    enabled: bool
    open_dashboard_on_alert: bool
    sound_on_alert: bool
    min_signals: int
    min_fills: int


@dataclass(slots=True)
class LiveConfig:
    enabled: bool
    dry_run: bool
    signature_type: int
    private_key_env: str
    api_key_env: str
    api_secret_env: str
    api_passphrase_env: str
    funder_env: str
    kill_switch_path: str


@dataclass(slots=True)
class AppConfig:
    bot: BotConfig
    strategy: StrategyConfig
    btc_strategy: BtcStrategyConfig
    mt5_strategy: "Mt5StrategyConfig"
    mt5_layers: "Mt5LayerConfig"
    data: DataConfig
    spot: SpotConfig
    paper: PaperConfig
    storage: StorageConfig
    polymarket: PolymarketConfig
    kraken: KrakenConfig
    kraken_live: KrakenLiveConfig
    alpaca: AlpacaConfig
    alpaca_paper: AlpacaPaperConfig
    mt5: Mt5Config
    vercel: VercelConfig
    alerts: AlertsConfig
    readiness: "ReadinessConfig"
    live: LiveConfig


@dataclass(slots=True)
class ReadinessConfig:
    lookback_runs: int
    min_real_runs: int
    min_real_fills: int
    min_positive_run_rate: float
    max_run_drawdown: float
    max_zero_fill_streak: int


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    mt5_strategy = dict(data["mt5_strategy"])
    mt5_strategy["session_windows_utc"] = tuple(mt5_strategy.get("session_windows_utc", ()))

    return AppConfig(
        bot=BotConfig(**data["bot"]),
        strategy=StrategyConfig(**data["strategy"]),
        btc_strategy=BtcStrategyConfig(**data["btc_strategy"]),
        mt5_strategy=Mt5StrategyConfig(**mt5_strategy),
        mt5_layers=Mt5LayerConfig(**data["mt5_layers"]),
        data=DataConfig(**data["data"]),
        spot=SpotConfig(**data["spot"]),
        paper=PaperConfig(**data["paper"]),
        storage=StorageConfig(**data["storage"]),
        polymarket=PolymarketConfig(**data["polymarket"]),
        kraken=KrakenConfig(**data["kraken"]),
        kraken_live=KrakenLiveConfig(**data["kraken_live"]),
        alpaca=AlpacaConfig(**data["alpaca"]),
        alpaca_paper=AlpacaPaperConfig(**data["alpaca_paper"]),
        mt5=Mt5Config(**data["mt5"]),
        vercel=VercelConfig(**data["vercel"]),
        alerts=AlertsConfig(**data["alerts"]),
        readiness=ReadinessConfig(**data["readiness"]),
        live=LiveConfig(**data["live"]),
    )
