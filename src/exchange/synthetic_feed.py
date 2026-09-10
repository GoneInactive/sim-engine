"""Synthetic index price generator, replacing the live Kraken WS feed.

build-spec.md originally called for real Kraken BTC/USD and ETH/USD prices
as the index (§4.1). In practice the live feed proved too unreliable for a
multi-day unattended run, so the whole exchange now runs on a fully
synthetic price process instead — this module is the only thing that
changed; everything downstream (IndexPriceService staleness/fallback/
blend, admin shock/drift/spread events, MM bot quoting) is unaware of the
swap because it still just calls `on_raw_tick(product, underlying_price,
now)`, the exact same interface the Kraken client used.

Two independent pieces:
  - `SyntheticFeedClient`: ticks each product's "underlying" price forward
    with geometric Brownian motion (continuous, no unrealistic jumps,
    standard model for an asset price) — a plain random-walk/SMA-hold
    would just flatline without admin intervention, which makes for a
    boring multi-day book.
  - `RandomEventScheduler`: fires the *same* admin-triggerable events
    (price shock, bull/bear drift, liquidity event, BTC/ETH spread) on its
    own Poisson-arrival timer, independent of anything the admin presses,
    so the book has some organic life to it between manual interventions.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
import random
import time

from .bots import BotManager
from .config import Config
from .index_feed import IndexPriceService

logger = logging.getLogger("exchange.synthetic_feed")

SECONDS_PER_YEAR = 365.0 * 24 * 3600


class SyntheticFeedClient:
    def __init__(self, config: Config, index_service: IndexPriceService, params: dict[str, dict] | None = None):
        self.config = config
        self.index_service = index_service
        self.prices: dict[str, float] = {
            symbol: cfg.starting_price for symbol, cfg in config.products.items()
        }
        # Read fresh every tick, not baked in at construction, so an admin
        # volatility/drift change (state.set_synthetic_params) takes effect
        # immediately without a restart. Defaults to the static config
        # values when no live params dict is supplied (e.g. in tests).
        self.params: dict[str, dict] = params if params is not None else {
            symbol: {"annual_drift": cfg.annual_drift, "annual_volatility": cfg.annual_volatility}
            for symbol, cfg in config.products.items()
        }
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _step(self, price: float, annual_drift: float, annual_vol: float, dt_seconds: float) -> float:
        dt = dt_seconds / SECONDS_PER_YEAR
        z = random.gauss(0.0, 1.0)
        drift_term = (annual_drift - 0.5 * annual_vol * annual_vol) * dt
        shock_term = annual_vol * math.sqrt(dt) * z
        return price * math.exp(drift_term + shock_term)

    async def run(self) -> None:
        interval = self.config.synthetic_feed.tick_interval_seconds
        logger.info("synthetic_feed: starting, tick_interval=%.2fs", interval)
        spread_cfg = self.config.spread
        while not self._stop:
            now = time.time()
            for symbol in self.config.products:
                p = self.params[symbol]
                self.prices[symbol] = self._step(
                    self.prices[symbol], p["annual_drift"], p["annual_volatility"], interval
                )
                self.index_service.on_raw_tick(symbol, self.prices[symbol], now)

            # BTC-ETH mini spread: derived, not its own GBM walk — its
            # index price is just BTC's index minus ETH's (both already
            # contract-notional-scaled), computed fresh from whatever the
            # two legs just ticked to (including any admin shock/drift
            # offsets already layered onto them).
            btc_index = self.index_service.get_index_price(spread_cfg.btc_product, now)
            eth_index = self.index_service.get_index_price(spread_cfg.eth_product, now)
            if btc_index is not None and eth_index is not None:
                self.index_service.on_raw_tick(spread_cfg.symbol, btc_index - eth_index, now)

            await asyncio.sleep(interval)


class RandomEventScheduler:
    """Fires random admin-style events on its own timer, independent of
    the admin panel. Same underlying mechanisms as api_admin.py's
    /events/* and /bots/noise routes, just self-triggered.

    The kind/product/direction of the *next* event is committed up front
    (in _schedule_next, before the wait even starts) rather than decided at
    fire time — magnitude is still randomized when it actually fires, but
    the commitment lets peek_next_event() expose real advance knowledge for
    bots.InsiderBot to "front-run", which is the whole point of that
    teaching exercise."""

    _KINDS = ("shock", "drift", "liquidity", "spread")
    _DIRECTIONAL_KINDS = ("shock", "drift")

    def __init__(self, index_service: IndexPriceService, bot_manager: BotManager, config: Config):
        self.index_service = index_service
        self.bot_manager = bot_manager
        self.config = config
        self._counter = itertools.count(1)
        self._stop = False
        self.next_event_at: float | None = None
        self.next_event_kind: str | None = None
        self.next_event_product: str | None = None
        self.next_event_direction: int | None = None

    def stop(self) -> None:
        self._stop = True

    def peek_next_event(self) -> dict | None:
        """Read-only lookahead at the next scheduled event, for
        bots.InsiderBot — never used to change when/what actually fires."""
        if self.next_event_at is None:
            return None
        return {
            "at": self.next_event_at,
            "kind": self.next_event_kind,
            "product": self.next_event_product,
            "direction": self.next_event_direction,
        }

    async def run(self) -> None:
        cfg = self.config.synthetic_feed
        if not cfg.random_events_enabled:
            logger.info("random_events: disabled")
            return
        logger.info("random_events: enabled, mean_interval=%.0fs", cfg.random_event_mean_interval_seconds)
        while not self._stop:
            self._schedule_next()
            wait = max(0.0, self.next_event_at - time.time())
            await asyncio.sleep(wait)
            if not self._stop:
                self._fire_scheduled()

    def _schedule_next(self) -> None:
        cfg = self.config.synthetic_feed
        wait = random.expovariate(1.0 / cfg.random_event_mean_interval_seconds)
        self.next_event_at = time.time() + wait
        self.next_event_kind = random.choice(self._KINDS)
        self.next_event_product = random.choice(list(self.config.products.keys()))
        self.next_event_direction = random.choice([1, -1])

    def _fire_scheduled(self) -> None:
        now = time.time()
        idx = next(self._counter)
        kind = self.next_event_kind
        product = self.next_event_product
        direction = self.next_event_direction
        name = f"auto_{kind}_{idx}"
        tick = self.config.products[product].tick_size

        if kind == "shock":
            target_offset = direction * random.uniform(5, 40) * tick
            self.index_service.trigger_price_shock(
                product, target_offset, now, ramp_seconds=1.0, hold_seconds=random.uniform(5, 15), name=name
            )
        elif kind == "drift":
            drift = direction * random.uniform(10, 60) * tick
            duration = random.uniform(20, 60)
            self.index_service.trigger_bull_bear(product, drift, duration, now, name=name)
        elif kind == "liquidity":
            liq_kind = random.choice(["withdraw", "flood"])
            duration = random.uniform(15, 45)
            magnitude = random.uniform(1.5, 3.0)
            self.bot_manager.trigger_liquidity_event(product, liq_kind, duration, now, magnitude)
        elif kind == "spread":
            spread_kind = random.choice(["widen", "invert", "converge"])
            magnitude = random.uniform(0.3, 0.8) if spread_kind == "converge" else random.uniform(10, 30) * tick
            self.index_service.trigger_spread_event(spread_kind, magnitude, now, name=name)

        logger.info("random_events: fired %s (%s) on %s", name, kind, product)
        # Cleared (not left stale pointing at what just fired) so a bot
        # that hasn't ticked yet this instant sees "nothing scheduled" for
        # the brief window until _schedule_next runs again, rather than a
        # peek_next_event() that looks like a still-upcoming event.
        self.next_event_at = None
        self.next_event_kind = None
        self.next_event_product = None
        self.next_event_direction = None
