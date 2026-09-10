"""Shared in-process application state: one MatchingEngine, one
IndexPriceService, one BotManager, one AuthStore — built once and handed to
all three FastAPI apps (public, admin, website) so they operate on the same
live state without a network hop between them.

Durable storage (build-spec.md §11) lives in persistence.py — this class
takes an optional PersistenceLog and, when given one, wires it into the
engine's on_fill/on_order hooks and calls it from every other
state-changing chokepoint (register/issue-key/chat). Without one (e.g. in
tests), everything behaves exactly as it always did: in-memory only.
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from typing import TYPE_CHECKING

from .auth import AccountExistsError, AuthStore
from .bots import BotManager
from .config import Config, ProductConfig
from .engine import MatchingEngine
from .futures import FuturesChainManager
from .index_feed import IndexPriceService
from .ledger import apply_adjustment, equity, unrealized_pnl
from .models import next_adjustment_id
from .options import OptionsChainManager, next_boundary
from .rfq import RFQManager

if TYPE_CHECKING:
    from .persistence import PersistenceLog

PRICE_HISTORY_LEN = 120
ADMIN_TRADING_ACCOUNT_ID = "admin"
ADMIN_TRADING_STARTING_CASH = 1_000_000.0
CHAT_HISTORY_LEN = 200
CHAT_MAX_LEN = 300


def _is_bot_account(account_id: str) -> bool:
    return (
        account_id.startswith("mm_")
        or account_id.startswith("noise_")
        or account_id.startswith("arb_")
        or account_id.startswith("insider_")
    )


class AppState:
    def __init__(self, config: Config, persistence_log: "PersistenceLog | None" = None):
        self.config = config
        self.persistence_log = persistence_log
        # index_service is built before the engine so the engine can be
        # wired with a live mark_price_fn (relative MAX_POSITION sizing,
        # see ledger.max_position_for) at construction time.
        self.index_service = IndexPriceService(config.feed, config.products)
        self.engine = MatchingEngine(
            config.products, config.fees.maker_bps, config.fees.taker_bps,
            on_fill=self._on_fill, on_order=self._on_order,
            mark_price_fn=self.index_service.get_index_price,
        )
        self.bot_manager = BotManager(
            self.engine, self.index_service, config.accounts.starting_cash, mm_defaults=config.mm_bots.default,
        )
        self.bot_manager.spawn_defaults(list(config.products.keys()))
        self.auth = AuthStore(config.admin_password, config.website_password)
        self.feed_mode: dict[str, str] = {symbol: "live" for symbol in config.products}
        self.replay_speed: float = 1.0
        self.price_history: dict[str, deque] = {
            symbol: deque(maxlen=PRICE_HISTORY_LEN) for symbol in config.products
        }
        self.starting_cash_by_account: dict[str, float] = {}
        # Mutable, admin-adjustable synthetic-feed params (§7 "adjust bot
        # parameters live" extended to the price process itself) — the
        # feed client reads this dict fresh every tick, not config.products
        # directly, so an admin change takes effect immediately.
        self.synthetic_params: dict[str, dict] = {
            symbol: {"annual_drift": cfg.annual_drift, "annual_volatility": cfg.annual_volatility}
            for symbol, cfg in config.products.items()
        }

        # The presenter can trade too, well-capitalized, logging in as
        # "admin" with the admin panel password.
        self.engine.get_or_create_account(ADMIN_TRADING_ACCOUNT_ID, ADMIN_TRADING_STARTING_CASH)
        self.starting_cash_by_account[ADMIN_TRADING_ACCOUNT_ID] = ADMIN_TRADING_STARTING_CASH
        self.engine.unlimited_position_accounts.add(ADMIN_TRADING_ACCOUNT_ID)
        self.auth.register(ADMIN_TRADING_ACCOUNT_ID, config.admin_password)

        # -- BTC-ETH mini spread instrument ---------------------------------
        # Registered unconditionally (own order book, own bots) but never
        # added to config.products — it lives only in engine/index_service
        # so the homepage's product grid and admin Market tab are unaffected.
        # `spread_enabled` gates student order submission and its bots'
        # `.active` flag; the book itself always exists.
        spread_cfg = ProductConfig(
            symbol=config.spread.symbol,
            underlying=f"{config.spread.btc_product}-{config.spread.eth_product}",
            contract_size=config.spread.contract_size,
            tick_size=config.spread.tick_size,
            leverage=config.spread.leverage,
            allow_negative_price=True,
        )
        self.engine.add_product(spread_cfg)
        self.index_service.add_product(spread_cfg)
        self.bot_manager.spawn_defaults([config.spread.symbol])
        self.spread_enabled = config.spread.enabled_default
        if not self.spread_enabled:
            self._set_bots_active(config.spread.symbol, False)
        self.price_history[config.spread.symbol] = deque(maxlen=PRICE_HISTORY_LEN)

        # -- rolling option chains ---------------------------------------
        self.options_managers: dict[str, OptionsChainManager] = {}
        self.options_enabled: dict[str, bool] = {}
        for chain_id, occfg in config.options.items():
            manager = OptionsChainManager(
                self.engine, self.index_service, self.bot_manager, occfg, config.mm_bots.options, config.noise_bots.options,
            )
            self.options_managers[chain_id] = manager
            enabled = occfg.enabled_default
            self.options_enabled[chain_id] = enabled
            if enabled:
                now = time.time()
                manager.create_chain(now, next_boundary(now, occfg.window_seconds))

        # -- 1-hour rolling futures + calendar spreads --------------------
        self.futures_manager = FuturesChainManager(
            self.engine, self.index_service, self.bot_manager, config.futures, config.mm_bots.futures,
            config.noise_bots.futures,
        )
        self.futures_enabled = config.futures.enabled_default
        if self.futures_enabled:
            now = time.time()
            for underlying in config.futures.underlyings:
                self.futures_manager.init_chain(underlying, now)

        # -- RFQs -------------------------------------------------------
        self.rfq_manager = RFQManager(self.engine, self.is_tradeable)

        # -- insider bots ---------------------------------------------------
        if config.insider_bots.enabled_default:
            for _ in range(config.insider_bots.count):
                self.bot_manager.spawn_insider_bot(
                    lead_seconds=config.insider_bots.lead_seconds,
                    size=config.insider_bots.size,
                    hold_after_seconds=config.insider_bots.hold_after_seconds,
                )

        # -- global chat ---------------------------------------------------
        # One shared room, everyone with an account can post — durable via
        # persistence_log when one is configured (see persistence.py).
        self.chat_messages: deque[dict] = deque(maxlen=CHAT_HISTORY_LEN)
        self._next_chat_id = itertools.count(1)

    # -- persistence hooks, wired into MatchingEngine above -----------------
    def _on_fill(self, fill) -> None:
        if self.persistence_log is None:
            return
        # settle_fill uses id 0 for both order ids (there's no real
        # counterparty order for a settlement) — that's the signal this
        # fill never had a fee charged, distinct from "0 bps" which is a
        # real, deliberately-zero fee rate.
        is_settlement = fill.maker_order_id == 0 and fill.taker_order_id == 0
        maker_fee_bps = None if is_settlement else self.engine.maker_fee_bps
        taker_fee_bps = None if is_settlement else self.engine.taker_fee_bps
        self.persistence_log.log_fill(fill, maker_fee_bps, taker_fee_bps)

    def _on_order(self, order) -> None:
        # Fills are always logged (a bot-vs-student fill still moves a
        # real student's cash — see _on_fill), but a bot's own resting
        # orders are pure system noise: bot accounts are never restored,
        # so their order rows would just accumulate unboundedly over a
        # long-running session for nothing.
        if self.persistence_log is not None and not _is_bot_account(order.account_id):
            self.persistence_log.log_order(order)

    def post_chat_message(self, account_id: str, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("message is empty")
        if len(text) > CHAT_MAX_LEN:
            text = text[:CHAT_MAX_LEN]
        message = {
            "id": next(self._next_chat_id),
            "account_id": account_id,
            "text": text,
            "timestamp": time.time(),
        }
        self.chat_messages.append(message)
        if self.persistence_log is not None:
            self.persistence_log.log_chat(message)
        return message

    def _set_bots_active(self, product: str, active: bool) -> None:
        for bot in self.bot_manager.mm_bots:
            if bot.product == product:
                bot.config.active = active
        for bot in self.bot_manager.noise_bots:
            if bot.product == product:
                bot.config.active = active
        for bot in self.bot_manager.arb_bots:
            if bot.product == product:
                bot.config.active = active

    def set_spread_enabled(self, enabled: bool) -> bool:
        self.spread_enabled = enabled
        self._set_bots_active(self.config.spread.symbol, enabled)
        return self.spread_enabled

    def set_options_enabled(self, chain_id: str, enabled: bool) -> bool:
        manager = self.options_managers[chain_id]
        self.options_enabled[chain_id] = enabled
        if enabled and not manager.chain:
            now = time.time()
            manager.create_chain(now, next_boundary(now, manager.cfg.window_seconds))
        return self.options_enabled[chain_id]

    def set_futures_enabled(self, enabled: bool) -> bool:
        self.futures_enabled = enabled
        if enabled:
            now = time.time()
            for underlying in self.config.futures.underlyings:
                if not self.futures_manager.contracts[underlying]:
                    self.futures_manager.init_chain(underlying, now)
        return self.futures_enabled

    def adjust_balance(self, account_id: str, delta: float, reason: str) -> float:
        account = self.engine.accounts.get(account_id)
        if account is None:
            raise KeyError(account_id)
        new_cash = apply_adjustment(account, delta)
        if self.persistence_log is not None:
            self.persistence_log.log_adjustment(next_adjustment_id(), account_id, delta, reason, time.time())
        return new_cash

    def is_tradeable(self, symbol: str) -> bool:
        if symbol == self.config.spread.symbol:
            return self.spread_enabled
        for chain_id, manager in self.options_managers.items():
            if symbol in manager.chain:
                return self.options_enabled[chain_id]
        if symbol in self.futures_manager.all_symbols():
            return self.futures_enabled
        return True

    def _book_mid(self, product: str) -> tuple[float | None, float | None, float | None]:
        """Returns (best_bid, best_ask, mid) — mid is None (not a fallback
        value) whenever the book is one-sided, so callers that display it
        as a stat can honestly show "n/a" instead of silently substituting
        something else."""
        book = self.engine.book_snapshot(product, depth=1)
        best_bid = book["bids"][0]["price"] if book["bids"] else None
        best_ask = book["asks"][0]["price"] if book["asks"] else None
        mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
        return best_bid, best_ask, mid

    def record_price_tick(self, now: float | None = None) -> None:
        """Sparkline/chart data source: the book's own midpoint, not the
        synthetic index — this is what actually moves with real order flow
        (buys and sells), so the chart shows the traded market, not just
        the theo the MM bots price around. Falls back to the index price
        only here (to keep the series continuous) when the book is
        momentarily one-sided, e.g. before the first MM bot has quoted."""
        now = now if now is not None else time.time()
        for product in (*self.config.products, self.config.spread.symbol):
            _, _, mid = self._book_mid(product)
            if mid is None:
                mid = self.index_service.get_index_price(product, now)
            if mid is not None:
                self.price_history[product].append(mid)

    def market_snapshot(self, product: str, now: float | None = None) -> dict:
        """Book + index/mid/spread/last-trade + sparkline for `product`.
        Shared by the website's order book page and the admin page's
        market panel so a presenter running admin actions doesn't have to
        leave the admin panel to see what happened."""
        now = now if now is not None else time.time()
        last_trade = None
        last_trade_qty = None
        last_trade_ts = None
        last_trade_side = None
        for fill in reversed(self.engine.trade_tape):
            if fill.product == product:
                last_trade = fill.price
                last_trade_qty = fill.qty
                last_trade_ts = fill.timestamp
                last_trade_side = fill.taker_side.value
                break
        book = self.engine.book_snapshot(product)
        best_bid, best_ask, mid = self._book_mid(product)
        spread_bps = (best_ask - best_bid) / mid * 10000 if mid else None
        return {
            "book": book,
            "index_price": self.index_service.get_index_price(product, now),
            "stale": self.index_service.is_stale(product),
            "last_trade": last_trade,
            "last_trade_qty": last_trade_qty,
            "last_trade_ts": last_trade_ts,
            "last_trade_side": last_trade_side,
            "mid": mid,
            "spread_bps": spread_bps,
            "sparkline": list(self.price_history.get(product, [])),
            "session_volume_qty": self.engine.volume_qty.get(product, 0),
            "session_volume_notional": self.engine.volume_notional.get(product, 0.0),
        }

    def set_synthetic_params(
        self, product: str, annual_volatility: float | None = None, annual_drift: float | None = None
    ) -> dict:
        params = self.synthetic_params[product]
        if annual_volatility is not None:
            params["annual_volatility"] = annual_volatility
        if annual_drift is not None:
            params["annual_drift"] = annual_drift
        return params

    def _log_account_and_credentials(self, account_id: str, starting_cash: float, record) -> None:
        if self.persistence_log is None:
            return
        self.persistence_log.log_account(account_id, starting_cash)
        self.persistence_log.log_credentials(
            account_id, record.key, record.active, record.password_salt, record.password_hash,
        )

    def register_student(self, account_id: str, password: str, client_ip: str | None = None) -> str:
        """Self-serve registration: active immediately, no admin approval
        step — unless `client_ip` has already self-registered
        AuthStore.MAX_SELF_SERVE_ACCOUNTS_PER_IP accounts, in which case
        this one is created inactive and needs an admin to activate it (see
        AuthStore.register). Deposits the starting cash on account
        creation either way."""
        self.engine.get_or_create_account(account_id, self.config.accounts.starting_cash)
        self.starting_cash_by_account.setdefault(account_id, self.config.accounts.starting_cash)
        record = self.auth.register(account_id, password, client_ip)
        self._log_account_and_credentials(account_id, self.config.accounts.starting_cash, record)
        return record.key

    def login_student(self, account_id: str, password: str) -> str | None:
        record = self.auth.login(account_id, password)
        if record is None:
            return None
        # login() can itself claim a still-unclaimed account (see
        # auth.py) — that sets a real password_hash for the first time,
        # which must be persisted just like a normal register() would be,
        # or it's back to unclaimed on the next restart.
        if self.persistence_log is not None:
            self.persistence_log.log_credentials(
                account_id, record.key, record.active, record.password_salt, record.password_hash,
            )
        return record.key

    def admin_issue_key(self, account_id: str) -> str:
        """Admin-panel driven account creation: inactive until an admin
        explicitly activates it (§7). No password."""
        self.engine.get_or_create_account(account_id, self.config.accounts.starting_cash)
        self.starting_cash_by_account.setdefault(account_id, self.config.accounts.starting_cash)
        record = self.auth.issue_key(account_id)
        self._log_account_and_credentials(account_id, self.config.accounts.starting_cash, record)
        return record.key

    def index_prices(self, now: float | None = None) -> dict[str, float]:
        now = now if now is not None else time.time()
        return self.index_service.get_all_index_prices(now)

    def leaderboard(self, now: float | None = None) -> list[dict]:
        prices = self.index_prices(now)
        rows = []
        for account_id, account in self.engine.accounts.items():
            if _is_bot_account(account_id):
                continue  # bots don't show on the student leaderboard
            if account_id == ADMIN_TRADING_ACCOUNT_ID:
                continue  # the house account isn't a student to rank against
            rows.append(
                {
                    "account_id": account_id,
                    "cash": account.cash,
                    "equity": equity(account, prices),
                    "positions": {p: pos.qty for p, pos in account.positions.items() if pos.qty != 0},
                }
            )
        rows.sort(key=lambda r: r["equity"], reverse=True)
        return rows

    def fill_view(self, f, account_id: str) -> dict:
        # This account's own side: whatever the taker did if we were the
        # taker, the opposite if we were the resting maker — inferring it
        # as "buy iff taker" (as an earlier version of this did) is wrong
        # whenever the taker was the one selling into a resting bid.
        is_taker = f.taker_account_id == account_id
        if is_taker:
            side = f.taker_side.value
        else:
            side = "sell" if f.taker_side.value == "buy" else "buy"
        role = "taker" if is_taker else "maker"
        fee_bps = self.engine.taker_fee_bps if is_taker else self.engine.maker_fee_bps
        fee = f.price * f.qty * fee_bps / 10_000
        return {
            "id": f.id,
            "product": f.product,
            "price": f.price,
            "qty": f.qty,
            "timestamp": f.timestamp,
            "side": side,
            "role": role,
            "fee": fee,
            "counterparty": f.maker_account_id if is_taker else f.taker_account_id,
        }

    def quote_view(self, quote) -> dict:
        return {
            "id": quote.id,
            "rfq_id": quote.rfq_id,
            "account_id": quote.account_id,
            "price": quote.price,
            "qty": quote.qty,
            "timestamp": quote.timestamp,
            "status": quote.status,
        }

    def rfq_view(self, rfq, viewer_account_id: str) -> dict:
        """Shared by the public API's /rfqs routes and the website's 'rfqs'
        WS channel so there's one implementation of the requester-only
        quote-visibility rule (see rfq.RFQManager.visible_quotes)."""
        return {
            "id": rfq.id,
            "account_id": rfq.account_id,
            "own": rfq.account_id == viewer_account_id,
            "product": rfq.product,
            "side": rfq.side.value,
            "qty": rfq.qty,
            "remaining_qty": rfq.remaining_qty,
            "created_at": rfq.created_at,
            "expires_at": rfq.expires_at,
            "status": rfq.status,
            "quotes": [self.quote_view(q) for q in self.rfq_manager.visible_quotes(rfq, viewer_account_id)],
        }

    def portfolio(self, account_id: str, fill_limit: int = 50) -> dict:
        account = self.engine.accounts[account_id]
        prices = self.index_prices()
        fills = [
            self.fill_view(f, account_id) for f in self.engine.fills_by_account.get(account_id, [])
        ][-fill_limit:][::-1]
        orders = [
            {
                "id": o.id,
                "product": o.product,
                "side": o.side.value,
                "type": o.type.value,
                "qty": o.qty,
                "price": o.price,
                "remaining_qty": o.remaining_qty,
                "status": o.status.value,
            }
            for o in self.engine.orders_by_account.get(account_id, {}).values()
        ]
        starting_cash = self.starting_cash_by_account.get(account_id, self.config.accounts.starting_cash)
        return {
            "account_id": account.id,
            "cash": account.cash,
            "balance": account.cash,
            "realized_pnl": account.cash - starting_cash,
            "positions": {
                p: {"qty": pos.qty, "avg_cost": pos.avg_cost}
                for p, pos in account.positions.items()
                if pos.qty != 0
            },
            "unrealized_pnl": unrealized_pnl(account, prices),
            "equity": equity(account, prices),
            "frozen": account.frozen,
            "recent_fills": fills,
            "open_orders": [o for o in orders if o["status"] in ("open", "partially_filled")],
        }
