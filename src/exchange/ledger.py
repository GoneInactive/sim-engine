"""Cash/position accounting.

Implements the §3 cash rule from build-spec.md: cash only moves when a
position is closed or reduced (realized PnL on that trade); opening or
adding to a position never touches cash upfront.

Fills that flip a position through zero are split into two legs — close
the existing position at the fill price (realize PnL), then open a fresh
position in the new direction at that same fill price — rather than netting
PnL across the whole fill in one step. See the worked example in
build-spec.md §3.
"""
from __future__ import annotations

from .models import Account, Position, Side


def apply_fill(account: Account, product: str, side: Side, qty: int, price: float) -> float:
    """Apply one fill (qty contracts of `side`, at `price`) to `account`'s
    position in `product`. Returns realized PnL from this fill (0.0 if the
    fill only opened/added to a position)."""
    pos = account.position_for(product)
    signed_qty = side.sign * qty
    realized = 0.0

    same_direction = pos.qty == 0 or (pos.qty > 0) == (signed_qty > 0)
    if same_direction:
        total_qty = abs(pos.qty) + qty
        pos.avg_cost = (abs(pos.qty) * pos.avg_cost + qty * price) / total_qty
        pos.qty += signed_qty
        return 0.0

    # Reducing and/or flipping through zero.
    closing_qty = min(qty, abs(pos.qty))
    if pos.qty > 0:
        realized = closing_qty * (price - pos.avg_cost)
    else:
        realized = closing_qty * (pos.avg_cost - price)
    account.cash += realized

    pos.qty += signed_qty
    remaining = qty - closing_qty
    if remaining > 0:
        # Flipped through zero: the leftover qty opens a brand new position.
        pos.avg_cost = price
    elif pos.qty == 0:
        pos.avg_cost = 0.0

    return realized


def apply_adjustment(account: Account, delta: float) -> float:
    """Admin-driven direct balance change (deposit/withdrawal), independent
    of any trade — see api_admin.py's /accounts/{id}/adjust_balance."""
    account.cash += delta
    return account.cash


def unrealized_pnl(account: Account, index_prices: dict[str, float]) -> float:
    total = 0.0
    for product, pos in account.positions.items():
        if pos.qty == 0:
            continue
        index = index_prices.get(product)
        if index is None:
            continue
        total += pos.qty * (index - pos.avg_cost)
    return total


def equity(account: Account, index_prices: dict[str, float]) -> float:
    return account.cash + unrealized_pnl(account, index_prices)


def would_breach_max_position(pos: Position, side: Side, qty: int, max_position: int) -> bool:
    prospective = pos.qty + side.sign * qty
    return abs(prospective) > max_position


def max_position_for(cash: float, leverage: float, mark_price: float | None, tick_size: float | None = None) -> int:
    """Balance-relative position cap, replacing the old fixed-contract-count
    MAX_POSITION: an account can hold up to `leverage` times its own cash in
    notional (cash and mark_price both real dollars — mark_price is the
    instrument's current contract-scaled index price, already the "price of
    one contract", not the raw underlying price). Floors at 1 contract (a
    student with a nonnegative balance can always place a starter order)
    unless cash itself is negative, and at 0 when there's genuinely no price
    to size against yet (mark_price is None, e.g. before the first tick).

    mark_price == 0 is not the same situation as mark_price is None: a
    calendar spread (near - far) is a legitimate instrument that's simply
    *worth* exactly zero right now (both legs tracking spot 1:1 — see
    FuturesChainManager's own docstring) — that's real, current pricing
    data, not a missing one. Treating it the same as "no price yet" made
    every calendar spread permanently untradable (by anyone, including the
    exchange's own MM bots) for as long as it sat at parity, which per that
    same docstring is most of the time — not a risk control, just a dead
    market. `tick_size` (the smallest price move the instrument can
    actually make) stands in for the sizing price in that case instead,
    so the cap reflects "this could move by at least a tick and that has
    real dollar consequences" rather than "this is worth nothing, so you
    may hold none of it"."""
    if mark_price is None:
        return 0
    sizing_price = mark_price if mark_price != 0 else tick_size
    if not sizing_price:
        return 0
    notional_capacity = max(cash, 0.0) * leverage
    return max(1, int(notional_capacity / abs(sizing_price)))
