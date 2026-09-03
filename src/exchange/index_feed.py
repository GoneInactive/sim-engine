"""Index ("mark") price service — build-spec.md §4 and §6.3.

One IndexPriceService instance holds, per product:
  - the live raw price fed in by whatever source is currently active
    (the synthetic feed generator — see synthetic_feed.py — or a replay
    driver in replay mode, both call `on_raw_tick`, this module doesn't
    care which),
  - staleness detection and SMA fallback when ticks stop arriving,
  - a reconnect blend back to live once ticks resume,
  - a set of active admin-triggered offset events (price shock, bull/bear
    drift, BTC/ETH spread widen/invert/converge) layered on top of the
    base price.

`get_index_price(product, now)` is the single read path everything else
(MM bots, leaderboard mark-to-market, website) should call.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .config import FeedConfig, ProductConfig

logger = logging.getLogger("exchange.index_feed")


@dataclass
class OffsetEvent:
    """A time-shaped additive offset applied on top of a product's base
    index price: ramps from 0 to `target_offset` over `ramp_seconds`, holds
    until `hold_until`, then decays back to 0 over `decay_seconds`."""

    name: str
    product: str
    start_time: float
    ramp_seconds: float
    hold_until: float  # absolute time the hold phase ends
    decay_seconds: float
    target_offset: float

    def offset_at(self, now: float) -> Optional[float]:
        """Returns None once the event has fully decayed away (caller
        should drop it)."""
        if now < self.start_time:
            return 0.0
        ramp_end = self.start_time + self.ramp_seconds
        if now <= ramp_end:
            frac = 1.0 if self.ramp_seconds <= 0 else (now - self.start_time) / self.ramp_seconds
            return self.target_offset * frac
        if now <= self.hold_until:
            return self.target_offset
        decay_end = self.hold_until + self.decay_seconds
        if now <= decay_end:
            frac = 1.0 if self.decay_seconds <= 0 else (now - self.hold_until) / self.decay_seconds
            return self.target_offset * (1.0 - frac)
        return None


@dataclass
class _ProductState:
    raw_price: Optional[float] = None  # already scaled to contract terms
    last_update: Optional[float] = None
    mode: str = "live"  # "live" | "fallback"
    recent_ticks: deque = field(default_factory=lambda: deque(maxlen=200))
    fallback_price: Optional[float] = None
    blend_from: Optional[float] = None
    blend_start: Optional[float] = None


class IndexPriceService:
    def __init__(self, feed_cfg: FeedConfig, products: dict[str, ProductConfig]):
        self.feed_cfg = feed_cfg
        self.products = products
        self.state: dict[str, _ProductState] = {
            symbol: _ProductState(recent_ticks=deque(maxlen=feed_cfg.sma_window))
            for symbol in products
        }
        self.events: dict[str, list[OffsetEvent]] = {symbol: [] for symbol in products}

    # -- ingestion --------------------------------------------------------
    def on_raw_tick(self, product: str, underlying_price: float, now: float) -> None:
        """Called by whichever source is active (synthetic feed generator
        or replay driver) with a fresh underlying (e.g. BTC/USD) price."""
        cfg = self.products[product]
        index_price = underlying_price * cfg.contract_size
        state = self.state[product]
        state.raw_price = index_price
        state.last_update = now
        state.recent_ticks.append(index_price)

        if state.mode == "fallback":
            # We were stale and are now receiving ticks again: start a
            # blend back to live rather than snapping.
            logger.info("feed:%s stale->live, starting reconnect blend", product)
            state.mode = "live"
            state.blend_from = (
                state.fallback_price if state.fallback_price is not None else index_price
            )
            state.blend_start = now
            state.fallback_price = None

    # -- base price (live / fallback / blend) ------------------------------
    def _base_price(self, product: str, now: float) -> Optional[float]:
        state = self.state[product]
        if state.raw_price is None or state.last_update is None:
            return None

        is_stale = (now - state.last_update) > self.feed_cfg.stale_threshold_seconds
        if is_stale and state.mode == "live":
            logger.info("feed:%s live->stale, entering SMA fallback", product)
            state.mode = "fallback"
            ticks = list(state.recent_ticks)
            state.fallback_price = sum(ticks) / len(ticks) if ticks else state.raw_price

        if state.mode == "fallback":
            return state.fallback_price

        # mode == "live"
        if state.blend_start is not None:
            elapsed = now - state.blend_start
            duration = self.feed_cfg.reconnect_blend_seconds
            if duration <= 0 or elapsed >= duration:
                state.blend_from = None
                state.blend_start = None
                return state.raw_price
            frac = elapsed / duration
            return state.blend_from + (state.raw_price - state.blend_from) * frac

        return state.raw_price

    # -- admin event offsets ------------------------------------------------
    def _offset(self, product: str, now: float) -> float:
        active = []
        total = 0.0
        for event in self.events[product]:
            offset = event.offset_at(now)
            if offset is None:
                logger.info("feed:%s event %s expired", product, event.name)
                continue
            active.append(event)
            total += offset
        self.events[product] = active
        return total

    def get_index_price(self, product: str, now: float) -> Optional[float]:
        base = self._base_price(product, now)
        if base is None:
            return None
        return base + self._offset(product, now)

    def get_all_index_prices(self, now: float) -> dict[str, float]:
        out = {}
        for product in self.products:
            price = self.get_index_price(product, now)
            if price is not None:
                out[product] = price
        return out

    def is_stale(self, product: str) -> bool:
        return self.state[product].mode == "fallback"

    # -- admin-triggered events (§6.3) ---------------------------------------
    def add_event(self, event: OffsetEvent) -> None:
        self.events[event.product].append(event)
        logger.info(
            "feed:%s event %s added target_offset=%.4f ramp=%.1fs hold_until=%.1f decay=%.1fs",
            event.product,
            event.name,
            event.target_offset,
            event.ramp_seconds,
            event.hold_until,
            event.decay_seconds,
        )

    def trigger_price_shock(
        self,
        product: str,
        target_offset: float,
        now: float,
        ramp_seconds: float = 1.0,
        hold_seconds: float = 5.0,
        name: str = "shock",
    ) -> OffsetEvent:
        event = OffsetEvent(
            name=name,
            product=product,
            start_time=now,
            ramp_seconds=ramp_seconds,
            hold_until=now + ramp_seconds + hold_seconds,
            decay_seconds=self.feed_cfg.shock_decay_seconds,
            target_offset=target_offset,
        )
        self.add_event(event)
        return event

    def trigger_bull_bear(
        self,
        product: str,
        drift: float,
        duration_seconds: float,
        now: float,
        name: str = "drift",
    ) -> OffsetEvent:
        """`drift` is the total additive move applied smoothly over
        `duration_seconds` (positive = bull, negative = bear)."""
        event = OffsetEvent(
            name=name,
            product=product,
            start_time=now,
            ramp_seconds=duration_seconds,
            hold_until=now + duration_seconds,
            decay_seconds=self.feed_cfg.shock_decay_seconds,
            target_offset=drift,
        )
        self.add_event(event)
        return event

    def trigger_spread_event(
        self,
        kind: str,
        magnitude: float,
        now: float,
        btc_product: str = "BTC-MINI",
        eth_product: str = "ETH-MINI",
        ramp_seconds: float = 2.0,
        hold_seconds: float = 8.0,
        name: str = "spread",
    ) -> tuple[OffsetEvent, OffsetEvent]:
        """Joint two-product event moving the BTC/ETH index spread
        (defined here as `btc_index - eth_index`, both already in
        contract-notional dollar terms). `kind` is 'widen', 'invert', or
        'converge'; `magnitude` is a dollar amount for widen/invert, or a
        0..1 fraction of the current spread to remove for converge."""
        btc_price = self.get_index_price(btc_product, now)
        eth_price = self.get_index_price(eth_product, now)
        if btc_price is None or eth_price is None:
            raise ValueError("cannot trigger spread event before both products have an index price")
        current_spread = btc_price - eth_price

        if kind == "widen":
            shift = magnitude if current_spread >= 0 else -magnitude
        elif kind == "invert":
            sign = 1.0 if current_spread >= 0 else -1.0
            shift = -2 * current_spread - sign * magnitude
        elif kind == "converge":
            frac = max(0.0, min(1.0, magnitude))
            shift = -current_spread * frac
        else:
            raise ValueError(f"unknown spread event kind: {kind}")

        btc_event = OffsetEvent(
            name=name,
            product=btc_product,
            start_time=now,
            ramp_seconds=ramp_seconds,
            hold_until=now + ramp_seconds + hold_seconds,
            decay_seconds=self.feed_cfg.shock_decay_seconds,
            target_offset=shift / 2,
        )
        eth_event = OffsetEvent(
            name=name,
            product=eth_product,
            start_time=now,
            ramp_seconds=ramp_seconds,
            hold_until=now + ramp_seconds + hold_seconds,
            decay_seconds=self.feed_cfg.shock_decay_seconds,
            target_offset=-shift / 2,
        )
        self.add_event(btc_event)
        self.add_event(eth_event)
        return btc_event, eth_event
