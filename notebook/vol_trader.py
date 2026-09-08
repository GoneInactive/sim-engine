"""
Vol trading bot.

Every 15-minute BTC option is priced by the exchange off ONE fixed
assumption: IMPLIED_VOL (config.yaml options.implied_volatility, currently
0.55). That number doesn't move tick to tick - but BTC-MINI's actual
realized volatility does. When the underlying is moving a lot more than
0.55 annualized, the whole chain is underpriced (cheap gamma) and worth
owning; when it's moving a lot less, the chain is overpriced and worth
selling. This is the classic "trade realized vs implied" vol strategy,
delta-hedged so the bet is on volatility itself, not on direction:

  realized_vol > IMPLIED_VOL + ENTRY_EDGE  -> long vol: buy an ATM straddle
  realized_vol < IMPLIED_VOL - ENTRY_EDGE  -> short vol: sell an ATM straddle
  |realized_vol - IMPLIED_VOL| < EXIT_EDGE -> flatten (same hysteresis
                                               shape as the mean-reversion
                                               signal in the presentation
                                               notebooks - avoid flip-flops
                                               right at the threshold)

Delta hedging: a straddle's net delta isn't zero except exactly at expiry
with spot == strike, so every tick the bot computes the position's net
Black-Scholes delta and trades BTC-MINI to cancel it out, keeping the P&L
driven by realized-vs-implied vol rather than by which way BTC happened to
drift. bs_price/bs_delta below are the exact same formula the exchange
itself uses (src/exchange/options.py:bs_price, r=0) - not a reimplementation
guess, so theo/delta here matches what the matching engine is actually
quoting against.

The public API has no endpoint listing option symbols/strikes (GET
/products only lists the two spot products - see docs/API.md) - a real
bot has to derive them itself from IMPLIED_VOL's sibling config
(WINDOW_SECONDS/STRIKES_EACH_SIDE/STRIKE_INCREMENT, hardcoded below to
match config.yaml) and the same symbol format the server uses
(OptionsChainManager._symbol), then confirm each guess is actually live via
GET /book/{symbol} (a 404 means that strike/window combination isn't
currently trading).
"""
import math
import signal
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "daytek"
PASSWORD: str = "poop"

UNDERLYING: str = "BTC-MINI"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=8)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vol-trader-io")

SECONDS_PER_YEAR = 365.0 * 24 * 3600

##
## Config — the options.* values must match config.yaml's options: block;
## they aren't discoverable via the public API (see module docstring).
##
class Config:
    WINDOW_SECONDS: float = 900.0
    STRIKES_EACH_SIDE: int = 5
    STRIKE_INCREMENT: float = 1.0
    IMPLIED_VOL: float = 0.55

    REALIZED_VOL_SAMPLES: int = 120   # 2 min of 1s polling before trading starts
    ENTRY_EDGE: float = 0.10          # vol points clear of IMPLIED_VOL to open a position
    EXIT_EDGE: float = 0.03           # vol points inside IMPLIED_VOL to flatten
    MIN_SECONDS_TO_EXPIRY: float = 45.0  # stop opening/adjusting this close to expiry (pin risk)

    STRADDLE_QTY: int = 2             # contracts per leg (call + put)
    DELTA_HEDGE_BAND: float = 1.0     # only re-hedge once net delta drifts past this many BTC-MINI contracts

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

def get_book(product, headers):
    r = _request("GET", f"{BASE_URL}/book/{product}", headers=headers)
    if r.status_code == 404:
        return None  # not currently live - normal while probing candidate strikes
    r.raise_for_status()
    return r.json()

def get_account(headers):
    r = _request("GET", f"{BASE_URL}/account", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, qty, headers, price=None):
    body = {"product": product, "side": side, "type": "market" if price is None else "limit", "qty": qty}
    if price is not None:
        body["price"] = price
    r = _request("POST", f"{BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        detail = r.json().get("detail", r.text)
        print(f"REJECTED {side} {qty}x {product}: {detail}")
        return None
    return r.json()

##
## Black-Scholes — identical to src/exchange/options.py's bs_price (r=0),
## plus delta, which the engine computes internally but never exposes.
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

def symbol_for(expiry_ts, strike, option_type):
    expiry_label = time.strftime("%H%M", time.gmtime(expiry_ts))
    suffix = "C" if option_type == "call" else "P"
    return f"BTC-{expiry_label}-{strike:.2f}{suffix}"

##
## Chain discovery — derive candidate symbols client-side, confirm each is
## actually live via GET /book (see module docstring).
##
def discover_atm_pair(spot, expiry_ts, headers):
    """The live call+put at the strike closest to spot, or None if neither
    resolves (e.g. right at a window roll)."""
    atm = round(spot / Config.STRIKE_INCREMENT) * Config.STRIKE_INCREMENT
    offsets = sorted(range(-Config.STRIKES_EACH_SIDE, Config.STRIKES_EACH_SIDE + 1), key=abs)
    for i in offsets:
        strike = atm + i * Config.STRIKE_INCREMENT
        if strike <= 0:
            continue
        call_symbol = symbol_for(expiry_ts, strike, "call")
        put_symbol = symbol_for(expiry_ts, strike, "put")
        call_book_f = POOL.submit(get_book, call_symbol, headers)
        put_book_f = POOL.submit(get_book, put_symbol, headers)
        call_book, put_book = call_book_f.result(), put_book_f.result()
        if call_book is not None and put_book is not None:
            return {
                "strike": strike,
                "call_symbol": call_symbol, "put_symbol": put_symbol,
                "call_book": call_book, "put_book": put_book,
            }
    return None

##
## Realized vol — rolling log-return std of the underlying's own index
## price, annualized. Same rolling-window mechanics as SpreadWindow in
## presentation/02_signal_to_execution.ipynb.
##
class RealizedVolEstimator:
    def __init__(self, max_samples):
        self.log_returns = deque(maxlen=max_samples)
        self._last_price = None

    def update(self, price):
        if self._last_price is not None and self._last_price > 0 and price > 0:
            self.log_returns.append(math.log(price / self._last_price))
        self._last_price = price

    def value(self, poll_seconds):
        if len(self.log_returns) < self.log_returns.maxlen:
            return None
        samples_per_year = SECONDS_PER_YEAR / poll_seconds
        return statistics.pstdev(self.log_returns) * math.sqrt(samples_per_year)

##
## Position state machine — same hysteresis shape as SignalEngine in
## presentation/02_signal_to_execution.ipynb, applied to vol instead of spread.
##
class VolSignal:
    def __init__(self, implied_vol, entry_edge, exit_edge):
        self.implied_vol = implied_vol
        self.entry_edge = entry_edge
        self.exit_edge = exit_edge
        self.position = 0  # -1 short vol, 0 flat, +1 long vol

    def update(self, realized_vol):
        diff = realized_vol - self.implied_vol
        if self.position == 0:
            if diff > self.entry_edge:
                self.position = 1
            elif diff < -self.entry_edge:
                self.position = -1
        elif abs(diff) < self.exit_edge:
            self.position = 0
        return self.position

##
## Execution
##
def straddle_targets(position, qty):
    return position * qty  # same qty, same sign, on both call and put

def reconcile_straddle(atm, target_qty, account, headers):
    actions = []
    for symbol in (atm["call_symbol"], atm["put_symbol"]):
        current = account["positions"].get(symbol, {}).get("qty", 0)
        diff = target_qty - current
        if diff == 0:
            continue
        side = "buy" if diff > 0 else "sell"
        resp = submit_order(symbol, side, abs(diff), headers)
        if resp is not None:
            actions.append(f"{side} {abs(diff)}x {symbol}")
    return actions

def reconcile_hedge(target_qty, account, headers):
    current = account["positions"].get(UNDERLYING, {}).get("qty", 0)
    diff = target_qty - current
    if diff == 0:
        return []
    side = "buy" if diff > 0 else "sell"
    resp = submit_order(UNDERLYING, side, abs(diff), headers)
    return [f"{side} {abs(diff)}x {UNDERLYING} (hedge)"] if resp is not None else []

##
## Shutdown — flatten straddle + hedge before exiting
##
class ShutdownRequested(Exception):
    pass

def _signal_handler(signum, frame):
    raise ShutdownRequested()

def flatten(atm, headers):
    account = get_account(headers)
    actions = []
    if atm is not None:
        actions += reconcile_straddle(atm, 0, account, headers)
        account = get_account(headers)
    actions += reconcile_hedge(0, account, headers)
    print("flattened:", actions if actions else "already flat")

##
## Main Event Loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = register_or_login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    vol_estimator = RealizedVolEstimator(Config.REALIZED_VOL_SAMPLES)
    vol_signal = VolSignal(Config.IMPLIED_VOL, Config.ENTRY_EDGE, Config.EXIT_EDGE)
    last_position = 0
    current_atm = None  # the strike/symbols we're currently positioned in, if any

    try:
        while True:
            tick_start = time.monotonic()
            now = time.time()

            products = get_products()
            spot = products[UNDERLYING]["index_price"]
            vol_estimator.update(spot)
            realized_vol = vol_estimator.value(Config.POLL_SECONDS)

            expiry_ts = next_boundary(now, Config.WINDOW_SECONDS)
            t_years = max(0.0, expiry_ts - now) / SECONDS_PER_YEAR
            seconds_to_expiry = expiry_ts - now

            if realized_vol is not None and seconds_to_expiry > Config.MIN_SECONDS_TO_EXPIRY:
                position = vol_signal.update(realized_vol)
            else:
                position = 0  # not enough data yet, or too close to expiry - stay flat

            if position != 0 and current_atm is None:
                current_atm = discover_atm_pair(spot, expiry_ts, headers)
                if current_atm is None:
                    position = 0  # chain not live right now - can't act on the signal

            account = get_account(headers)
            actions = []

            if position != last_position:
                if position == 0 and current_atm is not None:
                    actions += reconcile_straddle(current_atm, 0, account, headers)
                    account = get_account(headers)
                    actions += reconcile_hedge(0, account, headers)
                    current_atm = None
                elif position != 0 and current_atm is not None:
                    actions += reconcile_straddle(current_atm, straddle_targets(position, Config.STRADDLE_QTY), account, headers)
                last_position = position
                if actions:
                    print(f"{datetime.now(timezone.utc).isoformat()}  realized={realized_vol}  "
                          f"implied={Config.IMPLIED_VOL}  position={position:+d}  " + ", ".join(actions))

            # Delta hedge - re-checked every tick while a straddle is open,
            # not just on a position change, since delta drifts with spot
            # even while the straddle itself is untouched.
            if current_atm is not None and position != 0:
                account = get_account(headers)
                qty = position * Config.STRADDLE_QTY
                call_delta = bs_delta(spot, current_atm["strike"], t_years, Config.IMPLIED_VOL, "call")
                put_delta = bs_delta(spot, current_atm["strike"], t_years, Config.IMPLIED_VOL, "put")
                net_option_delta = qty * (call_delta + put_delta)
                target_hedge_qty = -round(net_option_delta)
                current_hedge_qty = account["positions"].get(UNDERLYING, {}).get("qty", 0)
                if abs(target_hedge_qty - current_hedge_qty) >= Config.DELTA_HEDGE_BAND:
                    hedge_actions = reconcile_hedge(target_hedge_qty, account, headers)
                    if hedge_actions:
                        print(f"delta hedge: net_delta={net_option_delta:.2f}  " + ", ".join(hedge_actions))

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

    except ShutdownRequested:
        flatten(current_atm, headers)
    finally:
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
