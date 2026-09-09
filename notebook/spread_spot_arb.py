"""
Spread-spot combo arbitrage.

BTC-ETH-MINI is, by construction, worth btc_index - eth_index. That means
a position of +1 spread, -1 BTC-MINI, +1 ETH-MINI is PERMANENTLY flat:

  d(total pnl)/d(btc) = (+1 from spread) + (-1 from the BTC-MINI leg) = 0
  d(total pnl)/d(eth) = (-1 from spread) + (+1 from the ETH-MINI leg) = 0

...for any future move in either price, not just right now - this is an
exact linear identity, not an approximation like an option's delta. So if
the three order books ever let you execute all three legs at prices where
the combo turns a profit, that profit is *locked in permanently the
moment all three legs fill* - not a directional bet that needs to be
right later, and not a spread that needs to mean-revert. That's the
textbook definition of arbitrage, and it's the reason this bot is
structurally different from spread_arb.py (which compares the spread's
book to a computed index - a real but non-tradable reference price) and
spread_mm.py (which captures the spread's own bid-ask and then hedges
away the resulting directional risk as a *separate* step).

Two directions, using only real resting book prices (top of book):

  buy spread @ spread_ask, sell BTC-MINI @ btc_bid, buy ETH-MINI @ eth_ask
    profit per unit = btc_bid - eth_ask - spread_ask

  sell spread @ spread_bid, buy BTC-MINI @ btc_ask, sell ETH-MINI @ eth_bid
    profit per unit = spread_bid - btc_ask + eth_bid

Both fire as MARKET orders on all three legs, not marketable limits at the
exact prices measured. A limit order that doesn't fully match immediately
rests for the remainder instead of failing outright - and a resting leg
can then fill later, on its own, at an unrelated moment, silently
breaking the exact 1:1:1 ratio the whole hedge argument above depends on.
That's worse than paying a little extra slippage: a market order either
executes the full size now (this bot only ever sizes to what the book
already showed available, so in practice it does) or the fill comes back
short and this tick's result is treated as a broken combo needing cleanup
- never a quietly mismatched position discovered later. The MIN_EDGE_TICKS
margin exists partly to absorb this: a few ticks of slippage tolerance is
cheaper than a hedge that turns out not to be one.

Flattening on exit isn't required for risk reasons (the position is
self-hedged forever, per the math above) - it's done purely to free up
MAX_POSITION headroom and leave the account tidy, matching every other
bot in this directory.
"""
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spread-spot-arb-io")

##
## Config
##
class Config:
    TICK_SIZE: float = 0.10   # shared by BTC-MINI/ETH-MINI/BTC-ETH-MINI (config.yaml)
    MIN_EDGE_TICKS: int = 3   # combo profit must clear this many ticks per unit to fire - comfortably covers 3 legs' worth of taker fee
    SPREAD_MAX_POSITION: int = 75  # matches config.yaml spread.max_position - not discoverable via GET /products

    POLL_INTERVAL: float = 0.0
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

def get_book(product, headers):
    r = _request("GET", f"{BASE_URL}/book/{product}", headers=headers)
    r.raise_for_status()
    return r.json()

def get_account(headers):
    r = _request("GET", f"{BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_market_order(product, side, qty, headers):
    r = _request("POST", f"{BASE_URL}/orders", headers=headers,
                 json={"product": product, "side": side, "type": "market", "qty": qty})
    return r.json() if r.ok else None

##
## Combo detection and sizing
##
def best_levels(book):
    bid = book["bids"][0] if book["bids"] else None
    ask = book["asks"][0] if book["asks"] else None
    return bid, ask

def headroom(account, symbol, max_position, side):
    qty = account["positions"].get(symbol, {}).get("qty", 0)
    return (max_position - qty) if side == "buy" else (max_position + qty)

def plan_combo(spread_book, btc_book, eth_book, account, btc_max_position, eth_max_position):
    """Returns (direction, qty, legs) or None. legs is
    [(symbol, side, price), ...] for the three orders to fire concurrently."""
    edge = Config.MIN_EDGE_TICKS * Config.TICK_SIZE

    spread_bid, spread_ask = best_levels(spread_book)
    btc_bid, btc_ask = best_levels(btc_book)
    eth_bid, eth_ask = best_levels(eth_book)

    # Direction 1: buy spread, sell BTC, buy ETH
    if spread_ask and btc_bid and eth_ask:
        profit = btc_bid["price"] - eth_ask["price"] - spread_ask["price"]
        if profit > edge:
            qty = min(
                spread_ask["qty"], btc_bid["qty"], eth_ask["qty"],
                headroom(account, SPREAD_SYMBOL, Config.SPREAD_MAX_POSITION, "buy"),
                headroom(account, BTC_SYMBOL, btc_max_position, "sell"),
                headroom(account, ETH_SYMBOL, eth_max_position, "buy"),
            )
            if qty > 0:
                return "long_spread", qty, profit, [
                    (SPREAD_SYMBOL, "buy", spread_ask["price"]),
                    (BTC_SYMBOL, "sell", btc_bid["price"]),
                    (ETH_SYMBOL, "buy", eth_ask["price"]),
                ]

    # Direction 2: sell spread, buy BTC, sell ETH
    if spread_bid and btc_ask and eth_bid:
        profit = spread_bid["price"] - btc_ask["price"] + eth_bid["price"]
        if profit > edge:
            qty = min(
                spread_bid["qty"], btc_ask["qty"], eth_bid["qty"],
                headroom(account, SPREAD_SYMBOL, Config.SPREAD_MAX_POSITION, "sell"),
                headroom(account, BTC_SYMBOL, btc_max_position, "buy"),
                headroom(account, ETH_SYMBOL, eth_max_position, "sell"),
            )
            if qty > 0:
                return "short_spread", qty, profit, [
                    (SPREAD_SYMBOL, "sell", spread_bid["price"]),
                    (BTC_SYMBOL, "buy", btc_ask["price"]),
                    (ETH_SYMBOL, "sell", eth_bid["price"]),
                ]

    return None

##
## Shutdown — flatten all three legs (hygiene, not risk - see module docstring)
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(headers):
    print("shutting down: flattening all three legs...")
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
            products_f = POOL.submit(get_products)
            account_f = POOL.submit(get_account, headers)
            spread_book_f = POOL.submit(get_book, SPREAD_SYMBOL, headers)
            btc_book_f = POOL.submit(get_book, BTC_SYMBOL, headers)
            eth_book_f = POOL.submit(get_book, ETH_SYMBOL, headers)

            products = products_f.result()
            account = account_f.result()
            spread_book = spread_book_f.result()
            btc_book = btc_book_f.result()
            eth_book = eth_book_f.result()

            plan = plan_combo(
                spread_book, btc_book, eth_book, account,
                products[BTC_SYMBOL]["max_position"], products[ETH_SYMBOL]["max_position"],
            )

            if plan is not None:
                direction, qty, profit, legs = plan
                futures = [POOL.submit(submit_market_order, symbol, side, qty, headers) for symbol, side, price in legs]
                results = [f.result() for f in futures]
                # A market order response with status != "filled" or a
                # nonzero remaining_qty means that leg didn't get the full
                # size right now - only a clean "filled, nothing left" on
                # every leg confirms the intended 1:1:1 ratio actually landed.
                fully_filled = all(
                    r is not None and r["status"] == "filled" and r["remaining_qty"] == 0
                    for r in results
                )
                if fully_filled:
                    print(f"{datetime.now(timezone.utc).isoformat()}  {direction}  qty={qty}  "
                          f"locked_edge=${profit:.2f}/unit  " +
                          "; ".join(f"{side} {qty}x {symbol} (measured @ {price})" for symbol, side, price in legs))
                else:
                    # Not all three legs landed - the position is no longer
                    # guaranteed flat. Print loudly; flatten() on shutdown
                    # (or the next detected combo in the opposite direction)
                    # will clean it up rather than compounding blind here.
                    problems = [
                        f"{symbol}: {'rejected' if r is None else r['status'] + ' remaining=' + str(r['remaining_qty'])}"
                        for (symbol, _, _), r in zip(legs, results)
                        if r is None or r["status"] != "filled" or r["remaining_qty"] != 0
                    ]
                    print(f"PARTIAL COMBO FILL - {problems} - now carrying real exposure until cleaned up")

            if Config.POLL_INTERVAL:
                time.sleep(Config.POLL_INTERVAL)

    except ShutdownRequested:
        flatten(headers)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
