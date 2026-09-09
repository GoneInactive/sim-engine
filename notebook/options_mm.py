"""
Options market maker.

Quotes both sides of every live contract in the 15-minute BTC chain, tight
around its own Black-Scholes theo (same formula the exchange itself uses -
src/exchange/options.py:bs_price, r=0), sized and skewed like a real
inventory-aware MM:

  half_spread = max(2 ticks, theo * SPREAD_FRAC)   -- never quote inside 2
                                                       ticks; below that the
                                                       maker rebate doesn't
                                                       cover round-trip risk
  bid/ask     = theo (+/-) half_spread, both shifted by an inventory skew
                so a position that's drifted long gets sold off (ask drops,
                more likely to get lifted) and a short position gets bought
                back, instead of just sitting there accumulating

Quotes are only cancelled/replaced when the target price actually moves by
>= REQUOTE_EPS_TICKS - re-submitting an unchanged price every tick would
just burn the rate limit and taker fees against yourself for no reason.

Market making both sides everywhere still builds up net delta over time
(a run of calls getting lifted and puts sitting, say) - the same bs_delta
used by vol_trader.py sums position * delta across the whole chain every
tick, and BTC-MINI is used as a light, infrequent hedge (only rebalanced
past PORTFOLIO_DELTA_HEDGE_BAND) to keep that from becoming a directional
bet. This bot isn't trying to trade a realized-vs-implied vol view like
vol_trader.py - it's flat-vol, pure inventory/spread capture, hedged only
enough to not accumulate uncompensated direction.

Same chain-discovery problem as vol_trader.py applies here (GET /products
doesn't list option symbols - see docs/API.md) - candidate symbols are
derived from the same config the server uses and confirmed live by
actually trying to quote them; a symbol that comes back "unknown product"
is cached as dead for a cooldown instead of retried every tick, so a
still-rolling window doesn't turn into 20+ wasted rejections a second.
"""
import math
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "dummytrader"
PASSWORD: str = "poop"

UNDERLYING: str = "BTC-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=16, pool_maxsize=16)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="options-mm-io")

SECONDS_PER_YEAR = 365.0 * 24 * 3600

##
## Config — the options.* values must match config.yaml's options: block;
## none of them are discoverable via the public API (see docs/API.md).
##
class Config:
    WINDOW_SECONDS: float = 900.0
    STRIKES_EACH_SIDE: int = 15
    STRIKE_INCREMENT: float = 1.0
    IMPLIED_VOL: float = 0.55
    TICK_SIZE: float = 0.01
    MAX_POSITION: int = 55

    SPREAD_FRAC: float = 0.000000025        # half-spread as a fraction of theo
    MIN_HALF_SPREAD_TICKS: int = 1    # ...but never tighter than this many ticks (1 tick = the floor; maker rebate still covers it)
    SKEW_SENSITIVITY: float = 0.0     # ticks of quote-shift per contract of inventory
    QUOTE_SIZE: int = 3               # contracts per side, per symbol
    REQUOTE_EPS_TICKS: int = 2        # only cancel/replace once target moves >= this many ticks

    # A 1-cent tick on a chain this close to the underlying's own jitter
    # means theo crosses a tick almost every second for near-ATM strikes -
    # with ~22 symbols x 2 sides, requoting everything that "needs" it
    # every tick blows straight through the 20 req/s rate limit (each
    # replace is a cancel + a place). Capping how many sides actually get
    # touched per tick keeps this bot well under the limit; anything left
    # over just gets picked up on the next tick; the queue never grows -
    # cancel/replace demand each tick and be worked off, not accumulated.
    # Budget check: GET /products + /account + /orders = 3 calls, plus up
    # to MAX_REQUOTES_PER_TICK*2 (cancel+place) + 1 possible hedge order,
    # all at POLL_SECONDS cadence - 6 keeps worst case (3 + 12 + 1 = 16
    # req/s) comfortably under the 20 req/s sustained limit.
    MAX_REQUOTES_PER_TICK: int = 6

    PORTFOLIO_DELTA_HEDGE_BAND: float = 3.0  # BTC-MINI-equivalent contracts of slack before hedging
    MIN_SECONDS_TO_EXPIRY: float = 20.0      # pull all quotes this close to expiry - pin risk
    DEAD_SYMBOL_COOLDOWN: float = 20.0       # seconds to stop retrying a symbol that 400'd "unknown product"

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
    if not r.ok:
        return None
    return r.json()

def cancel_order(order_id, headers):
    r = _request("DELETE", f"{BASE_URL}/orders/{order_id}", headers=headers)
    return r.ok

##
## Black-Scholes — identical to src/exchange/options.py's bs_price (r=0).
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
    """Every (symbol, strike, option_type) the server *should* have live
    for this window - not yet confirmed, see dead-symbol cooldown above."""
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
## Quoting
##
def desired_quotes(theo, position_qty):
    """(bid_price, ask_price) before position-headroom sizing."""
    half_spread = max(Config.MIN_HALF_SPREAD_TICKS * Config.TICK_SIZE, theo * Config.SPREAD_FRAC)
    shift = -position_qty * Config.SKEW_SENSITIVITY * Config.TICK_SIZE
    bid = round_to_tick(max(Config.TICK_SIZE, theo - half_spread + shift), Config.TICK_SIZE)
    ask = round_to_tick(max(bid + Config.TICK_SIZE, theo + half_spread + shift), Config.TICK_SIZE)
    return bid, ask

def plan_symbol(symbol, strike, option_type, spot, t_years, position_qty, open_by_symbol):
    """Pure/no-I/O: decides which of this symbol's two sides need a
    cancel+replace and at what price/qty. Returns (theo, [side_plan, ...])
    where each side_plan is (side, target_price, target_qty, current_order_or_None)."""
    theo = bs_price(spot, strike, t_years, Config.IMPLIED_VOL, option_type)
    target_bid, target_ask = desired_quotes(theo, position_qty)

    buy_headroom = Config.MAX_POSITION - position_qty
    sell_headroom = Config.MAX_POSITION + position_qty
    target_bid_qty = min(Config.QUOTE_SIZE, max(0, buy_headroom))
    target_ask_qty = min(Config.QUOTE_SIZE, max(0, sell_headroom))

    resting = open_by_symbol.get(symbol, {"buy": None, "sell": None})
    eps = Config.REQUOTE_EPS_TICKS * Config.TICK_SIZE
    plans = []

    for side, target_price, target_qty in (("buy", target_bid, target_bid_qty), ("sell", target_ask, target_ask_qty)):
        current = resting.get(side)
        # current["price"] is None for a resting order this bot never
        # placed as a limit (e.g. a market order that didn't find enough
        # liquidity to fully fill and ended up resting anyway, instead of
        # being IOC-dropped - seen in practice on thin far-OTM strikes).
        # Always replace rather than compare against a price that isn't one.
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
    """Runs inside the thread pool - one cancel+place pair, at most, per
    call. Returns (action_str_or_None, is_unknown_product)."""
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

##
## Shutdown — pull every quote and flatten every position, options and hedge
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten_everything(headers, known_symbols):
    print("shutting down: cancelling all quotes and flattening...")
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
    print("flattened.")

##
## Main Event Loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = register_or_login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    dead_until = {}       # symbol -> monotonic time before which we won't retry it
    current_expiry = None
    known_symbols = set()

    try:
        while True:
            tick_start = time.monotonic()
            now = time.time()

            products = get_products()
            spot = products[UNDERLYING]["index_price"]
            expiry_ts = next_boundary(now, Config.WINDOW_SECONDS)
            t_years = max(0.0, expiry_ts - now) / SECONDS_PER_YEAR
            seconds_to_expiry = expiry_ts - now

            if expiry_ts != current_expiry:
                # New window - every candidate is worth trying again even
                # if a stale expiry's symbol was recently marked dead.
                dead_until = {}
                current_expiry = expiry_ts

            account_f = POOL.submit(get_account, headers)
            orders_f = POOL.submit(get_orders, headers)
            account = account_f.result()
            open_orders = [o for o in orders_f.result() if o["status"] in ("open", "partially_filled")]

            open_by_symbol = {}
            for o in open_orders:
                open_by_symbol.setdefault(o["product"], {"buy": None, "sell": None})[o["side"]] = o

            if seconds_to_expiry <= Config.MIN_SECONDS_TO_EXPIRY:
                # Pull everything and wait out the roll rather than quote
                # into settlement.
                for f in [POOL.submit(cancel_order, o["id"], headers) for o in open_orders]:
                    f.result()
                time.sleep(max(0.0, Config.POLL_SECONDS - (time.monotonic() - tick_start)))
                continue

            chain = candidate_chain(spot, expiry_ts)
            live_chain = [c for c in chain if dead_until.get(c[0], 0) <= time.monotonic()]
            known_symbols = {c[0] for c in chain}

            # Decide phase - no I/O, cheap, safe to do for the whole chain
            # every tick regardless of the requote budget below.
            requote_queue = []
            for symbol, strike, option_type in live_chain:
                position_qty = account["positions"].get(symbol, {}).get("qty", 0)
                theo, plans = plan_symbol(symbol, strike, option_type, spot, t_years, position_qty, open_by_symbol)
                for side, target_price, target_qty, current in plans:
                    requote_queue.append((symbol, side, target_price, target_qty, current))

            # Act phase - only the first MAX_REQUOTES_PER_TICK sides get
            # touched this tick; the rest are re-evaluated (and likely
            # still needed) next tick. Bounds this bot's own request rate
            # regardless of how many symbols simultaneously need a requote.
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
                print(f"{datetime.now(timezone.utc).isoformat()}  spot={spot:.2f}  "
                      f"net_delta={net_delta:.2f}  " + "; ".join(all_actions))

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

    except ShutdownRequested:
        flatten_everything(headers, known_symbols)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
