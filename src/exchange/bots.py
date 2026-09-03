"""Market maker + noise bots — build-spec.md §6.1/§6.2, and the
liquidity-event admin action (§6.3) that temporarily reshapes MM params.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from .engine import MatchingEngine, OrderRejected
from .index_feed import IndexPriceService
from .models import OrderType, Side

logger = logging.getLogger("exchange.bots")


@dataclass
class LiquidityEventState:
    """Temporary multiplier on MM spread/size, per product. `kind` is
    'withdraw' (wider spreads, smaller size, some bots go passive) or
    'flood' (tighter spreads, bigger size)."""

    kind: str
    spread_multiplier: float
    size_multiplier: float
    expires_at: float


@dataclass
class MMBotConfig:
    account_id: str
    base_spread_frac: float  # fraction of index price, e.g. 0.004 = 40bps
    quote_size: int
    skew_sensitivity: float  # how much inventory shifts the quote midpoint
    requote_interval: float = 2.0
    active: bool = True
    # §6.1: "requote on a short timer and/or whenever index price moves
    # more than some threshold" — the timer alone left visible gaps after a
    # fill (a bot sat one-sided for up to requote_interval) and let the
    # book lag a fast-moving index. Both bypass the timer immediately.
    reprice_threshold_ticks: float = 2.0


@dataclass
class NoiseBotConfig:
    account_id: str
    arrival_rate_per_sec: float  # Poisson lambda
    max_size: int = 2
    active: bool = True


class MarketMakerBot:
    def __init__(self, config: MMBotConfig, product: str):
        self.config = config
        self.product = product
        self.last_quote_time = 0.0
        self.last_quote_index_price: float | None = None
        self.bid_order_id: int | None = None
        self.ask_order_id: int | None = None

    def maybe_requote(
        self,
        engine: MatchingEngine,
        index_service: IndexPriceService,
        liquidity_events: dict[str, LiquidityEventState],
        now: float,
        spread_scale: float = 1.0,
    ) -> None:
        index = index_service.get_index_price(self.product, now)
        if index is None:
            return

        bid_order = engine.orders.get(self.bid_order_id) if self.bid_order_id is not None else None
        ask_order = engine.orders.get(self.ask_order_id) if self.ask_order_id is not None else None
        bid_filled = self.bid_order_id is not None and not (bid_order and bid_order.is_resting)
        ask_filled = self.ask_order_id is not None and not (ask_order and ask_order.is_resting)

        tick_size = engine.products[self.product].tick_size
        price_moved = (
            self.last_quote_index_price is not None
            and abs(index - self.last_quote_index_price) > tick_size * self.config.reprice_threshold_ticks
        )
        due_on_timer = now - self.last_quote_time >= self.config.requote_interval

        if not (due_on_timer or bid_filled or ask_filled or price_moved):
            return
        self.last_quote_time = now
        self.last_quote_index_price = index

        if not self.config.active:
            for oid in (self.bid_order_id, self.ask_order_id):
                if oid is not None:
                    try:
                        engine.cancel_order(oid, self.config.account_id)
                    except OrderRejected:
                        pass
            self.bid_order_id = None
            self.ask_order_id = None
            return

        # spread_scale is the admin's persistent global tightness lever
        # (§7 "adjust bot parameters live"); a liquidity event multiplies
        # further on top of it, temporarily.
        spread_mult = spread_scale
        size_mult = 1.0
        event = liquidity_events.get(self.product)
        passive = False
        if event and event.expires_at > now:
            spread_mult *= event.spread_multiplier
            size_mult = event.size_multiplier
            if event.kind == "withdraw" and size_mult <= 0:
                passive = True

        # cancel existing resting quotes before requoting
        for oid in (self.bid_order_id, self.ask_order_id):
            if oid is not None:
                try:
                    engine.cancel_order(oid, self.config.account_id)
                except OrderRejected:
                    pass
        self.bid_order_id = None
        self.ask_order_id = None

        if passive:
            return

        account = engine.accounts[self.config.account_id]
        pos = account.position_for(self.product).qty
        skew = -pos * self.config.skew_sensitivity  # long -> skew quotes down
        mid = index + skew

        half_spread = index * self.config.base_spread_frac * spread_mult / 2
        tick_size = engine.products[self.product].tick_size
        # Snap to the tick grid: an off-grid resting price would never
        # coincide with any row the ladder generates (i * tick_size), so
        # that liquidity would be silently invisible on the website.
        bid_price = round(round((mid - half_spread) / tick_size) * tick_size, 2)
        ask_price = round(round((mid + half_spread) / tick_size) * tick_size, 2)
        if ask_price <= bid_price:
            ask_price = round(bid_price + tick_size, 2)
        size = max(1, round(self.config.quote_size * size_mult))

        try:
            order = engine.submit_order(
                self.config.account_id, self.product, Side.BUY, OrderType.LIMIT, size, bid_price, now
            )
            if order.is_resting:
                self.bid_order_id = order.id
        except OrderRejected:
            pass
        try:
            order = engine.submit_order(
                self.config.account_id, self.product, Side.SELL, OrderType.LIMIT, size, ask_price, now
            )
            if order.is_resting:
                self.ask_order_id = order.id
        except OrderRejected:
            pass


class NoiseBot:
    def __init__(self, config: NoiseBotConfig, product: str):
        self.config = config
        self.product = product
        self._next_arrival = 0.0

    def maybe_trade(self, engine: MatchingEngine, index_service: IndexPriceService, now: float) -> None:
        if now < self._next_arrival:
            return
        self._next_arrival = now + random.expovariate(self.config.arrival_rate_per_sec)
        if not self.config.active:
            return

        index = index_service.get_index_price(self.product, now)
        if index is None:
            return
        side = random.choice([Side.BUY, Side.SELL])
        qty = random.randint(1, self.config.max_size)
        book = engine.book_snapshot(self.product, depth=1)
        touch = book["asks"][0]["price"] if side is Side.BUY and book["asks"] else (
            book["bids"][0]["price"] if side is Side.SELL and book["bids"] else index
        )
        tick_size = engine.products[self.product].tick_size
        price = round(round(touch / tick_size) * tick_size, 2)
        try:
            engine.submit_order(self.config.account_id, self.product, side, OrderType.LIMIT, qty, price, now)
        except OrderRejected:
            pass


@dataclass
class ArbBotConfig:
    account_id: str
    # Book price must be displaced from the index by more than this many
    # ticks before the bot acts — comfortably wider than the MM bots'
    # normal ~3-10 tick spread, so it's a backstop against the book being
    # dragged/held away from fair value, not a peg fighting normal quoting.
    threshold_ticks: float = 15.0
    correction_qty: int = 10
    check_interval: float = 1.0
    active: bool = True


class ArbBot:
    """A well-capitalized, uncapped backstop (build-spec.md §3's
    MAX_POSITION/buying-power limits exist to bound a *student's* risk —
    this account is intentionally exempt from both, see
    state.py:ADMIN_TRADING_ACCOUNT_ID-style unlimited_position_accounts)
    that trades against the book whenever it drifts too far from the
    index/theo price, so a well-funded student (or a coordinated group)
    can't just buy or sell enough size to hold the traded price away from
    fair value indefinitely."""

    def __init__(self, config: ArbBotConfig, product: str):
        self.config = config
        self.product = product
        self.last_check_time = 0.0

    def maybe_correct(self, engine: MatchingEngine, index_service: IndexPriceService, now: float) -> None:
        if not self.config.active:
            return
        if now - self.last_check_time < self.config.check_interval:
            return
        self.last_check_time = now

        index = index_service.get_index_price(self.product, now)
        if index is None:
            return
        tick_size = engine.products[self.product].tick_size
        threshold = self.config.threshold_ticks * tick_size

        book = engine.book_snapshot(self.product, depth=1)
        best_bid = book["bids"][0]["price"] if book["bids"] else None
        best_ask = book["asks"][0]["price"] if book["asks"] else None

        try:
            if best_ask is not None and best_ask < index - threshold:
                # book is too cheap relative to fair value — buy it back up
                engine.submit_order(
                    self.config.account_id, self.product, Side.BUY, OrderType.MARKET,
                    self.config.correction_qty, None, now,
                )
            elif best_bid is not None and best_bid > index + threshold:
                # book is too rich relative to fair value — sell it back down
                engine.submit_order(
                    self.config.account_id, self.product, Side.SELL, OrderType.MARKET,
                    self.config.correction_qty, None, now,
                )
        except OrderRejected:
            pass


class BotManager:
    """Owns all MM + noise bots across all products and runs one tick loop."""

    def __init__(self, engine: MatchingEngine, index_service: IndexPriceService, starting_cash: float):
        self.engine = engine
        self.index_service = index_service
        self.starting_cash = starting_cash
        self.mm_bots: list[MarketMakerBot] = []
        self.noise_bots: list[NoiseBot] = []
        self.arb_bots: list[ArbBot] = []
        self.liquidity_events: dict[str, LiquidityEventState] = {}
        # Persistent admin lever (§7 "adjust bot parameters live"), distinct
        # from a timed liquidity event: 1.0 = as-configured, <1 tighter,
        # >1 wider, applied to every MM bot's spread on every requote.
        self.global_spread_scale: float = 1.0

    def spawn_defaults(self, products: list[str]) -> None:
        # Spread is stored as a fraction of index price (so it scales with
        # the product), but calibrated here in *ticks* against the spec's
        # documented ~$75 contract notional (build-spec.md §2) so the book
        # actually reads as ~3-10 ticks wide, not a vague price fraction.
        reference_price = 75.0
        for product in products:
            tick_size = self.engine.products[product].tick_size
            for i in range(5):
                spread_ticks = 3 + 1.75 * i  # 3, 4.75, 6.5, 8.25, 10 ticks across the 5 bots
                spread = (spread_ticks * tick_size) / reference_price
                self.spawn_mm_bot(
                    product,
                    base_spread_frac=spread,
                    quote_size=3 + i,
                    skew_sensitivity=0.05,
                    requote_interval=1.5 + 0.3 * i,
                )
            for i in range(3):
                self.spawn_noise_bot(product, arrival_rate_per_sec=0.3 + 0.1 * i, max_size=2)
            self.spawn_arb_bot(product)

    def spawn_mm_bot(
        self,
        product: str,
        base_spread_frac: float = 0.004,
        quote_size: int = 3,
        skew_sensitivity: float = 0.05,
        requote_interval: float = 2.0,
    ) -> MarketMakerBot:
        existing = [b for b in self.mm_bots if b.product == product]
        index = len(existing)
        account_id = f"mm_{product}_{index}"
        while account_id in self.engine.accounts:
            index += 1
            account_id = f"mm_{product}_{index}"
        self.engine.get_or_create_account(account_id, self.starting_cash * 100)
        cfg = MMBotConfig(
            account_id=account_id,
            base_spread_frac=base_spread_frac,
            quote_size=quote_size,
            skew_sensitivity=skew_sensitivity,
            requote_interval=requote_interval,
        )
        bot = MarketMakerBot(cfg, product)
        self.mm_bots.append(bot)
        logger.info("bots: spawned MM bot %s for %s", account_id, product)
        return bot

    def spawn_noise_bot(
        self,
        product: str,
        arrival_rate_per_sec: float = 0.3,
        max_size: int = 2,
    ) -> NoiseBot:
        existing = [b for b in self.noise_bots if b.product == product]
        index = len(existing)
        account_id = f"noise_{product}_{index}"
        while account_id in self.engine.accounts:
            index += 1
            account_id = f"noise_{product}_{index}"
        self.engine.get_or_create_account(account_id, self.starting_cash * 100)
        cfg = NoiseBotConfig(account_id=account_id, arrival_rate_per_sec=arrival_rate_per_sec, max_size=max_size)
        bot = NoiseBot(cfg, product)
        self.noise_bots.append(bot)
        logger.info("bots: spawned noise bot %s for %s", account_id, product)
        return bot

    def spawn_arb_bot(
        self,
        product: str,
        threshold_ticks: float = 15.0,
        correction_qty: int = 10,
        check_interval: float = 1.0,
    ) -> ArbBot:
        existing = [b for b in self.arb_bots if b.product == product]
        index = len(existing)
        account_id = f"arb_{product}_{index}"
        while account_id in self.engine.accounts:
            index += 1
            account_id = f"arb_{product}_{index}"
        self.engine.get_or_create_account(account_id, 1_000_000_000.0)
        self.engine.unlimited_position_accounts.add(account_id)
        cfg = ArbBotConfig(
            account_id=account_id,
            threshold_ticks=threshold_ticks,
            correction_qty=correction_qty,
            check_interval=check_interval,
        )
        bot = ArbBot(cfg, product)
        self.arb_bots.append(bot)
        logger.info("bots: spawned arb bot %s for %s", account_id, product)
        return bot

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for bot in self.mm_bots:
            bot.maybe_requote(self.engine, self.index_service, self.liquidity_events, now, self.global_spread_scale)
        for bot in self.noise_bots:
            bot.maybe_trade(self.engine, self.index_service, now)
        for bot in self.arb_bots:
            bot.maybe_correct(self.engine, self.index_service, now)

    def trigger_liquidity_event(
        self, product: str, kind: str, duration_seconds: float, now: float, magnitude: float = 2.0
    ) -> None:
        if kind == "withdraw":
            state = LiquidityEventState(
                kind="withdraw", spread_multiplier=magnitude, size_multiplier=1.0 / magnitude, expires_at=now + duration_seconds
            )
        elif kind == "flood":
            state = LiquidityEventState(
                kind="flood", spread_multiplier=1.0 / magnitude, size_multiplier=magnitude, expires_at=now + duration_seconds
            )
        else:
            raise ValueError(f"unknown liquidity event kind: {kind}")
        self.liquidity_events[product] = state
        logger.info("bots:%s liquidity event %s for %.1fs (mag=%.2f)", product, kind, duration_seconds, magnitude)

    async def run_forever(self, interval_seconds: float = 0.5) -> None:
        import asyncio

        while True:
            self.tick()
            await asyncio.sleep(interval_seconds)
