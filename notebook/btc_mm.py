"""
BTC-MINI market maker.

Quotes both sides around the live index price, tight but not
loss-leading, with the same shape as options_mm.py's quoting logic:

  half_spread = max(MIN_HALF_SPREAD_TICKS ticks, index_price * SPREAD_FRAC)
  bid/ask     = index_price (+/-) half_spread, shifted by an inventory
                skew so a position that's drifted long gets sold off
                (ask drops, more likely to get lifted) and a short
                position gets bought back

Quotes only get cancelled/replaced once the target price actually moves
by >= REQUOTE_EPS_TICKS - re-submitting an unchanged price every tick
just burns the rate limit and pays the taker side of your own spread for
nothing. MAX_POSITION is read live from GET /products rather than
hardcoded (unlike the options chain / BTC-ETH spread, a real spot product
does expose it there - see docs/API.md).

See eth_mm.py for the ETH-MINI twin - deliberately the same file shape
(not a shared import) so either one can be read and run standalone,
matching every other bot in this directory.
"""
import time
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "btcmm"
PASSWORD: str = "cuquants"

PRODUCT: str = "BTC-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="btc-mm-io")

##
## Config
##
class Config:
    TICK_SIZE: float = 0.10   # BTC-MINI/ETH-MINI tick size (config.yaml) - not exposed by GET /products

    SPREAD_FRAC: float = 0.003        # half-spread as a fraction of index price
    MIN_HALF_SPREAD_TICKS: int = 2    # ...but never tighter than this many ticks
    SKEW_SENSITIVITY: float = 0.15    # ticks of quote-shift per contract of inventory
    QUOTE_SIZE: int = 3               # contracts per side
    REQUOTE_EPS_TICKS: int = 1        # only cancel/replace once target moves >= this many ticks

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
## Quoting
##
def round_to_tick(price, tick):
    return round(round(price / tick) * tick, 2)

def desired_quotes(index_price, position_qty):
    half_spread = max(Config.MIN_HALF_SPREAD_TICKS * Config.TICK_SIZE, index_price * Config.SPREAD_FRAC)
    shift = -position_qty * Config.SKEW_SENSITIVITY * Config.TICK_SIZE
    bid = round_to_tick(max(Config.TICK_SIZE, index_price - half_spread + shift), Config.TICK_SIZE)
    ask = round_to_tick(max(bid + Config.TICK_SIZE, index_price + half_spread + shift), Config.TICK_SIZE)
    return bid, ask

def plan_quotes(index_price, position_qty, max_position, open_by_side):
    target_bid, target_ask = desired_quotes(index_price, position_qty)
    buy_headroom = max_position - position_qty
    sell_headroom = max_position + position_qty
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
    order, reason = submit_order(PRODUCT, side, target_qty, headers, target_price)
    if order is not None:
        return f"{side} {target_qty}x {PRODUCT} @ {target_price}"
    return f"{side} {PRODUCT} REJECTED: {reason}"

##
## Shutdown — cancel resting quotes and flatten the position
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(headers):
    print("shutting down: cancelling quotes and flattening...")
    orders = get_orders(headers)
    open_orders = [o for o in orders if o["product"] == PRODUCT and o["status"] in ("open", "partially_filled")]
    for f in [POOL.submit(cancel_order, o["id"], headers) for o in open_orders]:
        f.result()

    account = get_account(headers)
    pos = account["positions"].get(PRODUCT, {}).get("qty", 0)
    if pos != 0:
        side = "sell" if pos > 0 else "buy"
        submit_market_order(PRODUCT, side, abs(pos), headers)
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
            index_price = products[PRODUCT]["index_price"]
            max_position = products[PRODUCT]["max_position"]

            account = get_account(headers)
            position_qty = account["positions"].get(PRODUCT, {}).get("qty", 0)

            orders = get_orders(headers)
            open_by_side = {}
            for o in orders:
                if o["product"] == PRODUCT and o["status"] in ("open", "partially_filled"):
                    open_by_side[o["side"]] = o

            plans = plan_quotes(index_price, position_qty, max_position, open_by_side)
            futures = [POOL.submit(execute_requote, side, price, qty, current, headers)
                       for side, price, qty, current in plans]
            actions = [f.result() for f in futures if f.result() is not None]

            if actions:
                print(f"{datetime.now(timezone.utc).isoformat()}  index={index_price:.2f}  "
                      f"position={position_qty:+d}  " + "; ".join(actions))

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

    except ShutdownRequested:
        flatten(headers)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
