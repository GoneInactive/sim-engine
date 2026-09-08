"""
Spread arbitrage bot.

BTC-ETH-MINI has its own order book with its own bots and its own
liquidity - it isn't automatically pegged to its two legs, so its resting
bids/asks can drift away from true fair value: btc_index - eth_index.
Whenever a level on the spread's own book is mispriced relative to that
fair value by more than MIN_EDGE_TICKS, take it - this is arb.py's
edge-taking logic verbatim, just applied to one product whose fair value
has to be computed client-side instead of read off GET /products (the
spread instrument is deliberately not listed there - it's a bolt-on
instrument over the two real products, not a real one itself).

Negative fair value is expected and correct, not a bug: an admin 'invert'
event on the spread is specifically designed to push btc_index below
eth_index, and the spread product accepts negative-priced limit orders for
exactly that reason (see ProductConfig.allow_negative_price in
src/exchange/config.py). Every price/edge comparison below works the same
whether fair_value is positive or negative - no special-casing needed.

MAX_POSITION for the spread isn't discoverable via GET /products either
(same reason), so it's hardcoded below to match config.yaml's
spread.max_position - keep the two in sync if that config ever changes.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "daytek"
PASSWORD: str = "poop"

SPREAD_SYMBOL: str = "BTC-ETH-MINI"
BTC_SYMBOL: str = "BTC-MINI"
ETH_SYMBOL: str = "ETH-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spread-arb-io")

##
## Config
##
class Config:
    TICK_LEVEL: float = 0.10
    MIN_EDGE_TICKS: int = 2      # must clear fair value by >= this many ticks to take
    MAX_LEVELS: int = 5          # book levels to walk per side per tick
    MAX_POSITION: int = 75       # matches config.yaml spread.max_position - see module docstring
    POLL_INTERVAL: float = 0.0   # extra sleep per tick; 0 = as fast as the API allows
    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25  # seconds, doubled each retry unless Retry-After is given

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

def register_or_login(account_id, password):
    r = _request("POST", f"{BASE_URL}/register", json={"account_id": account_id, "password": password})
    if r.status_code == 409:
        r = _request("POST", f"{BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_products():
    r = _request("GET", f"{BASE_URL}/products")
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}

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
## Arb Logic — identical shape to arb.py's plan_takes/stale_cancels,
## parameterized by an externally-supplied fair value instead of reading
## index_price off the product dict.
##
def plan_takes(fair_value, book, position_qty, open_orders):
    edge = Config.MIN_EDGE_TICKS * Config.TICK_LEVEL
    our_open_prices = {
        (o["side"], round(o["price"], 1))
        for o in open_orders
        if o["product"] == SPREAD_SYMBOL
    }

    plans = []

    # Asks below fair value - edge: buy them.
    buy_headroom = Config.MAX_POSITION - position_qty
    for level in book["asks"][:Config.MAX_LEVELS]:
        if buy_headroom <= 0:
            break
        price = round(level["price"], 1)
        if price > fair_value - edge:
            break  # asks sorted ascending - nothing further is mispriced
        if ("buy", price) in our_open_prices:
            continue
        qty = min(level["qty"], buy_headroom)
        if qty <= 0:
            continue
        plans.append(("buy", qty, price))
        buy_headroom -= qty

    # Bids above fair value + edge: sell into them.
    sell_headroom = position_qty + Config.MAX_POSITION
    for level in book["bids"][:Config.MAX_LEVELS]:
        if sell_headroom <= 0:
            break
        price = round(level["price"], 1)
        if price < fair_value + edge:
            break  # bids sorted descending - nothing further is mispriced
        if ("sell", price) in our_open_prices:
            continue
        qty = min(level["qty"], sell_headroom)
        if qty <= 0:
            continue
        plans.append(("sell", qty, price))
        sell_headroom -= qty

    return plans

def stale_cancels(fair_value, open_orders):
    edge = Config.MIN_EDGE_TICKS * Config.TICK_LEVEL
    to_cancel = []
    for o in open_orders:
        if o["product"] != SPREAD_SYMBOL:
            continue
        if o["side"] == "buy" and o["price"] > fair_value - edge:
            to_cancel.append(o["id"])
        elif o["side"] == "sell" and o["price"] < fair_value + edge:
            to_cancel.append(o["id"])
    return to_cancel

##
## Main Event Loop
##
def main():
    """
    Each tick:
    1. Fetch products (for the two legs' index prices -> fair_value),
       account positions, open orders, and the spread's own book - all
       concurrently.
    2. Cancel any resting order that's no longer mispriced.
    3. Take every remaining mispriced level, bounded by MAX_POSITION.
    """
    api_key = register_or_login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    while True:
        products_f = POOL.submit(get_products)
        account_f = POOL.submit(get_account, headers)
        orders_f = POOL.submit(list_orders, headers)
        book_f = POOL.submit(get_book, SPREAD_SYMBOL, headers)

        products = products_f.result()
        account = account_f.result()
        open_orders = [o for o in orders_f.result() if o["status"] == "open"]
        book = book_f.result()

        fair_value = round(products[BTC_SYMBOL]["index_price"] - products[ETH_SYMBOL]["index_price"], 2)
        position_qty = account["positions"].get(SPREAD_SYMBOL, {}).get("qty", 0)

        cancel_ids = stale_cancels(fair_value, open_orders)
        take_plans = plan_takes(fair_value, book, position_qty, open_orders)

        if cancel_ids:
            for f in [POOL.submit(cancel_order, oid, headers) for oid in cancel_ids]:
                f.result()

        if take_plans:
            futures = [
                POOL.submit(submit_order, SPREAD_SYMBOL, side, "limit", qty, headers, price)
                for side, qty, price in take_plans
            ]
            for (side, qty, price), f in zip(take_plans, futures):
                if f.result() is not None:
                    print(f"TOOK {side.upper()} {qty}x {SPREAD_SYMBOL} @ {price} (fair={fair_value})")

        if Config.POLL_INTERVAL:
            time.sleep(Config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
