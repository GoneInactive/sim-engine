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
class ProductConfig:
    symbol: str
    underlying: str
    contract_size: float
    max_position: int
    tick_size: float
    starting_price: float = 75.0
    annual_volatility: float = 0.6
    annual_drift: float = 0.0


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
class Config:
    network: NetworkConfig
    database_url: str
    products: dict[str, ProductConfig]
    accounts: AccountsConfig
    feed: FeedConfig
    rate_limit: RateLimitConfig
    synthetic_feed: SyntheticFeedConfig
    fees: FeesConfig
    admin_password: str
    website_password: str


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

    products = {
        symbol: ProductConfig(
            symbol=symbol,
            underlying=p["underlying"],
            contract_size=float(p["contract_size"]),
            max_position=int(p["max_position"]),
            tick_size=float(p["tick_size"]),
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

    return Config(
        network=network,
        database_url=_env("DATABASE_URL", raw["database"]["url"]),
        products=products,
        accounts=accounts,
        feed=feed,
        rate_limit=rate_limit,
        synthetic_feed=synthetic_feed,
        fees=fees,
        admin_password=_env("ADMIN_PASSWORD", raw["admin"]["password"]),
        website_password=_env("WEBSITE_PASSWORD", raw["website"]["password"]),
    )
