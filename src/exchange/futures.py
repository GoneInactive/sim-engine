"""1-hour rolling futures + calendar spreads, one ladder per underlying.

Unlike the options chain (options.py), which regenerates a whole strike
ladder on one shared clock-aligned window, futures form a rolling ladder:
`num_live` contracts are kept live at all times, each `window_seconds`
apart, staggered (not all expiring together). When the front contract
expires it's cash-settled at the underlying's current index price (no
strike/intrinsic-value math — a future just tracks spot 1:1) and a new far
contract is appended to keep `num_live` live. Calendar spreads (near - far,
both already contract-scaled) are auto-registered between every adjacent
pair of live contracts and rebuilt whenever a chain's contract set changes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .bots import BotManager
from .config import FuturesConfig, MMBotDefaults, NoiseBotDefaults, ProductConfig
from .engine import MatchingEngine, OrderRejected
from .index_feed import IndexPriceService
from .models import Side


@dataclass
class FutureInstrument:
    symbol: str
    underlying: str
    expiry_ts: float


@dataclass
class CalendarSpreadInstrument:
    symbol: str
    underlying: str
    near_symbol: str
    far_symbol: str


def _base_ticker(underlying: str) -> str:
    return underlying.split("-")[0]


class FuturesChainManager:
    def __init__(
        self,
        engine: MatchingEngine,
        index_service: IndexPriceService,
        bot_manager: BotManager,
        cfg: FuturesConfig,
        mm_cfg: MMBotDefaults,
        noise_cfg: NoiseBotDefaults | None = None,
    ):
        self.engine = engine
        self.index_service = index_service
        self.bot_manager = bot_manager
        self.cfg = cfg
        self.mm_cfg = mm_cfg
        self.noise_cfg = noise_cfg
        self.contracts: dict[str, dict[str, FutureInstrument]] = {u: {} for u in cfg.underlyings}
        self.calendar_spreads: dict[str, dict[str, CalendarSpreadInstrument]] = {u: {} for u in cfg.underlyings}

    def all_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for contracts in self.contracts.values():
            symbols.update(contracts.keys())
        for spreads in self.calendar_spreads.values():
            symbols.update(spreads.keys())
        return symbols

    # -- pricing --------------------------------------------------------------
    def price_tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for underlying, contracts in self.contracts.items():
            spot = self.index_service.get_index_price(underlying, now)
            if spot is None:
                continue
            # A 1-hour teaching future tracks spot 1:1 (no cost-of-carry/
            # basis model) — the calendar spread between two contracts is
            # therefore ~0 most of the time and only moves when the admin
            # or synthetic feed drives the two legs' recent ticks apart
            # during the brief window before both have re-priced.
            for fut in contracts.values():
                self.index_service.on_raw_tick(fut.symbol, spot, now)
        for underlying, spreads in self.calendar_spreads.items():
            for cs in spreads.values():
                near = self.index_service.get_index_price(cs.near_symbol, now)
                far = self.index_service.get_index_price(cs.far_symbol, now)
                if near is not None and far is not None:
                    self.index_service.on_raw_tick(cs.symbol, near - far, now)

    # -- lifecycle --------------------------------------------------------------
    def _contract_symbol(self, underlying: str, expiry_ts: float) -> str:
        label = time.strftime("%m%d%H", time.gmtime(expiry_ts))
        return f"{_base_ticker(underlying)}-FUT-{label}"

    def _spawn_mm_bot(self, symbol: str) -> None:
        self.bot_manager.spawn_mm_bot(
            symbol,
            legs=self.mm_cfg.legs,
            min_spread_ticks=self.mm_cfg.min_spread_ticks,
            delta_ticks=self.mm_cfg.delta_ticks,
            quote_size=self.mm_cfg.quote_size,
            skew_sensitivity=self.mm_cfg.skew_sensitivity,
            requote_interval=self.mm_cfg.requote_interval,
        )

    def _spawn_noise_bots(self, symbol: str) -> None:
        if self.noise_cfg is None:
            return
        for i in range(self.noise_cfg.count):
            self.bot_manager.spawn_noise_bot(
                symbol,
                arrival_rate_per_sec=self.noise_cfg.arrival_rate_per_sec + 0.1 * i,
                max_size=self.noise_cfg.max_size,
            )

    def create_contract(self, underlying: str, now: float, expiry_ts: float) -> str:
        spot = self.index_service.get_index_price(underlying, now)
        symbol = self._contract_symbol(underlying, expiry_ts)
        product_cfg = ProductConfig(
            symbol=symbol,
            underlying=underlying,
            contract_size=1.0,
            tick_size=self.cfg.tick_size,
            leverage=self.cfg.leverage,
            starting_price=spot if spot is not None else 0.0,
        )
        self.engine.add_product(product_cfg)
        self.index_service.add_product(product_cfg)
        self.contracts[underlying][symbol] = FutureInstrument(symbol=symbol, underlying=underlying, expiry_ts=expiry_ts)
        self._spawn_mm_bot(symbol)
        self._spawn_noise_bots(symbol)
        return symbol

    def _retire_calendar_spread(self, cs: CalendarSpreadInstrument) -> None:
        book = self.engine.books.get(cs.symbol)
        if book is not None:
            for order in list(book.bids) + list(book.asks):
                try:
                    self.engine.cancel_order(order.id, order.account_id)
                except OrderRejected:
                    pass
        self.bot_manager.remove_mm_bot(cs.symbol)
        self.bot_manager.remove_noise_bots(cs.symbol)
        self.engine.remove_product(cs.symbol)
        self.index_service.remove_product(cs.symbol)
        self.calendar_spreads[cs.underlying].pop(cs.symbol, None)

    def _rebuild_calendar_spreads(self, underlying: str, now: float) -> None:
        for cs in list(self.calendar_spreads[underlying].values()):
            self._retire_calendar_spread(cs)
        live = sorted(self.contracts[underlying].values(), key=lambda f: f.expiry_ts)
        for near, far in zip(live, live[1:]):
            symbol = f"{near.symbol}_{far.symbol}-CAL"
            product_cfg = ProductConfig(
                symbol=symbol,
                underlying=underlying,
                contract_size=1.0,
                tick_size=self.cfg.tick_size,
                leverage=self.cfg.leverage,
                starting_price=0.0,
                allow_negative_price=True,
            )
            self.engine.add_product(product_cfg)
            self.index_service.add_product(product_cfg)
            self.calendar_spreads[underlying][symbol] = CalendarSpreadInstrument(
                symbol=symbol, underlying=underlying, near_symbol=near.symbol, far_symbol=far.symbol,
            )
            self._spawn_mm_bot(symbol)
            self._spawn_noise_bots(symbol)

    def _settle_and_retire_future(self, fut: FutureInstrument, now: float) -> None:
        settlement = self.index_service.get_index_price(fut.underlying, now)
        if settlement is None:
            settlement = 0.0
        for account in self.engine.accounts.values():
            pos = account.positions.get(fut.symbol)
            if pos is None or pos.qty == 0:
                continue
            closing_side = Side.SELL if pos.qty > 0 else Side.BUY
            self.engine.settle_fill(account.id, fut.symbol, closing_side, abs(pos.qty), settlement, now)

        book = self.engine.books.get(fut.symbol)
        if book is not None:
            for order in list(book.bids) + list(book.asks):
                try:
                    self.engine.cancel_order(order.id, order.account_id)
                except OrderRejected:
                    pass

        self.bot_manager.remove_mm_bot(fut.symbol)
        self.bot_manager.remove_noise_bots(fut.symbol)
        self.engine.remove_product(fut.symbol)
        self.index_service.remove_product(fut.symbol)
        self.contracts[fut.underlying].pop(fut.symbol, None)

    def init_chain(self, underlying: str, now: float) -> None:
        for i in range(1, self.cfg.num_live + 1):
            self.create_contract(underlying, now, now + i * self.cfg.window_seconds)
        self._rebuild_calendar_spreads(underlying, now)

    def roll(self, now: float, futures_enabled: bool) -> None:
        """Settle anything past its expiry and, iff futures are currently
        enabled, keep `num_live` contracts live per underlying — but never
        tear down an in-flight contract just because the admin disabled the
        line mid-window, so open positions always settle fairly (same
        contract as OptionsChainManager.roll)."""
        for underlying in self.cfg.underlyings:
            contracts = self.contracts[underlying]
            expired = [f for f in contracts.values() if f.expiry_ts <= now]
            if not expired:
                continue
            for cs in list(self.calendar_spreads[underlying].values()):
                self._retire_calendar_spread(cs)
            for fut in expired:
                self._settle_and_retire_future(fut, now)
            if futures_enabled:
                live = sorted(contracts.values(), key=lambda f: f.expiry_ts)
                while len(live) < self.cfg.num_live:
                    farthest = live[-1].expiry_ts if live else now
                    symbol = self.create_contract(underlying, now, farthest + self.cfg.window_seconds)
                    live.append(contracts[symbol])
                self._rebuild_calendar_spreads(underlying, now)


class FuturesScheduler:
    """Periodically checks every underlying's chain for expired contracts —
    unlike OptionsScheduler, futures expiries are a staggered rolling ladder
    (not one shared clock-aligned window), so there's no single boundary to
    sleep until; a short fixed poll is simplest and roll() itself is a
    cheap no-op whenever nothing has expired yet."""

    def __init__(self, manager: FuturesChainManager, state, poll_seconds: float = 15.0):
        self.manager = manager
        self.state = state  # duck-typed: only .futures_enabled is read
        self.poll_seconds = poll_seconds
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        import asyncio

        while not self._stop:
            await asyncio.sleep(self.poll_seconds)
            if self._stop:
                break
            self.manager.roll(time.time(), self.state.futures_enabled)
