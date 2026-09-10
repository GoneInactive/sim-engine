"""Price-time priority limit order book + matching engine.

Order acceptance (§5 of build-spec.md):
  - qty must be a positive integer.
  - post-trade position must stay within the product's symmetric
    MAX_POSITION cap. Enforced using the order's *full* requested qty
    (worst case, as if it fills completely) at acceptance time — an order
    that would breach the cap even in the worst case is rejected outright,
    not partially accepted / reduce-only. This is a deliberate
    simplification flagged in build-spec.md §3.

Matching: incoming (taker) order trades against resting (maker) orders at
the maker's price, in price-time priority. Market orders are IOC: whatever
doesn't fill immediately is dropped, never rests on the book.
"""
from __future__ import annotations

import bisect
import itertools
import time
from dataclasses import dataclass
from typing import Callable

from . import ledger
from .config import ProductConfig
from .models import (
    Account,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Side,
    next_fill_id,
    next_order_id,
)


class OrderRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _Book:
    """One side-agnostic price-time priority book for a single product."""

    def __init__(self) -> None:
        self.bids: list[Order] = []  # best (highest price, then earliest) first
        self.asks: list[Order] = []  # best (lowest price, then earliest) first

    @staticmethod
    def _bid_key(order: Order) -> tuple:
        return (-order.price, order.timestamp, order.id)

    @staticmethod
    def _ask_key(order: Order) -> tuple:
        return (order.price, order.timestamp, order.id)

    def insert_resting(self, order: Order) -> None:
        if order.side is Side.BUY:
            keys = [self._bid_key(o) for o in self.bids]
            idx = bisect.bisect_left(keys, self._bid_key(order))
            self.bids.insert(idx, order)
        else:
            keys = [self._ask_key(o) for o in self.asks]
            idx = bisect.bisect_left(keys, self._ask_key(order))
            self.asks.insert(idx, order)

    def opposite_side(self, side: Side) -> list[Order]:
        return self.asks if side is Side.BUY else self.bids

    def remove(self, order: Order) -> None:
        side_list = self.bids if order.side is Side.BUY else self.asks
        try:
            side_list.remove(order)
        except ValueError:
            pass

    def best_bid(self) -> Order | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Order | None:
        return self.asks[0] if self.asks else None

    @staticmethod
    def _levels(orders: list[Order], depth: int) -> list[dict]:
        """Aggregate consecutive same-price resting orders into one price
        level (qty summed). The list is already sorted by price first
        (see _bid_key/_ask_key), so equal prices are contiguous and a
        plain groupby is enough — no re-sort needed."""
        levels: list[dict] = []
        for price, group in itertools.groupby(orders, key=lambda o: o.price):
            levels.append({"price": price, "qty": sum(o.remaining_qty for o in group)})
            if len(levels) >= depth:
                break
        return levels

    def snapshot(self, depth: int = 10) -> dict:
        return {
            "bids": self._levels(self.bids, depth),
            "asks": self._levels(self.asks, depth),
        }


def _crosses(taker_side: Side, taker_price: float | None, maker_price: float) -> bool:
    if taker_price is None:  # market order always crosses
        return True
    if taker_side is Side.BUY:
        return taker_price >= maker_price
    return taker_price <= maker_price


class MatchingEngine:
    def __init__(
        self,
        products: dict[str, ProductConfig],
        maker_fee_bps: float = -1.0,
        taker_fee_bps: float = 2.0,
        on_fill: Callable[[Fill], None] | None = None,
        on_order: Callable[[Order], None] | None = None,
        mark_price_fn: Callable[[str, float], float | None] | None = None,
    ):
        # Optional persistence hooks (default no-op) — called at the exact
        # points a Fill/Order is created or has its status change, so a
        # caller can durably log every state-changing event without this
        # module knowing anything about how or where it's stored. This is
        # the single chokepoint for *every* fill regardless of origin
        # (student order, MM bot, arb bot correction) since a bot-vs-
        # student fill still moves a real student's cash.
        self._on_fill = on_fill
        self._on_order = on_order
        # Balance-relative MAX_POSITION (see ledger.max_position_for) needs
        # a current mark price to size against — supplied by the caller
        # (state.py wires this to IndexPriceService.get_index_price) since
        # the engine itself has no price feed of its own. None (e.g. in
        # tests that construct a bare MatchingEngine) falls back to each
        # product's configured starting_price.
        self.mark_price_fn = mark_price_fn
        # Copied, not aliased: add_product/remove_product mutate this dict
        # at runtime (dynamic option contracts, the spread instrument), and
        # must never leak those changes back into the caller's own dict —
        # historically the same object as Config.products, whose whole
        # point is to stay exactly the statically-configured product set.
        self.products = dict(products)
        self.books: dict[str, _Book] = {symbol: _Book() for symbol in products}
        self.accounts: dict[str, Account] = {}
        self.orders: dict[int, Order] = {}
        self.trade_tape: list[Fill] = []
        # Running totals, updated incrementally per fill rather than
        # rescanning trade_tape on every request — that list only grows for
        # the life of the process and this is read on every ladder poll.
        self.volume_qty: dict[str, int] = {symbol: 0 for symbol in products}
        self.volume_notional: dict[str, float] = {symbol: 0.0 for symbol in products}
        # Incremental per-account indexes. Without these, MAX_POSITION
        # (checked on *every* order submission, including every bot
        # repost), GET /orders, and GET /fills all scanned the full
        # lifetime self.orders / trade_tape — O(everyone's order/fill
        # history ever) per call. That gets slower as the session runs and,
        # since MM bots reprice immediately on a fill or a price move past
        # threshold (see bots.py), *faster* growing exactly when the
        # market is moving a lot — the "slow during high moves" symptom.
        self.orders_by_account: dict[str, dict[int, Order]] = {}
        self.resting_orders_by_account_product: dict[str, dict[str, dict[int, Order]]] = {}
        self.fills_by_account: dict[str, list[Fill]] = {}
        # Applied to every fill's notional, both sides, independent of the
        # §3 realized-PnL cash rule — a negative maker_fee_bps is a rebate.
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        # Accounts exempt from MAX_POSITION (the admin/house trading
        # account — it needs to be able to absorb size without the same
        # cap a student is bounded by).
        self.unlimited_position_accounts: set[str] = set()

    # -- dynamic products ------------------------------------------------
    def add_product(self, cfg: ProductConfig) -> None:
        """Registers a new tradeable instrument at runtime (options
        chain contracts, the spread instrument) without disturbing any
        product registered at construction time."""
        self.products[cfg.symbol] = cfg
        self.books[cfg.symbol] = _Book()
        self.volume_qty[cfg.symbol] = 0
        self.volume_notional[cfg.symbol] = 0.0

    def remove_product(self, symbol: str) -> None:
        """Retires an instrument (e.g. an expired option contract) once
        it has been fully settled — no open orders/positions should
        remain against it. Trade/fill history referencing the symbol is
        untouched (it's just a string on old Fill/Order records)."""
        self.products.pop(symbol, None)
        self.books.pop(symbol, None)
        self.volume_qty.pop(symbol, None)
        self.volume_notional.pop(symbol, None)

    # -- accounts -----------------------------------------------------
    def get_or_create_account(self, account_id: str, starting_cash: float) -> Account:
        if account_id not in self.accounts:
            self.accounts[account_id] = Account(id=account_id, cash=starting_cash)
        return self.accounts[account_id]

    # -- incremental indexes -----------------------------------------------
    def _add_resting(self, order: Order) -> None:
        self.resting_orders_by_account_product.setdefault(order.account_id, {}).setdefault(
            order.product, {}
        )[order.id] = order

    def _remove_resting(self, order: Order) -> None:
        product_map = self.resting_orders_by_account_product.get(order.account_id)
        if product_map:
            order_map = product_map.get(order.product)
            if order_map:
                order_map.pop(order.id, None)

    # -- orders ---------------------------------------------------------
    def submit_order(
        self,
        account_id: str,
        product: str,
        side: Side,
        type_: OrderType,
        qty: int,
        price: float | None,
        now: float | None = None,
    ) -> Order:
        if product not in self.products:
            raise OrderRejected(f"unknown product {product}")
        if not isinstance(qty, int) or qty <= 0:
            raise OrderRejected("qty must be a positive integer")
        if type_ is OrderType.LIMIT:
            if price is None:
                raise OrderRejected("limit order requires a price")
            if price <= 0 and not self.products[product].allow_negative_price:
                # ProductConfig.allow_negative_price defaults to True for
                # every product now — this branch only fires for a product
                # some caller explicitly opted out (none currently do).
                # Kept as a real check, not deleted, since a future product
                # config could still want a positive-price floor and the
                # cost of keeping the guard is nothing.
                raise OrderRejected(f"price must be positive, got {price}")
            tick_size = self.products[product].tick_size
            ticks = price / tick_size
            if abs(ticks - round(ticks)) > 1e-6:
                raise OrderRejected(
                    f"price {price} is not a multiple of tick_size ({tick_size}) for {product}"
                )

        account = self.accounts.get(account_id)
        if account is None:
            raise OrderRejected(f"unknown account {account_id}")
        if account.frozen:
            raise OrderRejected("account is frozen")

        product_cfg = self.products[product]
        now_ts = now if now is not None else time.time()
        if account_id not in self.unlimited_position_accounts:
            pos = account.position_for(product)
            # Worst case exposure: current filled position, plus every resting
            # order this account already has open on this product (they could
            # all fill), plus this new order's full qty. Reads the small
            # per-account/product resting index, not the full order history.
            resting = self.resting_orders_by_account_product.get(account_id, {}).get(product, {})
            committed = pos.qty + sum(o.side.sign * o.remaining_qty for o in resting.values())
            prospective = committed + side.sign * qty
            mark_price = self.mark_price_fn(product, now_ts) if self.mark_price_fn else None
            if mark_price is None:
                # Same contract-scaling IndexPriceService.on_raw_tick applies
                # to every real tick — starting_price alone is the *raw*
                # underlying price for a base product (e.g. BTC-MINI's
                # 77500.0), not the ~77.5 contract-notional price everything
                # else in this cap is denominated in.
                mark_price = product_cfg.starting_price * product_cfg.contract_size
            max_position = ledger.max_position_for(account.cash, product_cfg.leverage, mark_price)
            if abs(prospective) > max_position:
                raise OrderRejected(
                    f"order would breach MAX_POSITION ({max_position}, {product_cfg.leverage}x balance) for {product}"
                )

        order = Order(
            id=next_order_id(),
            account_id=account_id,
            product=product,
            side=side,
            type=type_,
            qty=qty,
            price=price,
            remaining_qty=qty,
            status=OrderStatus.OPEN,
            timestamp=now_ts,
        )
        self.orders[order.id] = order
        self.orders_by_account.setdefault(account_id, {})[order.id] = order
        self._match(order)

        if order.remaining_qty > 0:
            if order.type is OrderType.LIMIT:
                self.books[product].insert_resting(order)
                self._add_resting(order)
            else:
                # Market order: IOC — whatever didn't fill is dropped, not
                # left resting. _match already sets status to FILLED or
                # PARTIALLY_FILLED for any real fill; a market order that
                # matched nothing at all is left as OPEN by _match (it only
                # touches .status on an actual fill), so that case needs
                # handling here. Either way the remainder is done, not
                # pending, so remaining_qty must go to zero — leaving it
                # nonzero is what let a market order into a thin/empty book
                # sit forever as "open" with remaining_qty == qty and a
                # null price, indistinguishable from a real resting order.
                if order.status is OrderStatus.OPEN:
                    order.status = OrderStatus.CANCELLED
                order.remaining_qty = 0
        if self._on_order is not None:
            self._on_order(order)
        return order

    def cancel_order(self, order_id: int, account_id: str) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            raise OrderRejected("no such order")
        if order.account_id != account_id:
            raise OrderRejected("not your order")
        if not order.is_resting:
            raise OrderRejected("order is not open")
        self.books[order.product].remove(order)
        order.status = OrderStatus.CANCELLED
        self._remove_resting(order)
        if self._on_order is not None:
            self._on_order(order)
        return order

    def kill_account_orders(self, account_id: str) -> list[Order]:
        killed = []
        for order in list(self.orders_by_account.get(account_id, {}).values()):
            if order.is_resting:
                self.books[order.product].remove(order)
                order.status = OrderStatus.CANCELLED
                self._remove_resting(order)
                killed.append(order)
                if self._on_order is not None:
                    self._on_order(order)
        return killed

    # -- matching ---------------------------------------------------------
    def _match(self, taker: Order) -> None:
        book = self.books[taker.product]
        opposite = book.opposite_side(taker.side)

        while taker.remaining_qty > 0 and opposite:
            maker = opposite[0]
            if not _crosses(taker.side, taker.price, maker.price):
                break

            fill_qty = min(taker.remaining_qty, maker.remaining_qty)
            fill_price = maker.price

            taker_account = self.accounts[taker.account_id]
            maker_account = self.accounts[maker.account_id]
            ledger.apply_fill(taker_account, taker.product, taker.side, fill_qty, fill_price)
            ledger.apply_fill(maker_account, maker.product, maker.side, fill_qty, fill_price)

            notional = fill_price * fill_qty
            maker_account.cash -= notional * self.maker_fee_bps / 10_000
            taker_account.cash -= notional * self.taker_fee_bps / 10_000

            taker.remaining_qty -= fill_qty
            maker.remaining_qty -= fill_qty

            fill = Fill(
                id=next_fill_id(),
                product=taker.product,
                price=fill_price,
                qty=fill_qty,
                timestamp=time.time(),
                maker_order_id=maker.id,
                taker_order_id=taker.id,
                maker_account_id=maker.account_id,
                taker_account_id=taker.account_id,
                taker_side=taker.side,
            )
            self.trade_tape.append(fill)
            self.fills_by_account.setdefault(fill.maker_account_id, []).append(fill)
            if fill.taker_account_id != fill.maker_account_id:
                self.fills_by_account.setdefault(fill.taker_account_id, []).append(fill)
            self.volume_qty[taker.product] += fill_qty
            self.volume_notional[taker.product] += notional
            if self._on_fill is not None:
                self._on_fill(fill)

            if maker.remaining_qty == 0:
                maker.status = OrderStatus.FILLED
                opposite.pop(0)
                self._remove_resting(maker)
            else:
                maker.status = OrderStatus.PARTIALLY_FILLED
            # The maker's own status just changed but submit_order (which
            # only sees the taker) won't emit it — this is the only place
            # that ever happens, so it must fire here.
            if self._on_order is not None:
                self._on_order(maker)

        if taker.remaining_qty == 0:
            taker.status = OrderStatus.FILLED
        elif taker.remaining_qty < taker.qty:
            taker.status = OrderStatus.PARTIALLY_FILLED
        # else: still OPEN (limit) — caller rests it if applicable

    def settle_fill(
        self, account_id: str, product: str, side: Side, qty: int, price: float, now: float | None = None
    ) -> Fill:
        """One-sided settlement (e.g. an option's cash settlement at
        expiry) — no counterparty and no fee, since it isn't a real
        market trade, just the ledger's existing close-PnL rule applied
        directly. Still produces a real Fill row (maker/taker both set to
        the settling account) so it shows up in that account's /fills
        history and goes through the same on_fill persistence hook as
        every other fill, instead of silently mutating cash with nothing
        to show for it afterward."""
        account = self.accounts[account_id]
        ledger.apply_fill(account, product, side, qty, price)
        fill = Fill(
            id=next_fill_id(),
            product=product,
            price=price,
            qty=qty,
            timestamp=now if now is not None else time.time(),
            maker_order_id=0,
            taker_order_id=0,
            maker_account_id=account_id,
            taker_account_id=account_id,
            taker_side=side,
        )
        self.trade_tape.append(fill)
        self.fills_by_account.setdefault(account_id, []).append(fill)
        self.volume_qty[product] = self.volume_qty.get(product, 0) + qty
        self.volume_notional[product] = self.volume_notional.get(product, 0.0) + price * qty
        if self._on_fill is not None:
            self._on_fill(fill)
        return fill

    def book_snapshot(self, product: str, depth: int = 10) -> dict:
        return self.books[product].snapshot(depth)
