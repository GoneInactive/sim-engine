"""Market maker + noise bots — build-spec.md §6.1/§6.2, and the
liquidity-event admin action (§6.3) that temporarily reshapes MM params.

The market maker is deliberately simple: one bot per product/contract,
configured by `legs` (how many bid/ask pairs to quote), `min_spread_ticks`
(each side's distance from mid for the first/tightest pair) and
`delta_ticks` (how much farther out each subsequent pair steps) — leg i's
bid/ask each sit min_spread_ticks + i*delta_ticks ticks from mid, all
skewed by the bot's own inventory.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from .config import MMBotDefaults
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
    legs: int = 3
    min_spread_ticks: float = 2.0
    delta_ticks: float = 1.0
    quote_size: int = 3
    skew_sensitivity: float = 0.05  # how much inventory shifts the quote midpoint
    requote_interval: float = 1.5
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
        self.bid_order_ids: list[int] = []
        self.ask_order_ids: list[int] = []

    def _any_resting_filled(self, engine: MatchingEngine, order_ids: list[int]) -> bool:
        for oid in order_ids:
            order = engine.orders.get(oid)
            if order is None or not order.is_resting:
                return True
        return False

    def _cancel_all(self, engine: MatchingEngine) -> None:
        for oid in (*self.bid_order_ids, *self.ask_order_ids):
            try:
                engine.cancel_order(oid, self.config.account_id)
            except OrderRejected:
                pass
        self.bid_order_ids = []
        self.ask_order_ids = []

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

        any_filled = self._any_resting_filled(engine, self.bid_order_ids) or self._any_resting_filled(
            engine, self.ask_order_ids
        )

        tick_size = engine.products[self.product].tick_size
        price_moved = (
            self.last_quote_index_price is not None
            and abs(index - self.last_quote_index_price) > tick_size * self.config.reprice_threshold_ticks
        )
        due_on_timer = now - self.last_quote_time >= self.config.requote_interval

        if not (due_on_timer or any_filled or price_moved):
            return
        self.last_quote_time = now
        self.last_quote_index_price = index

        if not self.config.active:
            self._cancel_all(engine)
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

        self._cancel_all(engine)
        if passive:
            return

        account = engine.accounts[self.config.account_id]
        pos = account.position_for(self.product).qty
        skew = -pos * self.config.skew_sensitivity  # long -> skew quotes down
        mid = index + skew
        size = max(1, round(self.config.quote_size * size_mult))

        for leg in range(self.config.legs):
            # min_spread_ticks/delta_ticks are each side's own distance
            # from mid (not a combined bid-ask spread halved) — that keeps
            # every leg's offset a whole number of ticks even when
            # delta_ticks itself is fractional, with no rounding collision
            # between adjacent legs.
            offset_ticks = (self.config.min_spread_ticks + leg * self.config.delta_ticks) * spread_mult
            offset = offset_ticks * tick_size
            # Snap to the tick grid: an off-grid resting price would never
            # coincide with any row the ladder generates (i * tick_size), so
            # that liquidity would be silently invisible on the website.
            bid_price = round(round((mid - offset) / tick_size) * tick_size, 2)
            ask_price = round(round((mid + offset) / tick_size) * tick_size, 2)
            if ask_price <= bid_price:
                ask_price = round(bid_price + tick_size, 2)

            try:
                order = engine.submit_order(
                    self.config.account_id, self.product, Side.BUY, OrderType.LIMIT, size, bid_price, now
                )
                if order.is_resting:
                    self.bid_order_ids.append(order.id)
            except OrderRejected:
                pass
            try:
                order = engine.submit_order(
                    self.config.account_id, self.product, Side.SELL, OrderType.LIMIT, size, ask_price, now
                )
                if order.is_resting:
                    self.ask_order_ids.append(order.id)
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
    # normal spread, so it's a backstop against the book being dragged/held
    # away from fair value, not a peg fighting normal quoting.
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


@dataclass
class InsiderBotConfig:
    account_id: str
    lead_seconds: float = 5.0
    size: int = 5
    hold_after_seconds: float = 8.0
    active: bool = True


class InsiderBot:
    """Teaching/surveillance exercise, not a real trading strategy: gets
    advance notice of the next scheduled random market event (see
    synthetic_feed.RandomEventScheduler.peek_next_event) and trades
    directionally `lead_seconds` before it fires, unwinding
    `hold_after_seconds` later — producing conspicuously well-timed P&L an
    admin/student can practice spotting on the leaderboard or fills feed."""

    def __init__(self, config: InsiderBotConfig):
        self.config = config
        self.acted_for_event_at: float | None = None
        self.open_product: str | None = None
        self.open_side: Side | None = None
        self.unwind_at: float | None = None

    def maybe_trade(self, engine: MatchingEngine, now: float, scheduler) -> None:
        if not self.config.active:
            return

        if self.open_product is not None and self.unwind_at is not None and now >= self.unwind_at:
            try:
                engine.submit_order(
                    self.config.account_id, self.open_product, self.open_side.opposite,
                    OrderType.MARKET, self.config.size, None, now,
                )
            except OrderRejected:
                pass
            self.open_product = None
            self.open_side = None
            self.unwind_at = None
            return

        if self.open_product is not None or scheduler is None:
            return  # already positioned for the last tip, waiting to unwind

        event = scheduler.peek_next_event()
        if event is None or event["kind"] not in ("shock", "drift"):
            return
        if self.acted_for_event_at == event["at"]:
            return
        if event["at"] - now > self.config.lead_seconds:
            return

        side = Side.BUY if event["direction"] > 0 else Side.SELL
        try:
            engine.submit_order(self.config.account_id, event["product"], side, OrderType.MARKET, self.config.size, None, now)
            self.open_product = event["product"]
            self.open_side = side
            self.unwind_at = now + self.config.hold_after_seconds
        except OrderRejected:
            pass
        self.acted_for_event_at = event["at"]


class BotManager:
    """Owns all MM + noise bots across all products and runs one tick loop."""

    def __init__(
        self,
        engine: MatchingEngine,
        index_service: IndexPriceService,
        starting_cash: float,
        mm_defaults: MMBotDefaults | None = None,
    ):
        self.engine = engine
        self.index_service = index_service
        self.starting_cash = starting_cash
        self.mm_defaults = mm_defaults or MMBotDefaults(
            legs=3, min_spread_ticks=2.0, delta_ticks=1.0, quote_size=3, skew_sensitivity=0.05, requote_interval=1.5
        )
        self.mm_bots: list[MarketMakerBot] = []
        self.noise_bots: list[NoiseBot] = []
        self.arb_bots: list[ArbBot] = []
        self.insider_bots: list[InsiderBot] = []
        self.liquidity_events: dict[str, LiquidityEventState] = {}
        # Persistent admin lever (§7 "adjust bot parameters live"), distinct
        # from a timed liquidity event: 1.0 = as-configured, <1 tighter,
        # >1 wider, applied to every MM bot's spread on every requote.
        self.global_spread_scale: float = 1.0
        # The random-event scheduler "tips" insider bots read from — wired
        # in after construction (app.py builds the scheduler after the bot
        # manager) via set_event_scheduler. None until then, and in tests
        # that never wire one, which just means insider bots stay inert.
        self._event_scheduler = None

    def set_event_scheduler(self, scheduler) -> None:
        self._event_scheduler = scheduler

    def spawn_defaults(self, products: list[str], mm_cfg: MMBotDefaults | None = None) -> None:
        cfg = mm_cfg or self.mm_defaults
        for product in products:
            self.spawn_mm_bot(
                product,
                legs=cfg.legs,
                min_spread_ticks=cfg.min_spread_ticks,
                delta_ticks=cfg.delta_ticks,
                quote_size=cfg.quote_size,
                skew_sensitivity=cfg.skew_sensitivity,
                requote_interval=cfg.requote_interval,
            )
            for i in range(3):
                self.spawn_noise_bot(product, arrival_rate_per_sec=0.3 + 0.1 * i, max_size=2)
            self.spawn_arb_bot(product)

    def spawn_mm_bot(
        self,
        product: str,
        legs: int = 3,
        min_spread_ticks: float = 2.0,
        delta_ticks: float = 1.0,
        quote_size: int = 3,
        skew_sensitivity: float = 0.05,
        requote_interval: float = 1.5,
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
            legs=legs,
            min_spread_ticks=min_spread_ticks,
            delta_ticks=delta_ticks,
            quote_size=quote_size,
            skew_sensitivity=skew_sensitivity,
            requote_interval=requote_interval,
        )
        bot = MarketMakerBot(cfg, product)
        self.mm_bots.append(bot)
        logger.info("bots: spawned MM bot %s for %s (legs=%d)", account_id, product, legs)
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

    def spawn_insider_bot(
        self,
        lead_seconds: float = 5.0,
        size: int = 5,
        hold_after_seconds: float = 8.0,
    ) -> InsiderBot:
        index = len(self.insider_bots)
        account_id = f"insider_{index}"
        while account_id in self.engine.accounts:
            index += 1
            account_id = f"insider_{index}"
        self.engine.get_or_create_account(account_id, self.starting_cash * 10)
        cfg = InsiderBotConfig(
            account_id=account_id, lead_seconds=lead_seconds, size=size, hold_after_seconds=hold_after_seconds,
        )
        bot = InsiderBot(cfg)
        self.insider_bots.append(bot)
        logger.info("bots: spawned insider bot %s", account_id)
        return bot

    def remove_mm_bot(self, product: str) -> None:
        self.mm_bots = [b for b in self.mm_bots if b.product != product]

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for bot in self.mm_bots:
            bot.maybe_requote(self.engine, self.index_service, self.liquidity_events, now, self.global_spread_scale)
        for bot in self.noise_bots:
            bot.maybe_trade(self.engine, self.index_service, now)
        for bot in self.arb_bots:
            bot.maybe_correct(self.engine, self.index_service, now)
        for bot in self.insider_bots:
            bot.maybe_trade(self.engine, now, self._event_scheduler)

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
