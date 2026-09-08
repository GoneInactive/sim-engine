"""
Optimized market-making bot.

Behavior is identical to mm.py, but the hot path is tightened up:

1. HTTP keep-alive: every request goes through one shared `requests.Session`
   with a bigger connection pool, instead of opening a fresh TCP/TLS
   connection per call.
2. Parallel legs: cancel-bid/cancel-ask, place-bid/place-ask, and
   fetch-products/fetch-book are each independent round trips, so they're
   fired concurrently on a small thread pool instead of sequentially -
   each batch takes ~1 round trip instead of ~2.
3. Quotes/sizes are computed *before* cancelling resting orders, so the
   cancel -> place window contains nothing but those two HTTP calls.
4. Fill-id dedup uses a set (O(1)) instead of a list (O(n)).
5. Rejected orders (submit_order returning None) no longer crash the bot
   on the next comparison.
"""
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "knight-capital"
PASSWORD: str = "cuquants"

##
## Shared HTTP session (keep-alive) + thread pool for concurrent legs
##
SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mm-io")

##
## Config
##
class Config:
    TRADING_PRODUCT: str = "BTC-MINI"
    DEFAULT_SIZE: int = 4
    MIN_SPREAD: int = 3
    TICK_LEVEL: float = 0.10

##
## API Call Functions
##
def login(account_id, password):
    r = SESSION.post(f"{BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_products():
    r = SESSION.get(f"{BASE_URL}/products")
    r.raise_for_status()
    return r.json()

def get_book(product, headers):
    r = SESSION.get(f"{BASE_URL}/book/{product}", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, order_type, qty, headers, price=None):
    body = {"product": product, "side": side, "type": order_type, "qty": qty}
    if price is not None:
        body["price"] = price
    r = SESSION.post(f"{BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        print("rejected:", r.json().get("detail"))
        return None
    return r.json()

def cancel_order(order_id, headers):
    if order_id is None:
        return None
    r = SESSION.delete(f"{BASE_URL}/orders/{order_id}", headers=headers)
    if not r.ok:
        print("cancel rejected:", r.json().get("detail"))
        return None
    return r.json()

def list_fills(headers):
    r = SESSION.get(f"{BASE_URL}/fills", headers=headers)
    r.raise_for_status()
    return r.json()

##
## State
##
@dataclass
class State:
    our_bid: dict[str, float | int | str] | None = None
    our_ask: dict[str, float | int | str] | None = None
    fills: set[str] = field(default_factory=set)

##
## Helpers
##
def batch_cancel(trading_state, headers):
    bid_id = trading_state.our_bid['id'] if trading_state.our_bid else None
    ask_id = trading_state.our_ask['id'] if trading_state.our_ask else None
    futures = [POOL.submit(cancel_order, oid, headers) for oid in (bid_id, ask_id) if oid is not None]
    for f in futures:
        f.result()

def batch_place(trading_state, headers, theo_qty=None, theo_prices=None):
    if theo_qty is None:
        theo_qty = get_sizes()
    if theo_prices is None:
        theo_prices = get_quotes(trading_state, headers)

    print(f"PLACING: {theo_qty}, {theo_prices}")

    bid_future = POOL.submit(submit_order, Config.TRADING_PRODUCT, "buy", "limit", theo_qty[0], headers, theo_prices[0])
    ask_future = POOL.submit(submit_order, Config.TRADING_PRODUCT, "sell", "limit", theo_qty[1], headers, theo_prices[1])

    trading_state.our_bid = bid_future.result()
    trading_state.our_ask = ask_future.result()

    return trading_state

def refresh_quotes(trading_state, headers, reason):
    print(f"REASON: {reason}")
    theo_qty = get_sizes()
    theo_prices = get_quotes(trading_state, headers)
    batch_cancel(trading_state, headers)
    return batch_place(trading_state, headers, theo_qty, theo_prices)

##
## Trading Calculations
##
def get_sizes() -> tuple[int, int]:
    return (Config.DEFAULT_SIZE, Config.DEFAULT_SIZE)

def get_quotes(trading_state, headers) -> tuple[float, float]:
    """
    Get the index price and the orderbook
    What's the highest we can bid such that our half-spread >= MIN_SPREAD/2 around the index price
    Similiar for ask

    10.00
    9.00,15.00
    -> 8.00, 14.00
    """
    products_future = POOL.submit(get_products)
    book_future = POOL.submit(get_book, Config.TRADING_PRODUCT, headers)

    index_price: float = 0.0
    for product in products_future.result():
        if product['symbol'] == Config.TRADING_PRODUCT:
            index_price = round(product['index_price'], 1)

    book = book_future.result()
    bids, asks = book['bids'], book['asks']

    bid_bound = math.floor((index_price - 0.5 * Config.MIN_SPREAD * Config.TICK_LEVEL) * 10) / 10
    ask_bound = math.ceil((index_price + 0.5 * Config.MIN_SPREAD * Config.TICK_LEVEL) * 10) / 10

    theo_bid = bid_bound
    theo_ask = ask_bound

    our_bid_price = trading_state.our_bid['price'] if trading_state.our_bid else None
    our_ask_price = trading_state.our_ask['price'] if trading_state.our_ask else None

    for bid in bids:
        bid_price = round(bid['price'], 1)
        if bid_price <= bid_bound and our_bid_price is not None and bid_price != our_bid_price:
            theo_bid = bid_price + Config.TICK_LEVEL
            break

    for ask in asks:
        ask_price = round(ask['price'], 1)
        if ask_price >= ask_bound and our_ask_price is not None and ask_price != our_ask_price:
            theo_ask = ask_price - Config.TICK_LEVEL
            break

    return (round(theo_bid, 1), round(theo_ask, 1))

##
## Main Event Loop
##
def main():
    """
    1. Get recent fills
    2. Compute theo quotes, we compare to our current orders.
    3. IF different, cancel and re-place
    4. ELSE do nothing
    """
    API_KEY = login(ACCOUNT_ID, PASSWORD)
    HEADERS = {"X-API-Key": API_KEY}

    trading_state = State()
    ##
    ## Place init orders
    ##
    batch_place(trading_state, HEADERS)

    while True:
        ##
        ## Get Recent Fills and Update State
        ##
        theo_quotes = get_quotes(trading_state, headers=HEADERS)

        fills = list_fills(HEADERS)[:2]
        for fill in fills:
            if fill['id'] not in trading_state.fills:
                trading_state.fills.add(fill['id'])
                trading_state = refresh_quotes(trading_state, HEADERS, "MISSING FILLS")

        bid_price = trading_state.our_bid['price'] if trading_state.our_bid else None
        ask_price = trading_state.our_ask['price'] if trading_state.our_ask else None
        if bid_price != theo_quotes[0] or ask_price != theo_quotes[1]:
            trading_state = refresh_quotes(trading_state, HEADERS, "SUBOPTIMAL PRICING")


if __name__ == "__main__":
    main()
