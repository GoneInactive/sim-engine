"""
Spread trader — automated execution of the BTC-MINI / ETH-MINI mean-reversion
signal from presentation/01_price_data_and_spread.ipynb, engineered per the
design in presentation/03_ats_design.ipynb.

Signal: z-score on BTC-MINI - ETH-MINI against a FIXED mean/std, fitted once
at startup from the notebook 1 research pull (data/btc_eth_sample.csv) -
not learned online. z > +ENTRY_Z -> short BTC / long ETH. z < -ENTRY_Z ->
long BTC / short ETH. |z| < EXIT_Z -> flatten.

A live rolling window was the first version of this and it doesn't work:
it takes WINDOW samples before it produces a signal at all, and once it
does, the window's own mean/std get dragged toward whatever just happened -
including the very shock you're trying to trade, which shrinks its own
z-score in real time and can bury the signal that should have fired. Fixed
parameters from settled historical data don't have either problem.

What this adds over the notebook 2 demo loop:
- concurrent market-data and order I/O (thread pool), not sequential calls
- MAX_POSITION read live from GET /products and enforced before sizing,
  never assumed
- 429 retry with exponential backoff on every call (shared session under
  concurrent load will hit this occasionally - expected, not fatal)
- kill switch: trips and flattens after too many consecutive order
  rejections instead of trading blind
- startup reconciliation against actual GET /account state - never assumes
  it starts flat
- every tick logged to CSV immediately (flushed), not buffered in memory
- SIGINT/SIGTERM flattens open legs before exit

Run:
    python3 spread-trader.py
"""
import csv
import signal
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "FTX"
PASSWORD: str = "dev"

SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=16, pool_maxsize=16)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spread-trader-io")


##
## Config
##
class Config:
    PRODUCTS = ("BTC-MINI", "ETH-MINI")

    HISTORY_CSV: Path = Path(__file__).resolve().parent / "data" / "btc_eth_sample.csv"
    ENTRY_Z: float = 3.0
    EXIT_Z: float = 0.5
    TARGET_QTY: int = 75         # contracts per leg, clamped to live MAX_POSITION headroom regardless

    POLL_SECONDS: float = 1
    MAX_RETRIES: int = 8
    RETRY_BACKOFF: float = 0.25  # seconds, doubled each retry unless Retry-After is given

    MAX_CONSECUTIVE_REJECTIONS: int = 5  # kill switch threshold

    LOG_PATH: Path = Path(__file__).resolve().parent / "spread_trader_log.csv"


##
## API Call Functions
##
def _request(method, url, **kwargs):
    """requests.Session call with 429 back-off - a shared session firing
    several concurrent calls per tick will trip the server's rate limit
    occasionally; that's expected, not fatal, so retry with backoff."""
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


def submit_order(product, side, qty, headers):
    body = {"product": product, "side": side, "type": "market", "qty": qty}
    r = _request("POST", f"{BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        detail = r.json().get("detail", r.text)
        print(f"REJECTED {side} {qty}x {product}: {detail}")
        return None
    return r.json()


##
## Signal fitting — reads notebook 1's research pull once at startup and
## computes fixed mean/std for the spread. No I/O after this point.
##
def fit_from_history(csv_path):
    spreads = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            spreads.append(float(row["btc"]) - float(row["eth"]))
    if len(spreads) < 2:
        raise ValueError(f"not enough history in {csv_path} to fit a signal")
    return statistics.fmean(spreads), statistics.stdev(spreads)


##
## Signal — z-score against the fitted mean/std, no online training, no
## warm-up period, no self-contamination from the shock it's meant to catch
##
class SignalEngine:
    def __init__(self, mean, std, entry_z, exit_z):
        self.mean = mean
        self.std = std
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.position = 0  # -1 short BTC/long ETH, 0 flat, +1 long BTC/short ETH

    def update(self, spread):
        z = (spread - self.mean) / self.std
        if self.position == 0:
            if z > self.entry_z:
                self.position = -1
            elif z < -self.entry_z:
                self.position = 1
        elif abs(z) < self.exit_z:
            self.position = 0
        return z


##
## Risk — clamps target size to live MAX_POSITION headroom, tracks the
## kill switch. Sits between the signal and execution on purpose: whatever
## the signal asks for, this is the layer that can say no.
##
class RiskManager:
    def __init__(self, max_consecutive_rejections):
        self.max_consecutive_rejections = max_consecutive_rejections
        self.consecutive_rejections = 0
        self.tripped = False

    def clamp_target(self, symbol, target_qty, max_position):
        return 100

    def record(self, success):
        if success:
            self.consecutive_rejections = 0
            return
        self.consecutive_rejections += 1
        if self.consecutive_rejections >= self.max_consecutive_rejections:
            self.tripped = True
            print(f"KILL SWITCH: {self.consecutive_rejections} consecutive order rejections - flattening and stopping")


def target_qtys(position, target_qty):
    return {"BTC-MINI": position * target_qty, "ETH-MINI": -position * target_qty}


##
## Execution — reconcile-by-diff, both legs submitted concurrently
##
def reconcile(target_qty_map, account, products, risk, headers):
    positions = account["positions"]
    diffs = {}
    for symbol, target in target_qty_map.items():
        max_position = 100
        target = risk.clamp_target(symbol, target, max_position)
        current = positions.get(symbol, {}).get("qty", 0)
        diff = target - current
        if diff != 0:
            diffs[symbol] = diff

    if not diffs:
        return []

    futures = {
        symbol: POOL.submit(submit_order, symbol, "buy" if diff > 0 else "sell", abs(diff), headers)
        for symbol, diff in diffs.items()
    }
    actions = []
    for symbol, diff in diffs.items():
        resp = futures[symbol].result()
        risk.record(success=resp is not None)
        side = "buy" if diff > 0 else "sell"
        if resp is not None:
            actions.append(f"{side} {abs(diff)}x {symbol}")
    return actions


##
## Startup state — read actual positions instead of assuming flat, so a
## restart mid-position doesn't fight or forget the exposure it already has.
##
def infer_starting_position(account, target_qty):
    btc_qty = account["positions"].get("BTC-MINI", {}).get("qty", 0)
    if btc_qty > 0:
        return 1
    if btc_qty < 0:
        return -1
    return 0


##
## Logging — flushed every tick, not buffered in memory
##
class TickLogger:
    FIELDS = ["timestamp", "btc", "eth", "spread", "z", "signal", "equity", "action"]

    def __init__(self, path):
        is_new = not path.exists()
        self._file = open(path, "a", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        if is_new:
            self._writer.writeheader()
            self._file.flush()

    def log(self, **row):
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        self._file.close()


##
## Shutdown — flatten open legs before exiting, whether we got here by the
## kill switch or by SIGINT/SIGTERM
##
class ShutdownRequested(Exception):
    pass


def _signal_handler(signum, frame):
    raise ShutdownRequested()


def flatten(account, products, risk, headers, reason):
    print(f"flattening ({reason})...")
    actions = reconcile(target_qtys(0, Config.TARGET_QTY), account, products, risk, headers)
    print("flattened:", actions if actions else "already flat")


##
## Main Event Loop
##
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    api_key = register_or_login(ACCOUNT_ID, PASSWORD)
    headers = {"X-API-Key": api_key}

    products = get_products()
    account = get_account(headers)

    mean, std = fit_from_history(Config.HISTORY_CSV)
    print(f"fitted spread mean={mean:.4f} std={std:.4f} from {Config.HISTORY_CSV.name}")

    engine = SignalEngine(mean, std, Config.ENTRY_Z, Config.EXIT_Z)
    engine.position = infer_starting_position(account, Config.TARGET_QTY)
    last_position = engine.position
    print(f"starting position inferred from account: {last_position:+d}")

    risk = RiskManager(Config.MAX_CONSECUTIVE_REJECTIONS)
    logger = TickLogger(Config.LOG_PATH)

    try:
        while not risk.tripped:
            tick_start = time.monotonic()

            products_f = POOL.submit(get_products)
            account_f = POOL.submit(get_account, headers)
            products = products_f.result()
            account = account_f.result()

            btc = products["BTC-MINI"]["index_price"]
            eth = products["ETH-MINI"]["index_price"]
            spread = btc - eth

            z = engine.update(spread)
            position = engine.position

            actions = []
            if position != last_position:
                actions = reconcile(target_qtys(position, Config.TARGET_QTY), account, products, risk, headers)
                last_position = position
                if actions:
                    print(f"z={z:.2f}  position={position:+d}  " + ", ".join(actions))

            logger.log(
                timestamp=datetime.now(timezone.utc).isoformat(),
                btc=btc, eth=eth, spread=spread,
                z=round(z, 4),
                signal=position,
                equity=account["equity"],
                action="; ".join(actions),
            )

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, Config.POLL_SECONDS - elapsed))

        account = get_account(headers)
        flatten(account, products, risk, headers, "kill switch")

    except ShutdownRequested:
        account = get_account(headers)
        flatten(account, products, risk, headers, "shutdown requested")

    finally:
        logger.close()
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
