from exchange.ledger import apply_fill, equity, unrealized_pnl, would_breach_max_position
from exchange.models import Account, Position, Side


def test_opening_position_no_cash_impact():
    acc = Account(id="a1", cash=1000.0)
    realized = apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    assert realized == 0.0
    assert acc.cash == 1000.0
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == 5
    assert pos.avg_cost == 75.0


def test_adding_to_position_weighted_avg_cost():
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 85.0)
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == 10
    assert pos.avg_cost == 80.0
    assert acc.cash == 1000.0


def test_partial_reduce_realizes_pnl_leaves_avg_cost_unchanged():
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    realized = apply_fill(acc, "BTC-MINI", Side.SELL, 3, 80.0)
    assert realized == 3 * (80.0 - 75.0)
    assert acc.cash == 1000.0 + 15.0
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == 2
    assert pos.avg_cost == 75.0


def test_exact_close_resets_avg_cost():
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    apply_fill(acc, "BTC-MINI", Side.SELL, 5, 80.0)
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == 0
    assert pos.avg_cost == 0.0
    assert acc.cash == 1025.0


def test_flip_through_zero_spec_example():
    # build-spec.md §3 worked example: long +5 @ 75, sell 8 @ 80
    # -> close 5 @ 80 (realize $25), open new short -3 @ 80.
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    realized = apply_fill(acc, "BTC-MINI", Side.SELL, 8, 80.0)
    assert realized == 25.0
    assert acc.cash == 1025.0
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == -3
    assert pos.avg_cost == 80.0


def test_flip_through_zero_short_to_long():
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.SELL, 4, 100.0)  # short -4 @ 100
    realized = apply_fill(acc, "BTC-MINI", Side.BUY, 10, 90.0)
    # close 4 @ 90 (realize 4*(100-90)=40), open +6 @ 90
    assert realized == 40.0
    assert acc.cash == 1040.0
    pos = acc.position_for("BTC-MINI")
    assert pos.qty == 6
    assert pos.avg_cost == 90.0


def test_unrealized_pnl_and_equity():
    acc = Account(id="a1", cash=1000.0)
    apply_fill(acc, "BTC-MINI", Side.BUY, 5, 75.0)
    upnl = unrealized_pnl(acc, {"BTC-MINI": 80.0})
    assert upnl == 25.0
    assert equity(acc, {"BTC-MINI": 80.0}) == 1025.0


def test_would_breach_max_position():
    pos = Position(qty=13)
    assert would_breach_max_position(pos, Side.BUY, 3, max_position=15) is True
    assert would_breach_max_position(pos, Side.BUY, 2, max_position=15) is False
    assert would_breach_max_position(pos, Side.SELL, 30, max_position=15) is True
