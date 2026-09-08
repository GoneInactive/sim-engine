"""
Options market maker pool.

Runs N independent market makers on the 15-minute BTC options chain at
once, each its own account, each in its own thread. Every maker quotes
the same Black-Scholes theo (src/exchange/options.py:bs_price, r=0) but at
a different, progressively wider spread tier:

  maker 0: tightest allowed spread, right at the touch
  maker 1: one tick wider
  maker i: i ticks wider than maker 0

That builds real layered depth in the book - several distinct price
levels with resting size - instead of N bots all fighting over the exact
same best price (where only price-time priority, not real depth, would
separate them). Each maker manages its own inventory, its own skew, and
its own portfolio delta hedge on BTC-MINI independently; nothing is
shared between them except the theo they're all quoting around and the
chain-discovery/cooldown bookkeeping.

Rate limiting is per API key (config.yaml rate_limit, enforced per
X-API-Key), not global - N separate accounts each get their own 20 req/s
budget, so running many makers concurrently doesn't require coordinating
a shared request budget the way a single account managing many symbols
does (see options_mm.py's MAX_REQUOTES_PER_TICK comment for that
narrower problem). Each maker still caps its own per-tick requotes for
the same reason options_mm.py does: its own chain has ~20+ symbols x 2
sides, and even one account's own churn can trip its own limit.

This is deliberately a separate file from options_mm.py rather than a
parameter on it - options_mm.py stays a single, directly-runnable bot you
can read top to bottom; this one is explicitly about running several of
that same shape at once.
"""
import math
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_PREFIX: str = "optionsmm_pool"
PASSWORD: str = "cuquants"

UNDERLYING: str = "BTC-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=32, pool_maxsize=32)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="options-mm-pool-io")

SECONDS_PER_YEAR = 365.0 * 24 * 3600

##
## Config — shared chain parameters must match config.yaml's options:
## block (not discoverable via the public API - see docs/API.md).
## Per-maker tiering is derived below, not hardcoded per maker.
##
class Config:
    WINDOW_SECONDS: float = 900.0
    STRIKES_EACH_SIDE: int = 5
    STRIKE_INCREMENT: float = 1.0
    IMPLIED_VOL: float = 0.55
    TICK_SIZE: float = 0.01
    MAX_POSITION: int = 15

    N_MAKERS: int = 5                 # how many independent MM accounts to run
    BASE_HALF_SPREAD_TICKS: int = 1   # maker 0's half-spread, in ticks
    TICKS_PER_TIER: int = 1           # each subsequent maker sits this many extra ticks out
    SPREAD_FRAC: float = 0.015        # half-spread as a fraction of theo, on top of the tier floor

    SKEW_SENSITIVITY: float = 0.5     # ticks of quote-shift per contract of inventory
    QUOTE_SIZE: int = 3               # contracts per side, per symbol, per maker
    REQUOTE_EPS_TICKS: int = 2        # only cancel/replace once target moves >= this many ticks

    MAX_REQUOTES_PER_TICK: int = 6    # per maker, per tick - see module docstring
    PORTFOLIO_DELTA_HEDGE_BAND: float = 3.0
    MIN_SECONDS_TO_EXPIRY: float = 20.0
    DEAD_SYMBOL_COOLDOWN: float = 20.0

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
## Black-Scholes — identical to src/exchange/options.py's bs_price (r=0).
## Shared by every maker - they're all pricing off the same fair value.
##
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_price(spot, strike, t_years, vol, option_type):
    if t_years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    if option_type == "call":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

def bs_delta(spot, strike, t_years, vol, option_type):
    if t_years <= 0 or vol <= 0:
        itm = spot > strike if option_type == "call" else spot < strike
        return (1.0 if option_type == "call" else -1.0) if itm else 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * sqrt_t)
    return _norm_cdf(d1) if option_type == "call" else _norm_cdf(d1) - 1.0

def next_boundary(now, window_seconds):
    return (math.floor(now / window_seconds) + 1) * window_seconds

def round_to_tick(price, tick):
    return round(round(price / tick) * tick, 2)

def symbol_for(expiry_ts, strike, option_type):
    expiry_label = time.strftime("%H%M", time.gmtime(expiry_ts))
    suffix = "C" if option_type == "call" else "P"
    return f"BTC-{expiry_label}-{strike:.2f}{suffix}"

def candidate_chain(spot, expiry_ts):
    atm = round(spot / Config.STRIKE_INCREMENT) * Config.STRIKE_INCREMENT
    out = []
    for i in range(-Config.STRIKES_EACH_SIDE, Config.STRIKES_EACH_SIDE + 1):
        strike = atm + i * Config.STRIKE_INCREMENT
        if strike <= 0:
            continue
        for option_type in ("call", "put"):
            out.append((symbol_for(expiry_ts, strike, option_type), strike, option_type))
    return out

##
## Per-maker quoting — half_spread_ticks is this maker's tier: how many
## ticks wider than theo its quotes sit, on top of the % of theo everyone
## adds. Maker 0's tier ticks == BASE_HALF_SPREAD_TICKS (the touch);
## higher makers sit further out, building depth beyond the best price.
##
def desired_quotes(theo, position_qty, tier_extra_ticks):
    # base_half_spread is what maker 0 (tier_extra_ticks=0) quotes at -
    # theo-proportional, floored at BASE_HALF_SPREAD_TICKS. Every other
    # maker ADDS its tier's extra ticks on top of that, so tiers stay
    # separated by a real, constant tick gap regardless of theo's size -
    # a max() between the two (the original version of this) collapses
    # every tier to the same price the instant theo*SPREAD_FRAC exceeds
    # the tier tick floor, which it does for anything but deep-OTM.
    base_half_spread = max(Config.BASE_HALF_SPREAD_TICKS * Config.TICK_SIZE, theo * Config.SPREAD_FRAC)
    half_spread = base_half_spread + tier_extra_ticks * Config.TICK_SIZE
    shift = -position_qty * Config.SKEW_SENSITIVITY * Config.TICK_SIZE
    bid = round_to_tick(max(Config.TICK_SIZE, theo - half_spread + shift), Config.TICK_SIZE)
    ask = round_to_tick(max(bid + Config.TICK_SIZE, theo + half_spread + shift), Config.TICK_SIZE)
    return bid, ask

def plan_symbol(symbol, strike, option_type, spot, t_years, position_qty, open_by_symbol, tier_extra_ticks):
    theo = bs_price(spot, strike, t_years, Config.IMPLIED_VOL, option_type)
    target_bid, target_ask = desired_quotes(theo, position_qty, tier_extra_ticks)

    buy_headroom = Config.MAX_POSITION - position_qty
    sell_headroom = Config.MAX_POSITION + position_qty
    target_bid_qty = min(Config.QUOTE_SIZE, max(0, buy_headroom))
    target_ask_qty = min(Config.QUOTE_SIZE, max(0, sell_headroom))

    resting = open_by_symbol.get(symbol, {"buy": None, "sell": None})
    eps = Config.REQUOTE_EPS_TICKS * Config.TICK_SIZE
    plans = []

    for side, target_price, target_qty in (("buy", target_bid, target_bid_qty), ("sell", target_ask, target_ask_qty)):
        current = resting.get(side)
        current_price = current["price"] if current is not None else None
        needs_replace = (
            (current is None and target_qty > 0)
            or (current is not None and target_qty <= 0)
            or (current is not None and target_qty > 0 and current_price is None)
            or (current is not None and target_qty > 0 and current_price is not None and abs(current_price - target_price) >= eps)
        )
        if needs_replace:
            plans.append((side, target_price, target_qty, current))
    return theo, plans

def execute_requote(symbol, side, target_price, target_qty, current, headers):
    if current is not None:
        cancel_order(current["id"], headers)
    if target_qty <= 0:
        return None, False
    order, reason = submit_order(symbol, side, target_qty, headers, target_price)
    if order is not None:
        return f"{side} {target_qty}x {symbol} @ {target_price}", False
    if reason == f"unknown product {symbol}":
        return None, True
    return f"{side} {symbol} REJECTED: {reason}", False

##
## Portfolio delta hedge
##
def portfolio_delta(account, chain, spot, t_years):
    net = 0.0
    for symbol, strike, option_type in chain:
        qty = account["positions"].get(symbol, {}).get("qty", 0)
        if qty:
            net += qty * bs_delta(spot, strike, t_years, Config.IMPLIED_VOL, option_type)
    return net

def hedge_if_needed(account, chain, spot, t_years, headers):
    net_delta = portfolio_delta(account, chain, spot, t_years)
    target_hedge_qty = -round(net_delta)
    current_hedge_qty = account["positions"].get(UNDERLYING, {}).get("qty", 0)
    diff = target_hedge_qty - current_hedge_qty
    if abs(diff) < Config.PORTFOLIO_DELTA_HEDGE_BAND:
        return net_delta, None
    side = "buy" if diff > 0 else "sell"
    resp = submit_market_order(UNDERLYING, side, abs(diff), headers)
    return net_delta, (f"{side} {abs(diff)}x {UNDERLYING} (portfolio delta hedge)" if resp is not None else None)

def flatten_maker(headers, known_symbols, label):
    print(f"[{label}] shutting down: cancelling all quotes and flattening...")
    orders = get_orders(headers)
    open_orders = [o for o in orders if o["status"] in ("open", "partially_filled")]
    for f in [POOL.submit(cancel_order, o["id"], headers) for o in open_orders]:
        f.result()

    account = get_account(headers)
    for symbol, pos in list(account["positions"].items()):
        if pos["qty"] == 0 or (symbol != UNDERLYING and symbol not in known_symbols):
            continue
        side = "sell" if pos["qty"] > 0 else "buy"
        submit_market_order(symbol, side, abs(pos["qty"]), headers)
    print(f"[{label}] flattened.")

##
## One maker's event loop - runs in its own thread, its own account.
##
def run_maker(maker_index, stop_event):
    label = f"maker{maker_index}"
    account_id = f"{ACCOUNT_PREFIX}_{maker_index}"
    tier_extra_ticks = maker_index * Config.TICKS_PER_TIER

    api_key = register_or_login(account_id, PASSWORD)
    headers = {"X-API-Key": api_key}
    print(f"[{label}] connected as {account_id}, tier=+{tier_extra_ticks} ticks beyond the base spread")

    dead_until = {}
    current_expiry = None
    known_symbols = set()

    try:
        while not stop_event.is_set():
            tick_start = time.monotonic()
            now = time.time()

            products = get_products()
            spot = products[UNDERLYING]["index_price"]
            expiry_ts = next_boundary(now, Config.WINDOW_SECONDS)
            t_years = max(0.0, expiry_ts - now) / SECONDS_PER_YEAR
            seconds_to_expiry = expiry_ts - now

            if expiry_ts != current_expiry:
                dead_until = {}
                current_expiry = expiry_ts

            account = get_account(headers)
            open_orders = [o for o in get_orders(headers) if o["status"] in ("open", "partially_filled")]
            open_by_symbol = {}
            for o in open_orders:
                open_by_symbol.setdefault(o["product"], {"buy": None, "sell": None})[o["side"]] = o

            if seconds_to_expiry <= Config.MIN_SECONDS_TO_EXPIRY:
                for f in [POOL.submit(cancel_order, o["id"], headers) for o in open_orders]:
                    f.result()
                time.sleep(max(0.0, Config.POLL_SECONDS - (time.monotonic() - tick_start)))
                continue

            chain = candidate_chain(spot, expiry_ts)
            live_chain = [c for c in chain if dead_until.get(c[0], 0) <= time.monotonic()]
            known_symbols = {c[0] for c in chain}

            requote_queue = []
            for symbol, strike, option_type in live_chain:
                position_qty = account["positions"].get(symbol, {}).get("qty", 0)
                theo, plans = plan_symbol(symbol, strike, option_type, spot, t_years, position_qty, open_by_symbol, tier_extra_ticks)
                for side, target_price, target_qty, current in plans:
                    requote_queue.append((symbol, side, target_price, target_qty, current))

            batch = requote_queue[:Config.MAX_REQUOTES_PER_TICK]
            futures = [POOL.submit(execute_requote, symbol, side, price, qty, current, headers)
                       for symbol, side, price, qty, current in batch]

            all_actions = []
            newly_dead = set()
            for (symbol, side, price, qty, current), f in zip(batch, futures):
                action, is_unknown = f.result()
                if action:
                    all_actions.append(action)
                if is_unknown:
                    newly_dead.add(symbol)
            for symbol in newly_dead:
                dead_until[symbol] = time.monotonic() + Config.DEAD_SYMBOL_COOLDOWN

            net_delta, hedge_action = hedge_if_needed(account, chain, spot, t_years, headers)
            if hedge_action:
                all_actions.append(hedge_action)

            if all_actions:
                print(f"[{label}] {datetime.now(timezone.utc).isoformat()}  spot={spot:.2f}  "
                      f"net_delta={net_delta:.2f}  " + "; ".join(all_actions))

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

    finally:
        flatten_maker(headers, known_symbols, label)

##
## Main — spins up N_MAKERS threads, waits for Ctrl+C, signals every
## thread to stop (each flattens its own account on the way out).
##
def main():
    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        print("\nstop requested - waiting for every maker to flatten...")
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    threads = [
        threading.Thread(target=run_maker, args=(i, stop_event), daemon=False, name=f"maker-{i}")
        for i in range(Config.N_MAKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    POOL.shutdown(wait=False)
    print("all makers stopped.")


if __name__ == "__main__":
    main()
