"""
BTC/ETH spread-spot combo arbitrage — runs against your own private account.

BTC-ETH-MINI is, by construction, worth btc_index - eth_index. That means
a position of +1 spread, -1 BTC-MINI, +1 ETH-MINI is PERMANENTLY flat:

  d(total pnl)/d(btc) = (+1 from spread) + (-1 from the BTC-MINI leg) = 0
  d(total pnl)/d(eth) = (-1 from spread) + (+1 from the ETH-MINI leg) = 0

...for any future move in either price, not just right now — this is an
exact linear identity, not an approximation like an option's delta. So if
the three order books ever let you execute all three legs at prices where
the combo turns a profit, that profit is *locked in permanently the moment
all three legs fill* — not a directional bet that needs to be right later.

Two directions, using only real resting book prices (top of book):

  buy spread @ spread_ask, sell BTC-MINI @ btc_bid, buy ETH-MINI @ eth_ask
    profit per unit = btc_bid - eth_ask - spread_ask

  sell spread @ spread_bid, buy BTC-MINI @ btc_ask, sell ETH-MINI @ eth_bid
    profit per unit = spread_bid - btc_ask + eth_bid

Both fire as MARKET orders on all three legs, not marketable limits at the
exact prices measured. A limit order that doesn't fully match immediately
rests for the remainder instead of failing outright — and a resting leg
can then fill later, on its own, at an unrelated moment, silently breaking
the exact 1:1:1 ratio the whole hedge argument depends on. That's worse
than paying a little extra slippage: a market order either executes the
full size now (this bot only ever sizes to what the book already showed
available, so in practice it does) or the fill comes back short and this
tick's result is treated as a broken combo needing cleanup — never a
quietly mismatched position discovered later.

MAX_POSITION note: the server's position cap is balance-relative
(`max_position = floor(cash * leverage / mark_price)`, see
src/exchange/ledger.py:max_position_for), not a fixed contract count, and
isn't returned for the spread instrument by any public-API route (see
docs/API.md's "Instruments you can't discover" section) — `max_position_for`
below mirrors that formula exactly (same pattern notebook/vol_trader.py
uses for Black-Scholes) so this bot's own headroom math matches what the
server will actually accept, using GET /products' own `leverage`/
`index_price` for BTC-MINI/ETH-MINI and SPREAD_LEVERAGE (hardcoded from
config.yaml's `spread.leverage`, which falls back to `risk.default_leverage`)
for the spread itself. When the spread's fair value (btc_index - eth_index)
is itself exactly zero, `max_position_for` falls back to TICK_SIZE as the
sizing price instead of treating that as "no price" — matching the
server's own fallback so a legitimate zero price doesn't zero out this
bot's headroom.
"""
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

##
## Config
##
class Config:
    BASE_URL: str = "http://178.105.55.5:8000"
    ACCOUNT_ID: str = "my-spread-arb-bot"
    PASSWORD: str = "change-me"

    SPREAD_SYMBOL: str = "BTC-ETH-MINI"
    BTC_SYMBOL: str = "BTC-MINI"
    ETH_SYMBOL: str = "ETH-MINI"

    TICK_SIZE: float = 0.10   # shared by BTC-MINI/ETH-MINI/BTC-ETH-MINI (config.yaml)
    MIN_EDGE_TICKS: int = 3   # combo profit must clear this many ticks per unit — comfortably covers 3 legs' worth of taker fee
    SPREAD_LEVERAGE: float = 5.0  # config.yaml -> spread.leverage (falls back to risk.default_leverage)

    POLL_INTERVAL: float = 0.0
    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spread-spot-arb-io")

##
## API call functions
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
    r = _request("POST", f"{Config.BASE_URL}/register", json={"account_id": account_id, "password": password})
    if r.status_code == 409:
        r = _request("POST", f"{Config.BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_products():
    r = _request("GET", f"{Config.BASE_URL}/products")
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}

def get_book(product, headers):
    r = _request("GET", f"{Config.BASE_URL}/book/{product}", headers=headers)
    r.raise_for_status()
    return r.json()

def get_account(headers):
    r = _request("GET", f"{Config.BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_market_order(product, side, qty, headers):
    r = _request("POST", f"{Config.BASE_URL}/orders", headers=headers,
                 json={"product": product, "side": side, "type": "market", "qty": qty})
    return r.json() if r.ok else None

##
## MAX_POSITION — mirrors ledger.max_position_for exactly (see module
## docstring) so headroom() matches what the server will actually accept.
##
def max_position_for(cash, leverage, mark_price, tick_size=Config.TICK_SIZE):
    # mark_price == 0 is a legitimate price (the spread trading exactly at
    # btc_index == eth_index), not "no price yet" (mark_price is None) —
    # falls back to tick_size as the sizing price in that case. See
    # ledger.max_position_for's docstring for the full reasoning; this
    # mirrors it exactly.
    if mark_price is None:
        return 0
    sizing_price = mark_price if mark_price != 0 else tick_size
    if not sizing_price:
        return 0
    notional_capacity = max(cash, 0.0) * leverage
    return max(1, int(notional_capacity / abs(sizing_price)))

def headroom(account, symbol, max_position, side):
    qty = account["positions"].get(symbol, {}).get("qty", 0)
    return (max_position - qty) if side == "buy" else (max_position + qty)

##
## Combo detection and sizing
##
def best_levels(book):
    bid = book["bids"][0] if book["bids"] else None
    ask = book["asks"][0] if book["asks"] else None
    return bid, ask

def plan_combo(spread_book, btc_book, eth_book, account, spread_max_position, btc_max_position, eth_max_position):
    """Returns (direction, qty, profit, legs) or None. legs is
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
                headroom(account, Config.SPREAD_SYMBOL, spread_max_position, "buy"),
                headroom(account, Config.BTC_SYMBOL, btc_max_position, "sell"),
                headroom(account, Config.ETH_SYMBOL, eth_max_position, "buy"),
            )
            if qty > 0:
                return "long_spread", qty, profit, [
                    (Config.SPREAD_SYMBOL, "buy", spread_ask["price"]),
                    (Config.BTC_SYMBOL, "sell", btc_bid["price"]),
                    (Config.ETH_SYMBOL, "buy", eth_ask["price"]),
                ]

    # Direction 2: sell spread, buy BTC, sell ETH
    if spread_bid and btc_ask and eth_bid:
        profit = spread_bid["price"] - btc_ask["price"] + eth_bid["price"]
        if profit > edge:
            qty = min(
                spread_bid["qty"], btc_ask["qty"], eth_bid["qty"],
                headroom(account, Config.SPREAD_SYMBOL, spread_max_position, "sell"),
                headroom(account, Config.BTC_SYMBOL, btc_max_position, "buy"),
                headroom(account, Config.ETH_SYMBOL, eth_max_position, "sell"),
            )
            if qty > 0:
                return "short_spread", qty, profit, [
                    (Config.SPREAD_SYMBOL, "sell", spread_bid["price"]),
                    (Config.BTC_SYMBOL, "buy", btc_ask["price"]),
                    (Config.ETH_SYMBOL, "sell", eth_bid["price"]),
                ]

    return None

##
## Shutdown — flatten all three legs (hygiene, not risk — see module docstring)
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(headers):
    print("shutting down: flattening all three legs...")
    account = get_account(headers)
    for symbol in (Config.SPREAD_SYMBOL, Config.BTC_SYMBOL, Config.ETH_SYMBOL):
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

    api_key = register_or_login(Config.ACCOUNT_ID, Config.PASSWORD)
    headers = {"X-API-Key": api_key}
    print(f"logged in as {Config.ACCOUNT_ID}")

    try:
        while True:
            products_f = POOL.submit(get_products)
            account_f = POOL.submit(get_account, headers)
            spread_book_f = POOL.submit(get_book, Config.SPREAD_SYMBOL, headers)
            btc_book_f = POOL.submit(get_book, Config.BTC_SYMBOL, headers)
            eth_book_f = POOL.submit(get_book, Config.ETH_SYMBOL, headers)

            products = products_f.result()
            account = account_f.result()
            spread_book = spread_book_f.result()
            btc_book = btc_book_f.result()
            eth_book = eth_book_f.result()

            btc = products[Config.BTC_SYMBOL]
            eth = products[Config.ETH_SYMBOL]
            btc_max_position = max_position_for(account["cash"], btc["leverage"], btc["index_price"])
            eth_max_position = max_position_for(account["cash"], eth["leverage"], eth["index_price"])
            spread_index = (
                btc["index_price"] - eth["index_price"]
                if btc["index_price"] is not None and eth["index_price"] is not None else None
            )
            spread_max_position = max_position_for(account["cash"], Config.SPREAD_LEVERAGE, spread_index)

            plan = plan_combo(spread_book, btc_book, eth_book, account, spread_max_position, btc_max_position, eth_max_position)

            if plan is not None:
                direction, qty, profit, legs = plan
                futures = [POOL.submit(submit_market_order, symbol, side, qty, headers) for symbol, side, price in legs]
                results = [f.result() for f in futures]
                # A market order response with status != "filled" or a
                # nonzero remaining_qty means that leg didn't get the full
                # size right now — only a clean "filled, nothing left" on
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
                    # Not all three legs landed — the position is no longer
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
