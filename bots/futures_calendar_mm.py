"""
Futures / calendar-spread market maker — runs against your own private
account (not a notebook/teaching example), quoting every live futures
contract and calendar spread on the underlyings in Config.UNDERLYINGS.

Same quoting shape as the exchange's own built-in MarketMakerBot
(src/exchange/bots.py): `LEGS` bid/ask pairs per symbol, leg i sitting
MIN_SPREAD_TICKS + i*DELTA_TICKS ticks from mid on each side, skewed by
your own current position in that symbol so a filled side gets walked back
toward flat rather than quoted right back into the same fill. The
difference is this runs as an external HTTP client against your own
account, same as a student bot would, not inside the exchange process.

Discovery: futures/calendar-spread symbols roll over time (a 1-hour ladder,
see build-spec / src/exchange/futures.py) and there's no public-API route
that lists them (see docs/API.md's "Instruments you can't discover"
section) — this bot reads the website's unauthenticated `futures_matrix`
WS channel every poll to get the current symbol roster, expiry, and theo
price for each live contract/spread, then quotes each one over the public
trading API with your own API key.

Usage: edit the Config block below (BASE_URL/WS_BASE_URL for a hosted
instance, ACCOUNT_ID/PASSWORD for your account), then:

    python bots/futures_calendar_mm.py

Dependency gotcha: this needs the `websocket-client` package, but PyPI also
has an unrelated package literally called `websocket` that installs into
the exact same `websocket` import namespace — if both (or just the wrong
one) end up installed, `import websocket` succeeds but
`websocket.create_connection` doesn't exist, and every poll fails with
`module 'websocket' has no attribute 'create_connection'`. Fix:
`pip uninstall websocket; pip install websocket-client`.
"""
import json
import math
import signal
import time

import requests
from requests.adapters import HTTPAdapter

try:
    from websocket import create_connection
except ImportError as e:
    raise ImportError(
        "could not import create_connection from `websocket` — you have the wrong "
        "package installed (there's an unrelated PyPI package literally called "
        "`websocket` that shadows the same import name as `websocket-client`). "
        "Run: pip uninstall websocket; pip install websocket-client"
    ) from e

##
## Config
##
class Config:
    BASE_URL: str = "http://178.105.55.5:8000"       # public trading API
    WS_URL: str = "ws://178.105.55.5:8090/ws"         # website's multi-channel WS (unauthenticated)
    ACCOUNT_ID: str = "my-mm-bot"
    PASSWORD: str = "change-me"

    # Which futures underlyings to make markets on — must match (a subset
    # of) config.yaml's futures.underlyings on the server; quoting a
    # symbol the server doesn't recognize just gets rejected per-order,
    # it isn't fatal.
    UNDERLYINGS: list[str] = ["BTC-MINI", "ETH-MINI"]
    TICK_SIZE: float = 0.10  # config.yaml -> futures.tick_size

    LEGS: int = 2
    MIN_SPREAD_TICKS: float = 3.0
    DELTA_TICKS: float = 2.0
    QUOTE_SIZE: int = 2
    SKEW_SENSITIVITY: float = 0.05     # how much inventory shifts the quote midpoint
    MAX_INVENTORY: int = 20            # per symbol; past this, only the de-risking side is quoted

    REQUOTE_INTERVAL: float = 3.0      # seconds between full requote passes
    MATRIX_POLL_TIMEOUT: float = 2.0   # seconds to wait for one futures_matrix snapshot

    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

##
## API call functions — same register-or-login + 429 retry/backoff pattern
## as notebook/vol_trader.py.
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

def get_account(headers):
    r = _request("GET", f"{Config.BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, qty, headers, price=None):
    body = {"product": product, "side": side, "type": "market" if price is None else "limit", "qty": qty}
    if price is not None:
        body["price"] = price
    r = _request("POST", f"{Config.BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        detail = r.json().get("detail", r.text)
        print(f"REJECTED {side} {qty}x {product} @ {price}: {detail}")
        return None
    return r.json()

def cancel_order(order_id, headers):
    r = _request("DELETE", f"{Config.BASE_URL}/orders/{order_id}", headers=headers)
    return r.ok

##
## Futures/calendar-spread discovery — one fresh WS connection per poll.
## ws_feed (src/exchange/website.py) always sends the current snapshot on
## the very first tick after connecting (its dedup-by-content cache starts
## empty), so this doesn't need a persistent connection to stay current.
##
def fetch_futures_matrix():
    ws = create_connection(
        f"{Config.WS_URL}?channels=futures_matrix", timeout=Config.MATRIX_POLL_TIMEOUT,
    )
    try:
        msg = json.loads(ws.recv())
        return msg["data"]
    finally:
        ws.close()

def live_symbols(matrix):
    """Yields (symbol, theo_price) for every live futures contract and
    calendar spread across Config.UNDERLYINGS, theo_price being the
    'last' field (the same synthetic index/theo price the server's own MM
    bots quote around, not this bot's own resting book)."""
    for underlying in Config.UNDERLYINGS:
        info = matrix.get(underlying) or {"futures": [], "calendar_spreads": []}
        for f in info["futures"]:
            if f["last"] is not None:
                yield f["symbol"], f["last"]
        for c in info["calendar_spreads"]:
            if c["last"] is not None:
                yield c["symbol"], c["last"]

##
## Quoting — same ladder shape as MarketMakerBot.maybe_requote
## (src/exchange/bots.py): leg i sits MIN_SPREAD_TICKS + i*DELTA_TICKS
## ticks from mid on each side, skewed by inventory.
##
def snap_to_tick(price):
    return round(round(price / Config.TICK_SIZE) * Config.TICK_SIZE, 2)

def quote_symbol(symbol, theo, position_qty, headers):
    skew = -position_qty * Config.SKEW_SENSITIVITY
    mid = theo + skew
    quote_bid = abs(position_qty) < Config.MAX_INVENTORY or position_qty < 0
    quote_ask = abs(position_qty) < Config.MAX_INVENTORY or position_qty > 0

    new_order_ids = []
    for leg in range(Config.LEGS):
        offset = (Config.MIN_SPREAD_TICKS + leg * Config.DELTA_TICKS) * Config.TICK_SIZE
        bid_price = snap_to_tick(mid - offset)
        ask_price = snap_to_tick(mid + offset)
        if ask_price <= bid_price:
            ask_price = snap_to_tick(bid_price + Config.TICK_SIZE)

        if quote_bid:
            resp = submit_order(symbol, "buy", Config.QUOTE_SIZE, headers, price=bid_price)
            if resp is not None and resp["status"] in ("open", "partially_filled"):
                new_order_ids.append(resp["id"])
        if quote_ask:
            resp = submit_order(symbol, "sell", Config.QUOTE_SIZE, headers, price=ask_price)
            if resp is not None and resp["status"] in ("open", "partially_filled"):
                new_order_ids.append(resp["id"])
    return new_order_ids

##
## Shutdown — cancel every resting order and flatten every position before
## exiting, same discipline as notebook/vol_trader.py's flatten().
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten_all(resting_order_ids, headers):
    for oid in resting_order_ids:
        cancel_order(oid, headers)
    account = get_account(headers)
    for product, pos in account["positions"].items():
        if pos["qty"] == 0:
            continue
        side = "sell" if pos["qty"] > 0 else "buy"
        submit_order(product, side, abs(pos["qty"]), headers)
    print("flattened and cancelled all resting orders")

##
## Main loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = register_or_login(Config.ACCOUNT_ID, Config.PASSWORD)
    headers = {"X-API-Key": api_key}
    print(f"logged in as {Config.ACCOUNT_ID}")

    resting_order_ids: dict[str, list[int]] = {}  # symbol -> our own resting order ids

    try:
        while True:
            tick_start = time.monotonic()
            try:
                matrix = fetch_futures_matrix()
            except Exception as e:
                print(f"futures_matrix fetch failed ({e}), retrying next tick")
                time.sleep(Config.REQUOTE_INTERVAL)
                continue

            account = get_account(headers)
            live = dict(live_symbols(matrix))

            # Drop tracking for anything that rolled off since the last
            # poll — the server already cancelled/settled those orders
            # itself (see FuturesChainManager._settle_and_retire_future /
            # _retire_calendar_spread), there's nothing left to cancel.
            for stale_symbol in set(resting_order_ids) - set(live):
                resting_order_ids.pop(stale_symbol, None)

            for symbol, theo in live.items():
                for oid in resting_order_ids.pop(symbol, []):
                    cancel_order(oid, headers)
                position_qty = account["positions"].get(symbol, {}).get("qty", 0)
                resting_order_ids[symbol] = quote_symbol(symbol, theo, position_qty, headers)

            print(f"{time.strftime('%H:%M:%S')} quoting {len(live)} live symbols "
                  f"across {len(Config.UNDERLYINGS)} underlyings, equity=${account['equity']:.2f}")

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.REQUOTE_INTERVAL - elapsed))

    except ShutdownRequested:
        all_ids = [oid for ids in resting_order_ids.values() for oid in ids]
        flatten_all(all_ids, headers)


if __name__ == "__main__":
    main()
