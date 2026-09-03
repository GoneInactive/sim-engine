"""Shared in-process application state: one MatchingEngine, one
IndexPriceService, one BotManager, one AuthStore — built once and handed to
all three FastAPI apps (public, admin, website) so they operate on the same
live state without a network hop between them.

Persistence to Postgres (build-spec.md §11) is deferred until this moves
off a single dev machine; for now, all state is in-memory and does not
survive a process restart.
"""
from __future__ import annotations

import time
from collections import deque

from .auth import AccountExistsError, AuthStore
from .bots import BotManager
from .config import Config
from .engine import MatchingEngine
from .index_feed import IndexPriceService
from .ledger import equity, unrealized_pnl

PRICE_HISTORY_LEN = 120
ADMIN_TRADING_ACCOUNT_ID = "admin"
ADMIN_TRADING_STARTING_CASH = 1_000_000.0


class AppState:
    def __init__(self, config: Config):
        self.config = config
        self.engine = MatchingEngine(config.products, config.fees.maker_bps, config.fees.taker_bps)
        self.index_service = IndexPriceService(config.feed, config.products)
        self.bot_manager = BotManager(self.engine, self.index_service, config.accounts.starting_cash)
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
        for product in self.config.products:
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

    def register_student(self, account_id: str, password: str) -> str:
        """Self-serve registration: active immediately, no admin approval
        step. Deposits the starting cash on account creation."""
        self.engine.get_or_create_account(account_id, self.config.accounts.starting_cash)
        self.starting_cash_by_account.setdefault(account_id, self.config.accounts.starting_cash)
        record = self.auth.register(account_id, password)
        return record.key

    def login_student(self, account_id: str, password: str) -> str | None:
        record = self.auth.login(account_id, password)
        return record.key if record is not None else None

    def admin_issue_key(self, account_id: str) -> str:
        """Admin-panel driven account creation: inactive until an admin
        explicitly activates it (§7). No password."""
        self.engine.get_or_create_account(account_id, self.config.accounts.starting_cash)
        self.starting_cash_by_account.setdefault(account_id, self.config.accounts.starting_cash)
        record = self.auth.issue_key(account_id)
        return record.key

    def index_prices(self, now: float | None = None) -> dict[str, float]:
        now = now if now is not None else time.time()
        return self.index_service.get_all_index_prices(now)

    def leaderboard(self, now: float | None = None) -> list[dict]:
        prices = self.index_prices(now)
        rows = []
        for account_id, account in self.engine.accounts.items():
            if account_id.startswith("mm_") or account_id.startswith("noise_") or account_id.startswith("arb_"):
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
