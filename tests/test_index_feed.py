from exchange.config import FeedConfig, ProductConfig
from exchange.index_feed import IndexPriceService

PRODUCTS = {
    "BTC-MINI": ProductConfig(symbol="BTC-MINI", underlying="BTC/USD", contract_size=0.001, max_position=15, tick_size=0.05),
    "ETH-MINI": ProductConfig(symbol="ETH-MINI", underlying="ETH/USD", contract_size=0.03, max_position=15, tick_size=0.05),
}
FEED = FeedConfig(stale_threshold_seconds=5.0, sma_window=3, reconnect_blend_seconds=4.0, shock_decay_seconds=2.0)


def make_service():
    return IndexPriceService(FEED, PRODUCTS)


def test_live_price_scales_by_contract_size():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    assert svc.get_index_price("BTC-MINI", now=0.0) == 80.0


def test_no_price_before_first_tick():
    svc = make_service()
    assert svc.get_index_price("BTC-MINI", now=0.0) is None


def test_staleness_triggers_fallback_sma():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    svc.on_raw_tick("BTC-MINI", 82000.0, now=1.0)
    svc.on_raw_tick("BTC-MINI", 84000.0, now=2.0)
    # no ticks after t=2; at t=8 we're past the 5s stale threshold
    price = svc.get_index_price("BTC-MINI", now=8.0)
    assert svc.is_stale("BTC-MINI")
    expected_sma = (80.0 + 82.0 + 84.0) / 3
    assert price == expected_sma


def test_fallback_holds_flat_while_stale():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    p1 = svc.get_index_price("BTC-MINI", now=8.0)
    p2 = svc.get_index_price("BTC-MINI", now=20.0)
    assert p1 == p2 == 80.0


def test_reconnect_blends_linearly_not_snap():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    svc.get_index_price("BTC-MINI", now=8.0)  # goes stale, fallback = 80.0
    assert svc.is_stale("BTC-MINI")

    svc.on_raw_tick("BTC-MINI", 100000.0, now=8.0)  # reconnects at 100
    assert not svc.is_stale("BTC-MINI")
    # blend duration is 4s: halfway through should be ~midpoint
    mid = svc.get_index_price("BTC-MINI", now=10.0)
    assert 89.0 < mid < 91.0
    end = svc.get_index_price("BTC-MINI", now=12.5)
    assert end == 100.0


def test_price_shock_ramps_holds_and_decays():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    svc.trigger_price_shock("BTC-MINI", target_offset=10.0, now=0.0, ramp_seconds=2.0, hold_seconds=3.0)
    # mid-ramp
    assert abs(svc.get_index_price("BTC-MINI", now=1.0) - 85.0) < 1e-6
    # holding at full offset
    assert abs(svc.get_index_price("BTC-MINI", now=4.0) - 90.0) < 1e-6
    # decaying (hold ends at t=5, decay_seconds=2 -> fully gone by t=7)
    assert abs(svc.get_index_price("BTC-MINI", now=6.0) - 85.0) < 1e-6
    assert abs(svc.get_index_price("BTC-MINI", now=8.0) - 80.0) < 1e-6


def test_bull_market_drift_ramps_over_full_duration():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    svc.trigger_bull_bear("BTC-MINI", drift=20.0, duration_seconds=10.0, now=0.0)
    assert abs(svc.get_index_price("BTC-MINI", now=5.0) - 90.0) < 1e-6
    assert abs(svc.get_index_price("BTC-MINI", now=10.0) - 100.0) < 1e-6


def test_spread_widen_pushes_products_apart():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index 80
    svc.on_raw_tick("ETH-MINI", 2500.0, now=0.0)  # index 75
    svc.trigger_spread_event("widen", magnitude=10.0, now=0.0, ramp_seconds=1.0, hold_seconds=5.0)
    btc = svc.get_index_price("BTC-MINI", now=1.0)
    eth = svc.get_index_price("ETH-MINI", now=1.0)
    assert abs((btc - eth) - 15.0) < 1e-6  # original spread 5, widened by +10


def test_spread_invert_flips_sign():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index 80
    svc.on_raw_tick("ETH-MINI", 2500.0, now=0.0)  # index 75, spread = +5
    svc.trigger_spread_event("invert", magnitude=5.0, now=0.0, ramp_seconds=1.0, hold_seconds=5.0)
    btc = svc.get_index_price("BTC-MINI", now=1.0)
    eth = svc.get_index_price("ETH-MINI", now=1.0)
    assert (btc - eth) < 0


def test_spread_converge_pulls_toward_zero():
    svc = make_service()
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index 80
    svc.on_raw_tick("ETH-MINI", 2500.0, now=0.0)  # index 75, spread = +5
    svc.trigger_spread_event("converge", magnitude=1.0, now=0.0, ramp_seconds=1.0, hold_seconds=5.0)
    btc = svc.get_index_price("BTC-MINI", now=1.0)
    eth = svc.get_index_price("ETH-MINI", now=1.0)
    assert abs(btc - eth) < 1e-6
