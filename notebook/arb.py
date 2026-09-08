"""
Index arbitrage bot.

Each product publishes a live `index_price` (fair value). Whenever the
order book has a resting ask priced below fair value, or a resting bid
priced above fair value, by more than MIN_EDGE_TICKS ticks, that's free
edge - take it with a marketable limit order sized to the level.

Walks up to MAX_LEVELS book levels per side per product per tick, capped by
remaining headroom to each product's max_position, and skips levels where
we already have a resting order queued so repeated ticks don't stack
duplicate orders. Stale resting orders (fair value has since moved back
under them) get cancelled to free up position headroom.

Optimized for speed like mm2.py:
- one keep-alive requests.Session with a wide connection pool
- every read this tick (products, account, open orders, and one book per
  product) fires concurrently on a thread pool, so a tick costs ~1 round
  trip instead of N sequential ones
- every cancel and every take fires concurrently too
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "janestreet"
PASSWORD: str = "cuquants"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=16, pool_maxsize=16)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="arb-io")

##
## Config
##
class Config:
    TICK_LEVEL: float = 0.10
    MIN_EDGE_TICKS: int = 2      # must clear fair value by >= this many ticks to take
    MAX_LEVELS: int = 5          # book levels to walk per side, per product, per tick
    POLL_INTERVAL: float = 0.0   # extra sleep per tick; 0 = as fast as the API allows
    MAX_RETRIES: int = 8         # retries on 429 before giving up on a call
    RETRY_BACKOFF: float = 0.25  # seconds, doubled each retry (unless Retry-After is given)

##
## API Call Functions
##
def _request(method, url, **kwargs):
    """requests.Session call with 429 back-off - a shared session firing
    several calls per tick will trip the server's rate limit occasionally;
    that's expected, not fatal, so retry with backoff instead of crashing."""
    delay = Config.RETRY_BACKOFF
    r = SESSION.request(method, url, **kwargs)
    attempt = 0
    while r.status_code == 429 and attempt < Config.MAX_RETRIES:
        wait = float(r.headers.get("Retry-After", delay))
        time.sleep(wait)
        delay *= 2
        attempt += 1
        r = SESSION.request(method, url, **kwargs)
    return r

def login(account_id, password):
    # /login 401s on an account_id that's never been registered - unlike
    # mm.py/spread-trader.py, this had no fallback, so the bot couldn't
    # even start under a fresh ACCOUNT_ID without a separate manual
    # /register call first.
    r = _request("POST", f"{BASE_URL}/register", json={"account_id": account_id, "password": password})
    if r.status_code == 409:
        r = _request("POST", f"{BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_products():
    r = _request("GET", f"{BASE_URL}/products")
    r.raise_for_status()
    return r.json()

def get_book(product, headers):
    r = _request("GET", f"{BASE_URL}/book/{product}", headers=headers)
    r.raise_for_status()
    return r.json()

def get_account(headers):
    r = _request("GET", f"{BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def list_orders(headers):
    r = _request("GET", f"{BASE_URL}/orders", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, order_type, qty, headers, price=None):
    body = {"product": product, "side": side, "type": order_type, "qty": qty}
    if price is not None:
        body["price"] = price
    r = _request("POST", f"{BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        print("rejected:", r.json().get("detail"))
        return None
    return r.json()

def cancel_order(order_id, headers):
    r = _request("DELETE", f"{BASE_URL}/orders/{order_id}", headers=headers)
    if not r.ok:
        print("cancel rejected:", r.json().get("detail"))
        return None
    return r.json()

##
## Arb Logic
##
def plan_takes(symbol, index_price, book, position_qty, max_position, open_orders):
    """
    Mispriced book levels for `symbol` worth taking, as (symbol, side, qty, price).
    Skips levels we already have a resting order on, and never plans more
    size than remaining position headroom allows.
    """
    edge = Config.MIN_EDGE_TICKS * Config.TICK_LEVEL
    our_open_prices = {
        (o['side'], round(o['price'], 1))
        for o in open_orders
        if o['product'] == symbol
    }

    plans = []

    # Asks below fair value - edge: buy them.
    buy_headroom = max_position - position_qty
    for level in book['asks'][:Config.MAX_LEVELS]:
        if buy_headroom <= 0:
            break
        price = round(level['price'], 1)
        if price > index_price - edge:
            break  # asks sorted ascending - nothing further is mispriced
        if ('buy', price) in our_open_prices:
            continue
        qty = min(level['qty'], buy_headroom)
        if qty <= 0:
            continue
        plans.append((symbol, 'buy', qty, price))
        buy_headroom -= qty

    # Bids above fair value + edge: sell into them.
    sell_headroom = position_qty + max_position
    for level in book['bids'][:Config.MAX_LEVELS]:
        if sell_headroom <= 0:
            break
        price = round(level['price'], 1)
        if price < index_price + edge:
            break  # bids sorted descending - nothing further is mispriced
        if ('sell', price) in our_open_prices:
            continue
        qty = min(level['qty'], sell_headroom)
        if qty <= 0:
            continue
        plans.append((symbol, 'sell', qty, price))
        sell_headroom -= qty

    return plans

def stale_cancels(symbol, index_price, open_orders):
    """Our resting orders for `symbol` that fair value has moved back under - free them up."""
    edge = Config.MIN_EDGE_TICKS * Config.TICK_LEVEL
    to_cancel = []
    for o in open_orders:
        if o['product'] != symbol:
            continue
        if o['side'] == 'buy' and o['price'] > index_price - edge:
            to_cancel.append(o['id'])
        elif o['side'] == 'sell' and o['price'] < index_price + edge:
            to_cancel.append(o['id'])
    return to_cancel

##
## Main Event Loop
##
def main():
    """
    Each tick:
    1. Fetch products/index prices, account positions, open orders, and
       every product's book - all concurrently.
    2. Cancel any resting order that's no longer mispriced.
    3. Take every remaining mispriced level, bounded by position headroom.
    """
    api_key = login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    symbols = [p['symbol'] for p in get_products()]

    while True:
        products_f = POOL.submit(get_products)
        account_f = POOL.submit(get_account, headers)
        orders_f = POOL.submit(list_orders, headers)
        book_futures = {s: POOL.submit(get_book, s, headers) for s in symbols}

        products = {p['symbol']: p for p in products_f.result()}
        account = account_f.result()
        open_orders = [o for o in orders_f.result() if o['status'] == 'open']
        books = {s: f.result() for s, f in book_futures.items()}

        cancel_ids = []
        take_plans = []
        for symbol in symbols:
            product = products[symbol]
            index_price = round(product['index_price'], 1)
            position_qty = account['positions'].get(symbol, {}).get('qty', 0)

            cancel_ids += stale_cancels(symbol, index_price, open_orders)
            take_plans += plan_takes(
                symbol, index_price, books[symbol],
                position_qty, product['max_position'], open_orders,
            )

        if cancel_ids:
            for f in [POOL.submit(cancel_order, oid, headers) for oid in cancel_ids]:
                f.result()

        if take_plans:
            futures = [
                POOL.submit(submit_order, sym, side, "limit", qty, headers, price)
                for sym, side, qty, price in take_plans
            ]
            for (sym, side, qty, price), f in zip(take_plans, futures):
                if f.result() is not None:
                    print(f"TOOK {side.upper()} {qty}x {sym} @ {price}")

        if Config.POLL_INTERVAL:
            time.sleep(Config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
