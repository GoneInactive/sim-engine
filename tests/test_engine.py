import pytest

from exchange.config import ProductConfig
from exchange.engine import MatchingEngine, OrderRejected
from exchange.models import OrderStatus, OrderType, Side

PRODUCTS = {
    "BTC-MINI": ProductConfig(
        symbol="BTC-MINI", underlying="BTC/USD", contract_size=0.001, max_position=15, tick_size=0.05
    )
}


def make_engine():
    engine = MatchingEngine(PRODUCTS)
    engine.get_or_create_account("alice", 1000.0)
    engine.get_or_create_account("bob", 1000.0)
    return engine


def test_resting_limit_order_no_cross():
    engine = make_engine()
    order = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 70.0)
    assert order.status == OrderStatus.OPEN
    book = engine.book_snapshot("BTC-MINI")
    assert book["bids"] == [{"price": 70.0, "qty": 5}]
    assert book["asks"] == []


def test_crossing_limit_orders_fill_at_maker_price():
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)
    taker = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 85.0)

    assert taker.status == OrderStatus.FILLED
    assert len(engine.trade_tape) == 1
    fill = engine.trade_tape[0]
    assert fill.price == 80.0  # trades at maker (resting) price, not taker's limit
    assert fill.qty == 5

    alice_pos = engine.accounts["alice"].position_for("BTC-MINI")
    bob_pos = engine.accounts["bob"].position_for("BTC-MINI")
    assert alice_pos.qty == -5
    assert bob_pos.qty == 5


def test_partial_fill_rests_remainder():
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 3, 80.0)
    taker = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 80.0)

    assert taker.status == OrderStatus.PARTIALLY_FILLED
    assert taker.remaining_qty == 2
    book = engine.book_snapshot("BTC-MINI")
    assert book["bids"] == [{"price": 80.0, "qty": 2}]


def test_price_time_priority():
    engine = make_engine()
    engine.get_or_create_account("carol", 1000.0)
    first = engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)
    second = engine.submit_order("carol", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)
    taker = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 80.0)

    assert taker.status == OrderStatus.FILLED
    assert engine.trade_tape[0].maker_order_id == first.id
    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.OPEN


def test_market_order_is_ioc_and_never_rests():
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 3, 80.0)
    taker = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.MARKET, 10, None)

    assert taker.remaining_qty == 7
    assert taker.status == OrderStatus.PARTIALLY_FILLED
    book = engine.book_snapshot("BTC-MINI")
    assert book["bids"] == []  # unfilled market remainder never rests


def test_max_position_rejects_at_acceptance():
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 15, 70.0)
    with pytest.raises(OrderRejected):
        engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 1, 70.0)


def test_unlimited_position_account_bypasses_max_position():
    engine = make_engine()
    engine.unlimited_position_accounts.add("alice")
    engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 15, 70.0)
    # would normally be rejected — MAX_POSITION for BTC-MINI is 15
    order = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 50, 70.0)
    assert order.status == OrderStatus.OPEN


def test_maker_taker_fees_applied_on_fill():
    engine = MatchingEngine(PRODUCTS, maker_fee_bps=-1.0, taker_fee_bps=2.0)
    engine.get_or_create_account("alice", 1000.0)
    engine.get_or_create_account("bob", 1000.0)

    engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)  # maker
    engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 80.0)  # taker

    notional = 80.0 * 5
    expected_maker_rebate = notional * 1.0 / 10_000  # negative bps -> alice gains
    expected_taker_fee = notional * 2.0 / 10_000  # bob pays

    assert abs(engine.accounts["alice"].cash - (1000.0 + expected_maker_rebate)) < 1e-9
    assert abs(engine.accounts["bob"].cash - (1000.0 - expected_taker_fee)) < 1e-9


def test_fill_records_taker_side_for_correct_maker_side_inference():
    # A taker SELL (hitting a resting bid) means the maker was buying —
    # this used to be inferred backwards ("buy iff taker").
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 80.0)  # maker, buying
    engine.submit_order("bob", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)  # taker, selling
    fill = engine.trade_tape[0]
    assert fill.taker_side == Side.SELL


def test_limit_order_off_tick_price_rejected():
    engine = make_engine()
    with pytest.raises(OrderRejected):
        engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 1, 70.03)


def test_limit_order_non_positive_price_rejected():
    # A negative (or zero) price is tick-aligned just fine — nothing else
    # would catch it. Left unchecked, it becomes the book's own best
    # bid/ask, and the website's ladder centers itself on the book's mid
    # with no sanity bound, so it would center on garbage forever after.
    engine = make_engine()
    with pytest.raises(OrderRejected):
        engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 1, -70.0)
    with pytest.raises(OrderRejected):
        engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 1, 0.0)


def test_volume_tracked_incrementally_per_fill():
    engine = make_engine()
    engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)
    engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 3, 80.0)
    assert engine.volume_qty["BTC-MINI"] == 3
    assert engine.volume_notional["BTC-MINI"] == 240.0

    engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 2, 80.0)
    assert engine.volume_qty["BTC-MINI"] == 5
    assert engine.volume_notional["BTC-MINI"] == 400.0


def test_book_snapshot_aggregates_same_price_into_one_level():
    engine = make_engine()
    engine.get_or_create_account("carol", 1000.0)
    engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 3, 70.0)
    engine.submit_order("carol", "BTC-MINI", Side.BUY, OrderType.LIMIT, 4, 70.0)
    book = engine.book_snapshot("BTC-MINI")
    assert book["bids"] == [{"price": 70.0, "qty": 7}]


def test_cancel_order_removes_from_book():
    engine = make_engine()
    order = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 70.0)
    engine.cancel_order(order.id, "alice")
    assert order.status == OrderStatus.CANCELLED
    assert engine.book_snapshot("BTC-MINI")["bids"] == []


def test_kill_account_orders():
    engine = make_engine()
    o1 = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 70.0)
    o2 = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 3, 69.0)
    killed = engine.kill_account_orders("alice")
    assert {o.id for o in killed} == {o1.id, o2.id}
    assert engine.book_snapshot("BTC-MINI")["bids"] == []


def test_resting_index_isolated_per_account_and_cleaned_up_on_fill():
    engine = make_engine()
    o1 = engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 70.0)
    o2 = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 3, 69.0)

    alice_resting = engine.resting_orders_by_account_product["alice"]["BTC-MINI"]
    bob_resting = engine.resting_orders_by_account_product["bob"]["BTC-MINI"]
    assert set(alice_resting) == {o1.id}
    assert set(bob_resting) == {o2.id}

    # a taker fill against alice's resting bid must remove it from the
    # resting index (used by the MAX_POSITION hot path) without touching bob's
    engine.get_or_create_account("carol", 1000.0)
    engine.submit_order("carol", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 70.0)
    assert engine.resting_orders_by_account_product.get("alice", {}).get("BTC-MINI", {}) == {}
    assert set(engine.resting_orders_by_account_product["bob"]["BTC-MINI"]) == {o2.id}


def test_orders_and_fills_indexed_per_account():
    engine = make_engine()
    alice_order = engine.submit_order("alice", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 80.0)
    bob_order = engine.submit_order("bob", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 80.0)

    assert set(engine.orders_by_account["alice"]) == {alice_order.id}
    assert set(engine.orders_by_account["bob"]) == {bob_order.id}
    assert len(engine.fills_by_account["alice"]) == 1
    assert len(engine.fills_by_account["bob"]) == 1
    assert "carol" not in engine.fills_by_account


def test_frozen_account_rejected():
    engine = make_engine()
    engine.accounts["alice"].frozen = True
    with pytest.raises(OrderRejected):
        engine.submit_order("alice", "BTC-MINI", Side.BUY, OrderType.LIMIT, 1, 70.0)
