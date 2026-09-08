"""Generates a synthetic 2-hour price sample and writes it to CSV, standing
in for a real historical pull from the live exchange (`GET /book/{product}`
or the WebSocket stream, polled and logged over time).

Uses the exact same GBM step as `exchange.synthetic_feed.SyntheticFeedClient`
and the exact same event mechanics as `exchange.index_feed.IndexPriceService`
(the same shock/spread triggers the admin panel uses), so this is
authentically what the live system produces — not a separate reimplementation.

Run from the `presentation/` directory:
    python3 generate_data.py

Writes two files:
  - data/btc_eth_sample.csv         timestamp, btc, eth — the "live" data
  - data/btc_eth_sample_events.csv  when the scripted events happened
                                     (presenter's own reference — a real
                                     signal wouldn't have this)
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from exchange.config import FeedConfig, ProductConfig
from exchange.index_feed import IndexPriceService

SECONDS_PER_YEAR = 365.0 * 24 * 3600
DURATION_SECONDS = 2 * 3600
TICK_SECONDS = 1.0
START_TIMESTAMP = pd.Timestamp("2026-09-07 09:00:00")

# (time in minutes, event) — mix of single-leg shocks and spread events, so
# the spread ends up with real structure instead of being featureless noise.
SCHEDULED_EVENTS = [
    (25, "btc_shock"),
    (55, "eth_shock"),
    (80, "spread_widen"),
    (105, "spread_invert"),
]


def gbm_step(price: float, annual_drift: float, annual_vol: float, dt_seconds: float) -> float:
    """Mirrors exchange.synthetic_feed.SyntheticFeedClient._step exactly."""
    dt = dt_seconds / SECONDS_PER_YEAR
    z = random.gauss(0.0, 1.0)
    drift_term = (annual_drift - 0.5 * annual_vol * annual_vol) * dt
    shock_term = annual_vol * math.sqrt(dt) * z
    return price * math.exp(drift_term + shock_term)


def generate(seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    random.seed(seed)

    products = {
        "BTC-MINI": ProductConfig(
            symbol="BTC-MINI", underlying="BTC/USD", contract_size=0.001, max_position=15,
            tick_size=0.10, starting_price=77500.0, annual_volatility=0.55, annual_drift=0.0,
        ),
        "ETH-MINI": ProductConfig(
            symbol="ETH-MINI", underlying="ETH/USD", contract_size=0.03, max_position=15,
            tick_size=0.10, starting_price=2430.0, annual_volatility=0.70, annual_drift=0.0,
        ),
    }
    feed_cfg = FeedConfig(
        stale_threshold_seconds=7, sma_window=20, reconnect_blend_seconds=7,
        shock_decay_seconds=45,  # slower than the live default (7s) so shocks read as a
                                  # visible ramp/hold/fade shape at 2-hour zoom, not a single spike
    )

    svc = IndexPriceService(feed_cfg, products)
    prices = {symbol: cfg.starting_price for symbol, cfg in products.items()}

    rows = []
    event_log = []
    scheduled_by_second = {int(t * 60): label for t, label in SCHEDULED_EVENTS}

    for elapsed in range(0, DURATION_SECONDS + 1, int(TICK_SECONDS)):
        now = float(elapsed)
        for symbol, cfg in products.items():
            prices[symbol] = gbm_step(prices[symbol], cfg.annual_drift, cfg.annual_volatility, TICK_SECONDS)
            svc.on_raw_tick(symbol, prices[symbol], now)

        if elapsed in scheduled_by_second:
            kind = scheduled_by_second[elapsed]
            if kind == "btc_shock":
                svc.trigger_price_shock("BTC-MINI", target_offset=3.0, now=now, ramp_seconds=20, hold_seconds=180, name="btc_shock_1")
                event_log.append((elapsed, "BTC shock +$3"))
            elif kind == "eth_shock":
                svc.trigger_price_shock("ETH-MINI", target_offset=-2.5, now=now, ramp_seconds=20, hold_seconds=150, name="eth_shock_1")
                event_log.append((elapsed, "ETH shock -$2.50"))
            elif kind == "spread_widen":
                svc.trigger_spread_event("widen", magnitude=4.0, now=now, ramp_seconds=15, hold_seconds=240, name="spread_widen_1")
                event_log.append((elapsed, "spread widen"))
            elif kind == "spread_invert":
                svc.trigger_spread_event("invert", magnitude=2.0, now=now, ramp_seconds=15, hold_seconds=240, name="spread_invert_1")
                event_log.append((elapsed, "spread invert"))

        btc = svc.get_index_price("BTC-MINI", now)
        eth = svc.get_index_price("ETH-MINI", now)
        rows.append((elapsed, btc, eth))

    df = pd.DataFrame(rows, columns=["elapsed_s", "btc", "eth"])
    df["timestamp"] = pd.to_datetime(df["elapsed_s"], unit="s", origin=START_TIMESTAMP)
    df = df[["timestamp", "btc", "eth"]]

    events_df = pd.DataFrame(
        [(START_TIMESTAMP + pd.Timedelta(seconds=t), label) for t, label in event_log],
        columns=["timestamp", "label"],
    )
    return df, events_df


if __name__ == "__main__":
    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(exist_ok=True)

    df, events_df = generate()
    df.to_csv(data_dir / "btc_eth_sample.csv", index=False)
    events_df.to_csv(data_dir / "btc_eth_sample_events.csv", index=False)

    print(f"wrote {len(df):,} rows to {data_dir / 'btc_eth_sample.csv'}")
    print(f"wrote {len(events_df)} events to {data_dir / 'btc_eth_sample_events.csv'}")
