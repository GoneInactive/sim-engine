"""
Spread market maker, hedged on the two legs.

Quotes both sides of BTC-ETH-MINI's own book around fair value
(btc_index - eth_index, computed client-side - the spread isn't listed on
GET /products, see docs/API.md), same tight/inventory-aware shape as
btc_mm.py/options_mm.py. Fair value can legitimately be negative (an admin
"invert" event is designed to push it there) - unlike a normal spot
product's quotes, this bot's bid/ask are never floored at a positive
price; see ProductConfig.allow_negative_price in src/exchange/config.py.

The hedge: holding +1 unit of BTC-ETH-MINI is, by construction
(fair_value = btc_index - eth_index), economically identical to being
+1 delta to BTC-MINI and -1 delta to ETH-MINI - no Black-Scholes needed
here, it's a linear instrument, not an option. So the instant this bot's
own spread position moves off zero (a quote got lifted/hit), it hedges by
trading the two legs in the exact opposite proportion:

  spread position = +Q  ->  hedge = short Q BTC-MINI, long Q ETH-MINI
  spread position = -Q  ->  hedge = long Q BTC-MINI, short Q ETH-MINI

That converts "spread inventory carrying embedded BTC/ETH direction" into
"captured bid-ask edge sitting in three flat legs" - the same reason a
real index/spread market maker hedges on the underlying instead of just
carrying spread inventory and hoping it mean-reverts. Hedge legs are
market orders (pay the taker fee, but lock in the edge immediately)
rather than resting limit orders that might never fill.
"""
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "Knight Capital"
PASSWORD: str = "poop"

SPREAD_SYMBOL: str = "BTC-ETH-MINI"
BTC_SYMBOL: str = "BTC-MINI"
ETH_SYMBOL: str = "ETH-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spread-mm-io")

##
## Config
##
class Config:
    TICK_SIZE: float = 0.10   # matches config.yaml spread.tick_size
    MAX_POSITION: int = 75    # matches config.yaml spread.max_position - not discoverable via GET /products

    SPREAD_FRAC: float = 0.02          # half-spread as a fraction of |fair_value|
    MIN_HALF_SPREAD_TICKS: int = 2     # ...but never tighter than this many ticks
    SKEW_SENSITIVITY: float = 0.15     # ticks of quote-shift per contract of inventory
    QUOTE_SIZE: int = 3                # contracts per side
    REQUOTE_EPS_TICKS: int = 1         # only cancel/replace once target moves >= this many ticks

    HEDGE_BAND: int = 1        # hedge once |spread position| reaches this many contracts - tight, since the hedge is exact (linear), not approximate like an option's delta

    POLL_SECONDS: float = 1.0
    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25

##
## API Call Functions
##
def _request(method, url, **kwargs):
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

def get_account(headers):
    r = _request("GET", f"{BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def get_orders(headers):
    r = _request("GET", f"{BASE_URL}/orders", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, qty, headers, price):
    r = _request("POST", f"{BASE_URL}/orders", headers=headers,
                 json={"product": product, "side": side, "type": "limit", "qty": qty, "price": price})
    if not r.ok:
        return None, r.json().get("detail", r.text)
    return r.json(), None

def submit_market_order(product, side, qty, headers):
    r = _request("POST", f"{BASE_URL}/orders", headers=headers,
                 json={"product": product, "side": side, "type": "market", "qty": qty})
    return r.json() if r.ok else None

def cancel_order(order_id, headers):
    r = _request("DELETE", f"{BASE_URL}/orders/{order_id}", headers=headers)
    return r.ok

##
## Quoting — same shape as btc_mm.py, minus the positive-price floor
## (fair_value can legitimately be negative on this product).
##
def round_to_tick(price, tick):
    return round(round(price / tick) * tick, 2)

def desired_quotes(fair_value, position_qty):
    half_spread = max(Config.MIN_HALF_SPREAD_TICKS * Config.TICK_SIZE, abs(fair_value) * Config.SPREAD_FRAC)
    shift = -position_qty * Config.SKEW_SENSITIVITY * Config.TICK_SIZE
    bid = round_to_tick(fair_value - half_spread + shift, Config.TICK_SIZE)
    ask = round_to_tick(max(bid + Config.TICK_SIZE, fair_value + half_spread + shift), Config.TICK_SIZE)
    return bid, ask

def plan_quotes(fair_value, position_qty, open_by_side):
    target_bid, target_ask = desired_quotes(fair_value, position_qty)
    buy_headroom = Config.MAX_POSITION - position_qty
    sell_headroom = Config.MAX_POSITION + position_qty
    target_bid_qty = min(Config.QUOTE_SIZE, max(0, buy_headroom))
    target_ask_qty = min(Config.QUOTE_SIZE, max(0, sell_headroom))

    eps = Config.REQUOTE_EPS_TICKS * Config.TICK_SIZE
    plans = []
    for side, target_price, target_qty in (("buy", target_bid, target_bid_qty), ("sell", target_ask, target_ask_qty)):
        current = open_by_side.get(side)
        current_price = current["price"] if current is not None else None
        needs_replace = (
            (current is None and target_qty > 0)
            or (current is not None and target_qty <= 0)
            or (current is not None and target_qty > 0 and current_price is None)
            or (current is not None and target_qty > 0 and current_price is not None and abs(current_price - target_price) >= eps)
        )
        if needs_replace:
            plans.append((side, target_price, target_qty, current))
    return plans

def execute_requote(side, target_price, target_qty, current, headers):
    if current is not None:
        cancel_order(current["id"], headers)
    if target_qty <= 0:
        return None
    order, reason = submit_order(SPREAD_SYMBOL, side, target_qty, headers, target_price)
    if order is not None:
        return f"{side} {target_qty}x {SPREAD_SYMBOL} @ {target_price}"
    return f"{side} {SPREAD_SYMBOL} REJECTED: {reason}"

##
## Hedge — exact, linear, no Greeks. spread position +Q means +Q delta to
## BTC-MINI and -Q delta to ETH-MINI (see module docstring); hedging means
## holding the opposite in each leg.
##
def reconcile_hedge(spread_position_qty, account, headers):
    if abs(spread_position_qty) < Config.HEDGE_BAND:
        return []
    targets = {BTC_SYMBOL: -spread_position_qty, ETH_SYMBOL: spread_position_qty}
    actions = []
    for symbol, target in targets.items():
        current = account["positions"].get(symbol, {}).get("qty", 0)
        diff = target - current
        if diff == 0:
            continue
        side = "buy" if diff > 0 else "sell"
        resp = submit_market_order(symbol, side, abs(diff), headers)
        if resp is not None:
            actions.append(f"{side} {abs(diff)}x {symbol} (hedge)")
    return actions

##
## Shutdown — cancel resting spread quotes, flatten spread + both hedge legs
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(headers):
    print("shutting down: cancelling quotes and flattening...")
    orders = get_orders(headers)
    open_orders = [o for o in orders if o["product"] == SPREAD_SYMBOL and o["status"] in ("open", "partially_filled")]
    for f in [POOL.submit(cancel_order, o["id"], headers) for o in open_orders]:
        f.result()

    account = get_account(headers)
    for symbol in (SPREAD_SYMBOL, BTC_SYMBOL, ETH_SYMBOL):
        pos = account["positions"].get(symbol, {}).get("qty", 0)
        if pos != 0:
            side = "sell" if pos > 0 else "buy"
            submit_market_order(symbol, side, abs(pos), headers)
    print("flattened.")

##
## Main Event Loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = register_or_login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    try:
        while True:
            tick_start = time.monotonic()

            products = get_products()
            fair_value = round(products[BTC_SYMBOL]["index_price"] - products[ETH_SYMBOL]["index_price"], 2)

            account = get_account(headers)
            spread_position_qty = account["positions"].get(SPREAD_SYMBOL, {}).get("qty", 0)

            orders = get_orders(headers)
            open_by_side = {}
            for o in orders:
                if o["product"] == SPREAD_SYMBOL and o["status"] in ("open", "partially_filled"):
                    open_by_side[o["side"]] = o

            plans = plan_quotes(fair_value, spread_position_qty, open_by_side)
            futures = [POOL.submit(execute_requote, side, price, qty, current, headers)
                       for side, price, qty, current in plans]
            actions = [f.result() for f in futures if f.result() is not None]

            hedge_actions = reconcile_hedge(spread_position_qty, account, headers)
            actions += hedge_actions

            if actions:
                print(f"{datetime.now(timezone.utc).isoformat()}  fair_value={fair_value:.2f}  "
                      f"spread_position={spread_position_qty:+d}  " + "; ".join(actions))

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

    except ShutdownRequested:
        flatten(headers)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
