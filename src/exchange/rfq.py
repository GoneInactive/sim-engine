"""Request-for-quote: a student asks the exchange for a two-way market on
a specific product/size, and any other account (student or bot) can
respond with a firm price — first-come liquidity provision, not a
resting order on the shared book. The requester picks whichever quote(s)
they like and accepts; acceptance executes immediately via
MatchingEngine.execute_negotiated_trade at the quoted price, bilaterally
between just those two accounts.

Deliberately not book-based: an RFQ never touches engine.books, so it
can't move the visible market or leave a footprint other students can
front-run. Quotes are only visible to the requester and to the quoting
account itself (see RFQManager.visible_quotes) — quoting dealers can't see
each other's price, same as a real RFQ auction, which also means the
requester (not a race against other requesters) always picks the winner.

Everything here is in-memory only (like the bot/liquidity-event state in
bots.py) — an RFQ is a live negotiation, not something that needs to
survive a restart.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

from .engine import MatchingEngine, OrderRejected
from .models import Side

_rfq_id_counter = itertools.count(1)
_quote_id_counter = itertools.count(1)


def next_rfq_id() -> int:
    return next(_rfq_id_counter)


def next_quote_id() -> int:
    return next(_quote_id_counter)


class RFQError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class Quote:
    id: int
    rfq_id: int
    account_id: str
    price: float
    qty: int
    timestamp: float
    status: str = "open"  # "open" | "accepted" | "rejected" | "withdrawn" | "expired"


@dataclass
class RFQ:
    id: int
    account_id: str  # the requester
    product: str
    side: Side  # direction the requester wants to trade
    qty: int
    remaining_qty: int
    created_at: float
    expires_at: float
    status: str = "open"  # "open" | "filled" | "expired" | "cancelled"
    quotes: dict[int, Quote] = field(default_factory=dict)


DEFAULT_TTL_SECONDS = 30.0
MIN_TTL_SECONDS = 5.0
MAX_TTL_SECONDS = 300.0
MAX_OPEN_RFQS_PER_ACCOUNT = 5


class RFQManager:
    def __init__(self, engine: MatchingEngine, is_tradeable_fn=None):
        self.engine = engine
        # Duck-typed callback (state.py wires AppState.is_tradeable) so a
        # disabled instrument (admin-toggled spread/options/futures line)
        # can't be RFQ'd around the same gate /orders enforces. None (e.g.
        # bare unit tests) means "always tradeable".
        self._is_tradeable_fn = is_tradeable_fn
        self.rfqs: dict[int, RFQ] = {}
        self.rfqs_by_account: dict[str, list[int]] = {}

    def _is_tradeable(self, product: str) -> bool:
        return self._is_tradeable_fn(product) if self._is_tradeable_fn is not None else True

    def _sweep_expired(self, now: float) -> None:
        for rfq in self.rfqs.values():
            if rfq.status == "open" and now >= rfq.expires_at:
                rfq.status = "expired"
                for quote in rfq.quotes.values():
                    if quote.status == "open":
                        quote.status = "expired"

    def _touch(self, rfq_id: int, now: float | None = None) -> RFQ:
        now = now if now is not None else time.time()
        self._sweep_expired(now)
        rfq = self.rfqs.get(rfq_id)
        if rfq is None:
            raise RFQError("no such RFQ")
        return rfq

    def create_rfq(
        self, account_id: str, product: str, side: Side, qty: int, ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> RFQ:
        now = now if now is not None else time.time()
        self._sweep_expired(now)
        if account_id not in self.engine.accounts:
            raise RFQError(f"unknown account {account_id}")
        if product not in self.engine.products:
            raise RFQError(f"unknown product {product}")
        if not self._is_tradeable(product):
            raise RFQError(f"{product} is currently disabled")
        if not isinstance(qty, int) or qty <= 0:
            raise RFQError("qty must be a positive integer")
        ttl_seconds = max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, float(ttl_seconds)))
        open_count = sum(
            1 for rid in self.rfqs_by_account.get(account_id, []) if self.rfqs[rid].status == "open"
        )
        if open_count >= MAX_OPEN_RFQS_PER_ACCOUNT:
            raise RFQError(f"too many open RFQs (max {MAX_OPEN_RFQS_PER_ACCOUNT}) — cancel or wait for one to expire")

        rfq = RFQ(
            id=next_rfq_id(), account_id=account_id, product=product, side=side, qty=qty, remaining_qty=qty,
            created_at=now, expires_at=now + ttl_seconds,
        )
        self.rfqs[rfq.id] = rfq
        self.rfqs_by_account.setdefault(account_id, []).append(rfq.id)
        return rfq

    def cancel_rfq(self, rfq_id: int, account_id: str, now: float | None = None) -> RFQ:
        now = now if now is not None else time.time()
        rfq = self._touch(rfq_id, now)
        if rfq.account_id != account_id:
            raise RFQError("not your RFQ")
        if rfq.status != "open":
            raise RFQError("RFQ is not open")
        rfq.status = "cancelled"
        for quote in rfq.quotes.values():
            if quote.status == "open":
                quote.status = "withdrawn"
        return rfq

    def list_open_rfqs(self, product: str | None = None, now: float | None = None) -> list[RFQ]:
        now = now if now is not None else time.time()
        self._sweep_expired(now)
        return sorted(
            (r for r in self.rfqs.values() if r.status == "open" and (product is None or r.product == product)),
            key=lambda r: r.created_at,
        )

    def get_rfq(self, rfq_id: int, now: float | None = None) -> RFQ | None:
        now = now if now is not None else time.time()
        self._sweep_expired(now)
        return self.rfqs.get(rfq_id)

    def visible_quotes(self, rfq: RFQ, viewer_account_id: str) -> list[Quote]:
        """The requester sees every quote (they need to compare and pick a
        winner); anyone else only ever sees their own — dealers can't see
        a rival's price on the same RFQ."""
        if viewer_account_id == rfq.account_id:
            return sorted(rfq.quotes.values(), key=lambda q: q.timestamp)
        return sorted((q for q in rfq.quotes.values() if q.account_id == viewer_account_id), key=lambda q: q.timestamp)

    def submit_quote(
        self, rfq_id: int, account_id: str, price: float, qty: int | None = None, now: float | None = None,
    ) -> Quote:
        now = now if now is not None else time.time()
        rfq = self._touch(rfq_id, now)
        if rfq.status != "open":
            raise RFQError("RFQ is not open")
        if account_id not in self.engine.accounts:
            raise RFQError(f"unknown account {account_id}")
        if account_id == rfq.account_id:
            raise RFQError("cannot quote your own RFQ")
        qty = rfq.remaining_qty if qty is None else qty
        if not isinstance(qty, int) or qty <= 0:
            raise RFQError("qty must be a positive integer")
        if qty > rfq.remaining_qty:
            raise RFQError(f"qty exceeds the RFQ's remaining size ({rfq.remaining_qty})")
        product_cfg = self.engine.products[rfq.product]
        if price <= 0 and not product_cfg.allow_negative_price:
            raise RFQError(f"price must be positive, got {price}")

        quote = Quote(id=next_quote_id(), rfq_id=rfq.id, account_id=account_id, price=price, qty=qty, timestamp=now)
        rfq.quotes[quote.id] = quote
        return quote

    def withdraw_quote(self, rfq_id: int, quote_id: int, account_id: str, now: float | None = None) -> Quote:
        now = now if now is not None else time.time()
        rfq = self._touch(rfq_id, now)
        quote = rfq.quotes.get(quote_id)
        if quote is None:
            raise RFQError("no such quote")
        if quote.account_id != account_id:
            raise RFQError("not your quote")
        if quote.status != "open":
            raise RFQError("quote is not open")
        quote.status = "withdrawn"
        return quote

    def accept_quote(self, rfq_id: int, quote_id: int, account_id: str, now: float | None = None):
        now = now if now is not None else time.time()
        rfq = self._touch(rfq_id, now)
        if rfq.account_id != account_id:
            raise RFQError("not your RFQ")
        if rfq.status != "open":
            raise RFQError("RFQ is not open")
        quote = rfq.quotes.get(quote_id)
        if quote is None:
            raise RFQError("no such quote")
        if quote.status != "open":
            raise RFQError("quote is not open")
        if not self._is_tradeable(rfq.product):
            # Instrument was disabled after the RFQ/quote were posted —
            # same gate POST /orders enforces, checked again here since a
            # quote can sit open for a while before being accepted.
            raise RFQError(f"{rfq.product} is currently disabled")

        fill_qty = min(quote.qty, rfq.remaining_qty)
        try:
            fill = self.engine.execute_negotiated_trade(
                requester_account_id=rfq.account_id,
                provider_account_id=quote.account_id,
                product=rfq.product,
                requester_side=rfq.side,
                qty=fill_qty,
                price=quote.price,
                now=now,
            )
        except OrderRejected as e:
            raise RFQError(e.reason)

        quote.status = "accepted"
        rfq.remaining_qty -= fill_qty
        for other in rfq.quotes.values():
            if other.id != quote.id and other.status == "open" and other.qty > rfq.remaining_qty:
                # A quote sized for more than what's left of the RFQ can no
                # longer be fully honored — withdraw it rather than leave a
                # stale quote a requester could accept and have silently
                # short-filled.
                other.status = "withdrawn"
        if rfq.remaining_qty <= 0:
            rfq.status = "filled"
            for other in rfq.quotes.values():
                if other.status == "open":
                    other.status = "withdrawn"
        return fill, rfq
