"""Central config loader.

Everything network-addressable (bind host/port, public URLs, DB URL,
passwords) lives in config/config.yaml and is overridable by env var so the
exact same code runs locally (127.0.0.1) and on the Hetzner VM (0.0.0.0 /
real IP) with no code changes — only the YAML or env vars differ.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _env(name: str, default: str | None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class ServiceNetwork:
    host: str
    port: int


@dataclass(frozen=True)
class NetworkConfig:
    api: ServiceNetwork
    admin_api: ServiceNetwork
    website: ServiceNetwork
    api_base_url: str
    admin_api_base_url: str
    website_base_url: str


@dataclass(frozen=True)
class RiskConfig:
    default_leverage: float


@dataclass(frozen=True)
class ProductConfig:
    symbol: str
    underlying: str
    contract_size: float
    tick_size: float
    # Position cap is relative, not a fixed contract count: see
    # ledger.max_position_for. `leverage` is this instrument's own
    # override; every construction site falls back to
    # risk.default_leverage when the underlying config doesn't set one.
    leverage: float = 5.0
    starting_price: float = 75.0
    annual_volatility: float = 0.6
    annual_drift: float = 0.0
    # Every product accepts negative-priced limit orders. The BTC-ETH
    # spread needs this unconditionally (btc_index - eth_index is designed
    # to cross zero on an admin "invert" event); spot products and options
    # don't need it in the same structural way, but nothing downstream
    # (GBM step, Black-Scholes, ledger/PnL math) assumes a positive price
    # either, so there's no correctness reason to keep them floored.
    allow_negative_price: bool = True


@dataclass(frozen=True)
class AccountsConfig:
    starting_cash: float
    enforce_buying_power: bool
    freeze_on_zero_equity: bool


@dataclass(frozen=True)
class FeedConfig:
    stale_threshold_seconds: float
    sma_window: int
    reconnect_blend_seconds: float
    shock_decay_seconds: float


@dataclass(frozen=True)
class RateLimitConfig:
    requests_per_second: float
    burst: int
    ws_connections_per_key: int


@dataclass(frozen=True)
class SyntheticFeedConfig:
    tick_interval_seconds: float
    random_events_enabled: bool
    random_event_mean_interval_seconds: float


@dataclass(frozen=True)
class FeesConfig:
    maker_bps: float  # negative = rebate (maker gets paid)
    taker_bps: float


@dataclass(frozen=True)
class SpreadInstrumentConfig:
    enabled_default: bool
    symbol: str
    btc_product: str
    eth_product: str
    contract_size: float
    tick_size: float
    leverage: float = 5.0


@dataclass(frozen=True)
class OptionsChainConfig:
    id: str
    enabled_default: bool
    underlying: str
    window_seconds: float
    strikes_each_side: int
    strike_increment: float
    implied_volatility: float
    contract_size: float
    tick_size: float
    leverage: float = 5.0


@dataclass(frozen=True)
class FuturesConfig:
    enabled_default: bool
    underlyings: tuple[str, ...]
    window_seconds: float
    num_live: int
    tick_size: float
    leverage: float = 5.0


@dataclass(frozen=True)
class MMBotDefaults:
    legs: int
    min_spread_ticks: float
    delta_ticks: float
    quote_size: int
    skew_sensitivity: float
    requote_interval: float


@dataclass(frozen=True)
class MMBotsConfig:
    default: MMBotDefaults
    options: MMBotDefaults
    futures: MMBotDefaults


@dataclass(frozen=True)
class NoiseBotDefaults:
    count: int
    arrival_rate_per_sec: float
    max_size: int


@dataclass(frozen=True)
class NoiseBotsConfig:
    options: NoiseBotDefaults
    futures: NoiseBotDefaults


@dataclass(frozen=True)
class InsiderBotsConfig:
    enabled_default: bool
    count: int
    lead_seconds: float
    size: int
    hold_after_seconds: float


@dataclass(frozen=True)
class Config:
    exchange_name: str
    network: NetworkConfig
    database_url: str
    risk: RiskConfig
    products: dict[str, ProductConfig]
    accounts: AccountsConfig
    feed: FeedConfig
    rate_limit: RateLimitConfig
    synthetic_feed: SyntheticFeedConfig
    fees: FeesConfig
    admin_password: str
    website_password: str
    spread: SpreadInstrumentConfig
    options: dict[str, OptionsChainConfig]
    futures: FuturesConfig
    mm_bots: MMBotsConfig
    noise_bots: NoiseBotsConfig
    insider_bots: InsiderBotsConfig


def load_config(path: Path | str | None = None) -> Config:
    raw_path = Path(path) if path else Path(_env("SIM_ENGINE_CONFIG", str(DEFAULT_CONFIG_PATH)))
    with open(raw_path) as f:
        raw = yaml.safe_load(f)

    net = raw["network"]
    network = NetworkConfig(
        api=ServiceNetwork(
            host=_env("API_HOST", net["api"]["host"]),
            port=int(_env("API_PORT", str(net["api"]["port"]))),
        ),
        admin_api=ServiceNetwork(
            host=_env("ADMIN_API_HOST", net["admin_api"]["host"]),
            port=int(_env("ADMIN_API_PORT", str(net["admin_api"]["port"]))),
        ),
        website=ServiceNetwork(
            host=_env("WEBSITE_HOST", net["website"]["host"]),
            port=int(_env("WEBSITE_PORT", str(net["website"]["port"]))),
        ),
        api_base_url=_env("API_BASE_URL", net["public"]["api_base_url"]),
        admin_api_base_url=_env("ADMIN_API_BASE_URL", net["public"]["admin_api_base_url"]),
        website_base_url=_env("WEBSITE_BASE_URL", net["public"]["website_base_url"]),
    )

    risk_raw = raw.get("risk", {})
    risk = RiskConfig(default_leverage=float(risk_raw.get("default_leverage", 5.0)))

    products = {
        symbol: ProductConfig(
            symbol=symbol,
            underlying=p["underlying"],
            contract_size=float(p["contract_size"]),
            tick_size=float(p["tick_size"]),
            leverage=float(p.get("leverage", risk.default_leverage)),
            starting_price=float(p.get("starting_price", 75.0)),
            annual_volatility=float(p.get("annual_volatility", 0.6)),
            annual_drift=float(p.get("annual_drift", 0.0)),
        )
        for symbol, p in raw["products"].items()
    }

    accounts = AccountsConfig(
        starting_cash=float(raw["accounts"]["starting_cash"]),
        enforce_buying_power=bool(raw["accounts"]["enforce_buying_power"]),
        freeze_on_zero_equity=bool(raw["accounts"]["freeze_on_zero_equity"]),
    )

    feed = FeedConfig(
        stale_threshold_seconds=float(raw["feed"]["stale_threshold_seconds"]),
        sma_window=int(raw["feed"]["sma_window"]),
        reconnect_blend_seconds=float(raw["feed"]["reconnect_blend_seconds"]),
        shock_decay_seconds=float(raw["feed"]["shock_decay_seconds"]),
    )

    rate_limit = RateLimitConfig(
        requests_per_second=float(raw["rate_limit"]["requests_per_second"]),
        burst=int(raw["rate_limit"]["burst"]),
        ws_connections_per_key=int(raw["rate_limit"]["ws_connections_per_key"]),
    )

    synth_raw = raw.get("synthetic_feed", {})
    synthetic_feed = SyntheticFeedConfig(
        tick_interval_seconds=float(synth_raw.get("tick_interval_seconds", 1.0)),
        random_events_enabled=bool(synth_raw.get("random_events_enabled", True)),
        random_event_mean_interval_seconds=float(synth_raw.get("random_event_mean_interval_seconds", 240.0)),
    )

    fees_raw = raw.get("fees", {})
    fees = FeesConfig(
        maker_bps=float(fees_raw.get("maker_bps", -1.0)),
        taker_bps=float(fees_raw.get("taker_bps", 2.0)),
    )

    spread_raw = raw.get("spread", {})
    spread = SpreadInstrumentConfig(
        enabled_default=bool(spread_raw.get("enabled_default", False)),
        symbol=spread_raw.get("symbol", "BTC-ETH-MINI"),
        btc_product=spread_raw.get("btc_product", "BTC-MINI"),
        eth_product=spread_raw.get("eth_product", "ETH-MINI"),
        contract_size=float(spread_raw.get("contract_size", 1.0)),
        tick_size=float(spread_raw.get("tick_size", 0.10)),
        leverage=float(spread_raw.get("leverage", risk.default_leverage)),
    )

    options_raw = raw.get("options", {})
    options = {
        chain_id: OptionsChainConfig(
            id=chain_id,
            enabled_default=bool(c.get("enabled_default", False)),
            underlying=c.get("underlying", "BTC-MINI"),
            window_seconds=float(c.get("window_seconds", 900.0)),
            strikes_each_side=int(c.get("strikes_each_side", 5)),
            strike_increment=float(c.get("strike_increment", 1.0)),
            implied_volatility=float(c.get("implied_volatility", 0.55)),
            contract_size=float(c.get("contract_size", 1.0)),
            tick_size=float(c.get("tick_size", 0.01)),
            leverage=float(c.get("leverage", risk.default_leverage)),
        )
        for chain_id, c in options_raw.items()
    }

    futures_raw = raw.get("futures", {})
    futures = FuturesConfig(
        enabled_default=bool(futures_raw.get("enabled_default", False)),
        underlyings=tuple(futures_raw.get("underlyings", [])),
        window_seconds=float(futures_raw.get("window_seconds", 3600.0)),
        num_live=int(futures_raw.get("num_live", 5)),
        tick_size=float(futures_raw.get("tick_size", 0.10)),
        leverage=float(futures_raw.get("leverage", risk.default_leverage)),
    )

    def _mm_defaults(d: dict) -> MMBotDefaults:
        return MMBotDefaults(
            legs=int(d.get("legs", 3)),
            min_spread_ticks=float(d.get("min_spread_ticks", 2.0)),
            delta_ticks=float(d.get("delta_ticks", 1.0)),
            quote_size=int(d.get("quote_size", 3)),
            skew_sensitivity=float(d.get("skew_sensitivity", 0.05)),
            requote_interval=float(d.get("requote_interval", 1.5)),
        )

    mm_bots_raw = raw.get("mm_bots", {})
    mm_bots = MMBotsConfig(
        default=_mm_defaults(mm_bots_raw.get("default", {})),
        options=_mm_defaults(mm_bots_raw.get("options", mm_bots_raw.get("default", {}))),
        futures=_mm_defaults(mm_bots_raw.get("futures", mm_bots_raw.get("default", {}))),
    )

    def _noise_defaults(d: dict) -> NoiseBotDefaults:
        return NoiseBotDefaults(
            count=int(d.get("count", 3)),
            arrival_rate_per_sec=float(d.get("arrival_rate_per_sec", 0.3)),
            max_size=int(d.get("max_size", 2)),
        )

    noise_bots_raw = raw.get("noise_bots", {})
    noise_bots = NoiseBotsConfig(
        options=_noise_defaults(noise_bots_raw.get("options", {})),
        futures=_noise_defaults(noise_bots_raw.get("futures", {})),
    )

    insider_raw = raw.get("insider_bots", {})
    insider_bots = InsiderBotsConfig(
        enabled_default=bool(insider_raw.get("enabled_default", False)),
        count=int(insider_raw.get("count", 2)),
        lead_seconds=float(insider_raw.get("lead_seconds", 5.0)),
        size=int(insider_raw.get("size", 5)),
        hold_after_seconds=float(insider_raw.get("hold_after_seconds", 8.0)),
    )

    return Config(
        exchange_name=raw.get("exchange_name", "miniX"),
        network=network,
        database_url=_env("DATABASE_URL", raw["database"]["url"]),
        risk=risk,
        products=products,
        accounts=accounts,
        feed=feed,
        rate_limit=rate_limit,
        synthetic_feed=synthetic_feed,
        fees=fees,
        admin_password=_env("ADMIN_PASSWORD", raw["admin"]["password"]),
        website_password=_env("WEBSITE_PASSWORD", raw["website"]["password"]),
        spread=spread,
        options=options,
        futures=futures,
        mm_bots=mm_bots,
        noise_bots=noise_bots,
        insider_bots=insider_bots,
    )
