"""15-minute European BTC options: a rolling strike chain priced with
Black-Scholes and quoted by the existing MarketMakerBot machinery, so the
matching engine, ledger, and bots need no option-specific logic at all.

Cash settlement at expiry reuses ledger.apply_fill's existing "cash only
moves on close" rule unchanged — force-closing a position at intrinsic
value *is* exactly the right settlement, since realized PnL from that fill
is qty * (intrinsic - avg_cost), the correct payoff for a long/short option
position held to expiry.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .bots import BotManager
from .config import OptionsConfig, ProductConfig
from .engine import MatchingEngine, OrderRejected
from .index_feed import IndexPriceService
from .ledger import apply_fill
from .models import Side

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, option_type: str) -> float:
    """Black-Scholes theoretical price, zero risk-free rate (a 15-minute
    teaching instrument doesn't need discounting). Falls back to intrinsic
    value once time-to-expiry or vol collapse to zero, where the standard
    d1/d2 formula divides by zero."""
    if t_years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    if option_type == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def next_boundary(now: float, window_seconds: float) -> float:
    """The next clock-aligned window edge strictly after `now`."""
    return (math.floor(now / window_seconds) + 1) * window_seconds


@dataclass
class OptionInstrument:
    symbol: str
    strike: float
    option_type: str  # "call" | "put"
    expiry_ts: float
    underlying: str


class OptionsChainManager:
    """Owns the currently-live option chain: creates a fresh strike ladder
    each window, prices it every tick via Black-Scholes fed through the
    normal index-price path (so the existing MM bot quotes it exactly like
    any other product), and cash-settles positions at intrinsic value when
    a window expires."""

    def __init__(
        self,
        engine: MatchingEngine,
        index_service: IndexPriceService,
        bot_manager: BotManager,
        cfg: OptionsConfig,
    ):
        self.engine = engine
        self.index_service = index_service
        self.bot_manager = bot_manager
        self.cfg = cfg
        self.chain: dict[str, OptionInstrument] = {}

    # -- pricing ------------------------------------------------------------
    def price_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        spot = self.index_service.get_index_price(self.cfg.underlying, now)
        if spot is None:
            return
        for opt in self.chain.values():
            t_years = max(0.0, opt.expiry_ts - now) / SECONDS_PER_YEAR
            theo = bs_price(spot, opt.strike, t_years, self.cfg.implied_volatility, opt.option_type)
            self.index_service.on_raw_tick(opt.symbol, theo, now)

    # -- chain lifecycle ------------------------------------------------------
    @staticmethod
    def _symbol(expiry_ts: float, strike: float, option_type: str) -> str:
        expiry_label = time.strftime("%H%M", time.gmtime(expiry_ts))
        suffix = "C" if option_type == "call" else "P"
        # .0f would collide for a sub-1.0 strike_increment (e.g. 77.5 and
        # 78.0 both rendering "78") — always carrying 2 decimals keeps every
        # strike's symbol unique regardless of the configured increment.
        return f"BTC-{expiry_label}-{strike:.2f}{suffix}"

    def create_chain(self, now: float, expiry_ts: float) -> None:
        spot = self.index_service.get_index_price(self.cfg.underlying, now)
        if spot is None:
            return
        atm = round(spot / self.cfg.strike_increment) * self.cfg.strike_increment
        for i in range(-self.cfg.strikes_each_side, self.cfg.strikes_each_side + 1):
            strike = atm + i * self.cfg.strike_increment
            if strike <= 0:
                continue
            for option_type in ("call", "put"):
                symbol = self._symbol(expiry_ts, strike, option_type)
                product_cfg = ProductConfig(
                    symbol=symbol,
                    underlying=self.cfg.underlying,
                    contract_size=self.cfg.contract_size,
                    max_position=self.cfg.max_position,
                    tick_size=self.cfg.tick_size,
                    starting_price=spot,
                )
                self.engine.add_product(product_cfg)
                self.index_service.add_product(product_cfg)
                self.chain[symbol] = OptionInstrument(
                    symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    expiry_ts=expiry_ts,
                    underlying=self.cfg.underlying,
                )
                # Options run small near strike (theo can be a few cents) —
                # a base-spread fraction sized for a $75-notional future
                # would collapse to sub-tick here; the bot's own bid<ask
                # tick floor keeps quotes sane regardless, so an oversized
                # fraction is a deliberate choice, not a bug.
                self.bot_manager.spawn_mm_bot(
                    symbol, base_spread_frac=0.15, quote_size=3, skew_sensitivity=0.05, requote_interval=1.5
                )

    def _settle_and_retire(self, opt: OptionInstrument, now: float) -> None:
        spot = self.index_service.get_index_price(opt.underlying, now)
        settlement = 0.0
        if spot is not None:
            settlement = (
                max(spot - opt.strike, 0.0) if opt.option_type == "call" else max(opt.strike - spot, 0.0)
            )

        for account in self.engine.accounts.values():
            pos = account.positions.get(opt.symbol)
            if pos is None or pos.qty == 0:
                continue
            closing_side = Side.SELL if pos.qty > 0 else Side.BUY
            apply_fill(account, opt.symbol, closing_side, abs(pos.qty), settlement)

        book = self.engine.books.get(opt.symbol)
        if book is not None:
            for order in list(book.bids) + list(book.asks):
                try:
                    self.engine.cancel_order(order.id, order.account_id)
                except OrderRejected:
                    pass

        self.bot_manager.mm_bots = [b for b in self.bot_manager.mm_bots if b.product != opt.symbol]
        self.engine.remove_product(opt.symbol)
        self.index_service.remove_product(opt.symbol)
        self.chain.pop(opt.symbol, None)

    def roll(self, now: float, next_expiry_ts: float, options_enabled: bool) -> None:
        """Settle anything past its expiry, then start the next chain iff
        options are currently enabled — but never tear down an
        in-flight chain just because the admin disabled the line
        mid-window, so open positions always settle fairly."""
        expired = [opt for opt in self.chain.values() if opt.expiry_ts <= now]
        for opt in expired:
            self._settle_and_retire(opt, now)
        if options_enabled and not self.chain:
            self.create_chain(now, next_expiry_ts)


class OptionsScheduler:
    """Sleeps until each clock-aligned window boundary and rolls the
    chain. Same shape as synthetic_feed.RandomEventScheduler — a small,
    independent background task wired into app.py's asyncio.gather."""

    def __init__(self, manager: OptionsChainManager, state):
        self.manager = manager
        self.state = state  # duck-typed: only .options_enabled is read
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        import asyncio

        window = self.manager.cfg.window_seconds
        while not self._stop:
            now = time.time()
            boundary = next_boundary(now, window)
            await asyncio.sleep(max(0.0, boundary - now))
            if self._stop:
                break
            now = time.time()
            self.manager.roll(now, next_boundary(now, window), self.state.options_enabled)
