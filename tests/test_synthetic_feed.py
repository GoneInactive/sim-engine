import random

from exchange.config import (
    AccountsConfig,
    Config,
    FeedConfig,
    FeesConfig,
    NetworkConfig,
    ProductConfig,
    RateLimitConfig,
    ServiceNetwork,
    SyntheticFeedConfig,
)
from exchange.index_feed import IndexPriceService
from exchange.synthetic_feed import RandomEventScheduler, SyntheticFeedClient


def make_config(random_events_enabled=True, mean_interval=240.0):
    network = NetworkConfig(
        api=ServiceNetwork("127.0.0.1", 8000),
        admin_api=ServiceNetwork("127.0.0.1", 8001),
        website=ServiceNetwork("127.0.0.1", 8080),
        api_base_url="http://127.0.0.1:8000",
        admin_api_base_url="http://127.0.0.1:8001",
        website_base_url="http://127.0.0.1:8080",
    )
    products = {
        "BTC-MINI": ProductConfig(
            symbol="BTC-MINI", underlying="BTC/USD", contract_size=0.001, max_position=15,
            tick_size=0.05, starting_price=80000.0, annual_volatility=0.5, annual_drift=0.0,
        ),
        "ETH-MINI": ProductConfig(
            symbol="ETH-MINI", underlying="ETH/USD", contract_size=0.03, max_position=15,
            tick_size=0.05, starting_price=2500.0, annual_volatility=0.7, annual_drift=0.0,
        ),
    }
    return Config(
        network=network,
        database_url="sqlite://",
        products=products,
        accounts=AccountsConfig(starting_cash=1000.0, enforce_buying_power=False, freeze_on_zero_equity=False),
        feed=FeedConfig(stale_threshold_seconds=7, sma_window=20, reconnect_blend_seconds=7, shock_decay_seconds=7),
        rate_limit=RateLimitConfig(requests_per_second=20, burst=40, ws_connections_per_key=1),
        synthetic_feed=SyntheticFeedConfig(
            tick_interval_seconds=1.0,
            random_events_enabled=random_events_enabled,
            random_event_mean_interval_seconds=mean_interval,
        ),
        fees=FeesConfig(maker_bps=-1.0, taker_bps=2.0),
        admin_password="admin-pw",
        website_password="site-pw",
    )


def test_feed_client_ticks_index_service():
    random.seed(1)
    config = make_config()
    svc = IndexPriceService(config.feed, config.products)
    client = SyntheticFeedClient(config, svc)

    now = 0.0
    for symbol, cfg in config.products.items():
        price = client._step(cfg.starting_price, cfg.annual_drift, cfg.annual_volatility, 1.0)
        svc.on_raw_tick(symbol, price, now)

    index = svc.get_index_price("BTC-MINI", now)
    assert index is not None
    # one 1-second GBM step shouldn't move price by an order of magnitude
    assert 70.0 < index < 90.0


def test_gbm_step_is_positive_and_bounded_for_reasonable_vol():
    random.seed(42)
    config = make_config()
    client = SyntheticFeedClient(config, IndexPriceService(config.feed, config.products))
    price = 100.0
    for _ in range(1000):
        price = client._step(price, 0.0, 0.5, 1.0)
        assert price > 0


def test_feed_client_reads_live_params_dict():
    config = make_config()
    svc = IndexPriceService(config.feed, config.products)
    params = {
        symbol: {"annual_drift": cfg.annual_drift, "annual_volatility": cfg.annual_volatility}
        for symbol, cfg in config.products.items()
    }
    client = SyntheticFeedClient(config, svc, params)
    assert client.params is params  # same object, not a copy

    params["BTC-MINI"]["annual_volatility"] = 5.0
    assert client.params["BTC-MINI"]["annual_volatility"] == 5.0


def test_random_event_scheduler_disabled_does_nothing(monkeypatch):
    import asyncio

    config = make_config(random_events_enabled=False)
    svc = IndexPriceService(config.feed, config.products)

    class DummyBotManager:
        def trigger_liquidity_event(self, *a, **k):
            raise AssertionError("should never fire when disabled")

    scheduler = RandomEventScheduler(svc, DummyBotManager(), config)
    asyncio.run(scheduler.run())  # returns immediately since disabled


def test_random_event_scheduler_fires_a_recognized_kind():
    config = make_config()
    svc = IndexPriceService(config.feed, config.products)
    svc.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    svc.on_raw_tick("ETH-MINI", 2500.0, now=0.0)

    calls = []

    class DummyBotManager:
        def trigger_liquidity_event(self, *a, **k):
            calls.append(("liquidity", a, k))

    scheduler = RandomEventScheduler(svc, DummyBotManager(), config)
    random.seed(7)
    for _ in range(20):
        scheduler._fire_one()

    # at least one event of some kind should have been registered as an
    # index_service offset event or a liquidity call
    total_offset_events = sum(len(v) for v in svc.events.values())
    assert total_offset_events > 0 or calls
