"""
Calendar-spread / futures combo arbitrage — runs against your own private
account.

Every calendar spread is, by construction, worth near_future - far_future
(see src/exchange/futures.py's FuturesChainManager.price_tick). That means
a position of +1 calendar spread, -1 near future, +1 far future is
PERMANENTLY flat, exactly the same linear-identity argument as
btc_eth_spread_arb.py's BTC-ETH-MINI combo:

  d(total pnl)/d(near) = (+1 from the spread) + (-1 from the near leg) = 0
  d(total pnl)/d(far)  = (-1 from the spread) + (+1 from the far leg)  = 0

Two directions per (near, far, calendar_spread) triple, using only real
resting book prices (top of book):

  buy CAL @ cal_ask, sell near @ near_bid, buy far @ far_ask
    profit per unit = near_bid - far_ask - cal_ask

  sell CAL @ cal_bid, buy near @ near_ask, sell far @ far_bid
    profit per unit = cal_bid - near_ask + far_bid

All three legs fire as MARKET orders, never marketable limits — a limit
order that doesn't fully match immediately rests for the remainder instead
of failing outright, and a resting leg filling later on its own would
silently break the exact 1:1:1 ratio the hedge argument depends on. See
btc_eth_spread_arb.py's module docstring for the full reasoning; it's
identical here, just with dynamically-rolling futures contracts instead of
a single static spread symbol.

Discovery: futures/calendar-spread symbols roll over time and aren't
listed by any public-API route (see docs/API.md's "Instruments you can't
discover" section) — this bot reads the website's unauthenticated
`futures_matrix` WS channel every poll (same mechanism as
futures_calendar_mm.py) to find every live (near, far, calendar_spread)
triple across Config.UNDERLYINGS.

MAX_POSITION: the server's position cap is balance-relative
(`floor(cash * leverage / mark_price)`, see
src/exchange/ledger.py:max_position_for) and isn't exposed for these
dynamic instruments via any public route — `max_position_for` below
mirrors that formula, using each live contract/spread's own `last` theo
price from `futures_matrix` as its mark price (the same number the server
itself sizes against). A calendar spread's theo is, per
FuturesChainManager's own docstring, "~0 most of the time" (both legs
track spot 1:1); the server (and this mirror) falls back to the
instrument's tick size as the sizing price whenever mark_price is exactly
zero, rather than treating a legitimate zero price as "no price yet" and
capping it to 0 — otherwise the calendar-spread leg of this combo would be
untradable by construction almost all the time.
"""
import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
    ACCOUNT_ID: str = "CTC"
    PASSWORD: str = "dev"

    UNDERLYINGS: list[str] = ["BTC-MINI", "ETH-MINI"]
    TICK_SIZE: float = 0.10        # config.yaml -> futures.tick_size
    FUTURES_LEVERAGE: float = 5.0  # config.yaml -> futures.leverage (falls back to risk.default_leverage)

    MIN_EDGE_TICKS: int = 3        # combo profit must clear this many ticks per unit — comfortably covers 3 legs' worth of taker fee
    MATRIX_POLL_TIMEOUT: float = 2.0

    POLL_INTERVAL: float = 0.5
    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cal-arb-io")

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

def login(account_id, password):
    """Your account already exists — plain /login, no register-then-409-
    fallback dance (that pattern is for a brand-new student account, see
    notebook/vol_trader.py's docstring; this is your own private account)."""
    r = _request("POST", f"{Config.BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_book(product, headers):
    r = _request("GET", f"{Config.BASE_URL}/book/{product}", headers=headers)
    if r.status_code == 404:
        return None  # rolled off between the last matrix poll and now
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
## Discovery — one fresh WS connection per poll, same as futures_calendar_mm.py.
##
def fetch_futures_matrix():
    ws = create_connection(f"{Config.WS_URL}?channels=futures_matrix", timeout=Config.MATRIX_POLL_TIMEOUT)
    try:
        msg = json.loads(ws.recv())
        return msg["data"]
    finally:
        ws.close()

def live_triples(matrix):
    """Yields (near_symbol, far_symbol, cal_symbol, near_theo, far_theo,
    cal_theo) for every live calendar spread across Config.UNDERLYINGS —
    theo prices are each symbol's `last` field, the same mark price the
    server itself uses for MAX_POSITION (see module docstring)."""
    for underlying in Config.UNDERLYINGS:
        info = matrix.get(underlying) or {"futures": [], "calendar_spreads": []}
        theo_by_symbol = {f["symbol"]: f["last"] for f in info["futures"]}
        for c in info["calendar_spreads"]:
            near_theo = theo_by_symbol.get(c["near"])
            far_theo = theo_by_symbol.get(c["far"])
            yield c["near"], c["far"], c["symbol"], near_theo, far_theo, c["last"]

##
## MAX_POSITION — mirrors ledger.max_position_for exactly (see module docstring).
##
def max_position_for(cash, leverage, mark_price, tick_size=Config.TICK_SIZE):
    # mark_price == 0 is a legitimate price (a calendar spread at parity),
    # not "no price yet" (mark_price is None) — falls back to tick_size as
    # the sizing price in that case. See ledger.max_position_for's
    # docstring for the full reasoning; this mirrors it exactly.
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
    if book is None:
        return None, None
    bid = book["bids"][0] if book["bids"] else None
    ask = book["asks"][0] if book["asks"] else None
    return bid, ask

def plan_combo(near_symbol, far_symbol, cal_symbol, near_book, far_book, cal_book, account,
                near_max_position, far_max_position, cal_max_position):
    """Same shape as btc_eth_spread_arb.plan_combo — see module docstring
    for the two directions' profit formulas."""
    edge = Config.MIN_EDGE_TICKS * Config.TICK_SIZE

    near_bid, near_ask = best_levels(near_book)
    far_bid, far_ask = best_levels(far_book)
    cal_bid, cal_ask = best_levels(cal_book)

    # Direction 1: buy CAL, sell near, buy far
    if cal_ask and near_bid and far_ask:
        profit = near_bid["price"] - far_ask["price"] - cal_ask["price"]
        if profit > edge:
            qty = min(
                cal_ask["qty"], near_bid["qty"], far_ask["qty"],
                headroom(account, cal_symbol, cal_max_position, "buy"),
                headroom(account, near_symbol, near_max_position, "sell"),
                headroom(account, far_symbol, far_max_position, "buy"),
            )
            if qty > 0:
                return "long_cal", qty, profit, [
                    (cal_symbol, "buy", cal_ask["price"]),
                    (near_symbol, "sell", near_bid["price"]),
                    (far_symbol, "buy", far_ask["price"]),
                ]

    # Direction 2: sell CAL, buy near, sell far
    if cal_bid and near_ask and far_bid:
        profit = cal_bid["price"] - near_ask["price"] + far_bid["price"]
        if profit > edge:
            qty = min(
                cal_bid["qty"], near_ask["qty"], far_bid["qty"],
                headroom(account, cal_symbol, cal_max_position, "sell"),
                headroom(account, near_symbol, near_max_position, "buy"),
                headroom(account, far_symbol, far_max_position, "sell"),
            )
            if qty > 0:
                return "short_cal", qty, profit, [
                    (cal_symbol, "sell", cal_bid["price"]),
                    (near_symbol, "buy", near_ask["price"]),
                    (far_symbol, "sell", far_bid["price"]),
                ]

    return None

##
## Shutdown — flatten every position this bot could plausibly be carrying.
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(headers):
    print("shutting down: flattening all positions...")
    account = get_account(headers)
    for symbol, pos in account["positions"].items():
        if pos["qty"] != 0:
            side = "sell" if pos["qty"] > 0 else "buy"
            submit_market_order(symbol, side, abs(pos["qty"]), headers)
    print("flattened.")

##
## Main Event Loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = login(Config.ACCOUNT_ID, Config.PASSWORD)
    headers = {"X-API-Key": api_key}
    print(f"logged in as {Config.ACCOUNT_ID}")

    try:
        while True:
            tick_start = time.monotonic()
            try:
                matrix = fetch_futures_matrix()
            except Exception as e:
                print(f"futures_matrix fetch failed ({e}), retrying next tick")
                time.sleep(Config.POLL_INTERVAL)
                continue

            account = get_account(headers)
            triples = list(live_triples(matrix))

            for near_symbol, far_symbol, cal_symbol, near_theo, far_theo, cal_theo in triples:
                near_book_f = POOL.submit(get_book, near_symbol, headers)
                far_book_f = POOL.submit(get_book, far_symbol, headers)
                cal_book_f = POOL.submit(get_book, cal_symbol, headers)
                near_book, far_book, cal_book = near_book_f.result(), far_book_f.result(), cal_book_f.result()

                near_max_position = max_position_for(account["cash"], Config.FUTURES_LEVERAGE, near_theo)
                far_max_position = max_position_for(account["cash"], Config.FUTURES_LEVERAGE, far_theo)
                cal_max_position = max_position_for(account["cash"], Config.FUTURES_LEVERAGE, cal_theo)

                plan = plan_combo(
                    near_symbol, far_symbol, cal_symbol, near_book, far_book, cal_book, account,
                    near_max_position, far_max_position, cal_max_position,
                )
                if plan is None:
                    continue

                direction, qty, profit, legs = plan
                futures = [POOL.submit(submit_market_order, symbol, side, qty, headers) for symbol, side, price in legs]
                results = [f.result() for f in futures]
                fully_filled = all(
                    r is not None and r["status"] == "filled" and r["remaining_qty"] == 0
                    for r in results
                )
                if fully_filled:
                    print(f"{datetime.now(timezone.utc).isoformat()}  {direction}  {cal_symbol}  qty={qty}  "
                          f"locked_edge=${profit:.2f}/unit  " +
                          "; ".join(f"{side} {qty}x {symbol} (measured @ {price})" for symbol, side, price in legs))
                    account = get_account(headers)  # positions changed — refresh before the next triple's headroom check
                else:
                    problems = [
                        f"{symbol}: {'rejected' if r is None else r['status'] + ' remaining=' + str(r['remaining_qty'])}"
                        for (symbol, _, _), r in zip(legs, results)
                        if r is None or r["status"] != "filled" or r["remaining_qty"] != 0
                    ]
                    print(f"PARTIAL COMBO FILL on {cal_symbol} - {problems} - now carrying real exposure until cleaned up")
                    account = get_account(headers)

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_INTERVAL - elapsed))

    except ShutdownRequested:
        flatten(headers)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
