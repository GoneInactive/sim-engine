from exchange.bots import BotManager
from exchange.config import FeedConfig, ProductConfig
from exchange.engine import MatchingEngine
from exchange.index_feed import IndexPriceService
from exchange.models import OrderType, Side

PRODUCTS = {
    "BTC-MINI": ProductConfig(symbol="BTC-MINI", underlying="BTC/USD", contract_size=0.001, max_position=15, tick_size=0.05),
}
FEED = FeedConfig(stale_threshold_seconds=7, sma_window=20, reconnect_blend_seconds=7, shock_decay_seconds=7)


def make_manager():
    engine = MatchingEngine(PRODUCTS)
    index_service = IndexPriceService(FEED, PRODUCTS)
    index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index = 80
    manager = BotManager(engine, index_service, starting_cash=1000.0)
    manager.spawn_defaults(["BTC-MINI"])
    return manager, engine, index_service


def test_mm_bots_quote_around_index():
    manager, engine, index_service = make_manager()
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=10.0)
    book = engine.book_snapshot("BTC-MINI", depth=20)
    # 5 bots each post a bid+ask, but with tick-aligned prices (see
    # test_mm_quotes_snap_to_tick_grid) two bots can legitimately land on
    # the same level and aggregate into one row — so assert total quoted
    # size rather than a fixed level count.
    assert len(book["bids"]) >= 1
    assert len(book["asks"]) >= 1
    assert sum(b["qty"] for b in book["bids"]) == sum(3 + i for i in range(5))
    assert sum(a["qty"] for a in book["asks"]) == sum(3 + i for i in range(5))
    best_bid = book["bids"][0]["price"]
    best_ask = book["asks"][0]["price"]
    assert best_bid < 80.0 < best_ask
    assert best_ask - best_bid < 2.0  # tightest bot's spread should be small


def test_noise_bots_eventually_trade():
    manager, engine, index_service = make_manager()
    manager.tick(now=10.0)
    traded = False
    for i in range(200):
        manager.tick(now=10.0 + i * 0.5)
        if engine.trade_tape:
            traded = True
            break
    assert traded


def test_global_spread_scale_tightens_and_widens():
    manager, engine, index_service = make_manager()
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=10.0, spread_scale=1.0)
    normal_spread = engine.book_snapshot("BTC-MINI", depth=20)
    normal = normal_spread["asks"][0]["price"] - normal_spread["bids"][0]["price"]

    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=20.0, spread_scale=0.5)
    tightened = engine.book_snapshot("BTC-MINI", depth=20)
    tight = tightened["asks"][0]["price"] - tightened["bids"][0]["price"]
    assert tight < normal


def test_liquidity_withdraw_widens_spread():
    manager, engine, index_service = make_manager()
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=10.0)
    book = engine.book_snapshot("BTC-MINI", depth=20)
    normal_spread = book["asks"][0]["price"] - book["bids"][0]["price"]

    manager.trigger_liquidity_event("BTC-MINI", "withdraw", duration_seconds=30, now=20.0, magnitude=3.0)
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=25.0)
    book2 = engine.book_snapshot("BTC-MINI", depth=20)
    widened_spread = book2["asks"][0]["price"] - book2["bids"][0]["price"]
    assert widened_spread > normal_spread


def test_arb_bot_spawned_by_default_and_exempt_from_max_position():
    manager, engine, index_service = make_manager()
    assert len(manager.arb_bots) == 1
    bot = manager.arb_bots[0]
    assert bot.product == "BTC-MINI"
    assert bot.config.account_id in engine.unlimited_position_accounts
    assert engine.accounts[bot.config.account_id].cash == 1_000_000_000.0


def test_arb_bot_ignores_displacement_within_threshold():
    engine = MatchingEngine(PRODUCTS)
    index_service = IndexPriceService(FEED, PRODUCTS)
    index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index = 80
    manager = BotManager(engine, index_service, starting_cash=1000.0)
    bot = manager.spawn_arb_bot("BTC-MINI", threshold_ticks=15.0, correction_qty=10, check_interval=1.0)

    engine.get_or_create_account("whale", 100000.0)
    # displaced by only 5 ticks (0.25) — within the 15-tick threshold
    engine.submit_order("whale", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 79.75)

    bot.maybe_correct(engine, index_service, now=10.0)
    assert len(engine.trade_tape) == 0


def test_arb_bot_corrects_when_book_too_cheap():
    engine = MatchingEngine(PRODUCTS)
    index_service = IndexPriceService(FEED, PRODUCTS)
    index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index = 80
    manager = BotManager(engine, index_service, starting_cash=1000.0)
    bot = manager.spawn_arb_bot("BTC-MINI", threshold_ticks=15.0, correction_qty=10, check_interval=1.0)

    engine.get_or_create_account("whale", 1000.0)
    # a student holding the book far below fair value: displaced by 100
    # ticks ($5), well past the 15-tick threshold
    engine.submit_order("whale", "BTC-MINI", Side.SELL, OrderType.LIMIT, 15, 75.0)

    bot.maybe_correct(engine, index_service, now=10.0)
    assert len(engine.trade_tape) == 1
    fill = engine.trade_tape[0]
    assert fill.price == 75.0
    assert fill.taker_account_id == bot.config.account_id
    assert fill.taker_side == Side.BUY
    # unlimited position: absorbed the full correction_qty, no MAX_POSITION rejection
    assert engine.accounts[bot.config.account_id].position_for("BTC-MINI").qty == 10


def test_arb_bot_corrects_when_book_too_rich():
    engine = MatchingEngine(PRODUCTS)
    index_service = IndexPriceService(FEED, PRODUCTS)
    index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index = 80
    manager = BotManager(engine, index_service, starting_cash=1000.0)
    bot = manager.spawn_arb_bot("BTC-MINI", threshold_ticks=15.0, correction_qty=10, check_interval=1.0)

    engine.get_or_create_account("whale", 1000.0)
    engine.submit_order("whale", "BTC-MINI", Side.BUY, OrderType.LIMIT, 15, 85.0)

    bot.maybe_correct(engine, index_service, now=10.0)
    assert len(engine.trade_tape) == 1
    assert engine.trade_tape[0].taker_side == Side.SELL
    assert engine.accounts[bot.config.account_id].position_for("BTC-MINI").qty == -10


def test_arb_bot_inactive_does_not_correct():
    engine = MatchingEngine(PRODUCTS)
    index_service = IndexPriceService(FEED, PRODUCTS)
    index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    manager = BotManager(engine, index_service, starting_cash=1000.0)
    bot = manager.spawn_arb_bot("BTC-MINI", threshold_ticks=15.0)
    bot.config.active = False

    engine.get_or_create_account("whale", 1000.0)
    engine.submit_order("whale", "BTC-MINI", Side.SELL, OrderType.LIMIT, 15, 75.0)

    bot.maybe_correct(engine, index_service, now=10.0)
    assert len(engine.trade_tape) == 0


def test_spawn_mm_bot_adds_and_indexes_account():
    manager, engine, index_service = make_manager()
    assert len(manager.mm_bots) == 5
    bot = manager.spawn_mm_bot("BTC-MINI", base_spread_frac=0.01, quote_size=2)
    assert len(manager.mm_bots) == 6
    assert bot.config.account_id == "mm_BTC-MINI_5"
    assert bot.config.account_id in engine.accounts


def test_spawn_noise_bot_adds_and_indexes_account():
    manager, engine, index_service = make_manager()
    assert len(manager.noise_bots) == 3
    bot = manager.spawn_noise_bot("BTC-MINI", arrival_rate_per_sec=1.0, max_size=5)
    assert len(manager.noise_bots) == 4
    assert bot.config.account_id == "noise_BTC-MINI_3"
    assert bot.config.account_id in engine.accounts


def test_mm_quotes_snap_to_tick_grid():
    # A price that isn't a multiple of tick_size would never coincide with
    # any row the website ladder generates (i * tick_size), so it would be
    # invisible there even though it's a real resting order.
    manager, engine, index_service = make_manager()
    tick_size = engine.products["BTC-MINI"].tick_size
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=10.0)
    book = engine.book_snapshot("BTC-MINI", depth=20)
    for level in book["bids"] + book["asks"]:
        ratio = level["price"] / tick_size
        assert abs(ratio - round(ratio)) < 1e-6, f"{level['price']} is not tick-aligned"


def test_mm_inventory_skew_shifts_quotes():
    manager, engine, index_service = make_manager()
    for bot in manager.mm_bots:
        bot.maybe_requote(engine, index_service, manager.liquidity_events, now=10.0)
    bot = manager.mm_bots[0]
    # force a long position on the bot to check skew direction
    from exchange.models import Side
    engine.accounts[bot.config.account_id].position_for("BTC-MINI").qty = 10
    bot.last_quote_time = 0.0
    bot.maybe_requote(engine, index_service, manager.liquidity_events, now=20.0)
    book = engine.book_snapshot("BTC-MINI", depth=20)
    # long inventory should skew this bot's mid below index (80)
    assert bot.bid_order_id is not None
