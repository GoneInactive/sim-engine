"""
1. Pricing: get the index price and set that as fair value. +-2 ticks
2. Sizing: 2 lots
"""
import requests
import math

BASE_URL: str = "http://178.105.55.5:8000"
ACCOUNT_ID: str = "knight-capital"
PASSWORD: str = "cuquants"

##
## API Call Functions
##
def login(account_id, password):
    r = requests.post(f"{BASE_URL}/login", json={"account_id": account_id, "password": password})
    r.raise_for_status()
    return r.json()["api_key"]

def get_book(product,headers):
    r = requests.get(f"{BASE_URL}/book/{product}", headers=headers)
    r.raise_for_status()
    return r.json()

def submit_order(product, side, order_type, qty, headers, price=None):
    body = {"product": product, "side": side, "type": order_type, "qty": qty}
    if price is not None:
        body["price"] = price
    r = requests.post(f"{BASE_URL}/orders", headers=headers, json=body)
    if not r.ok:
        print("rejected:", r.json().get("detail"))
        return None
    return r.json()

def cancel_order(order_id,headers):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=headers)
    r.raise_for_status()
    return r.json()

def list_fills(headers):
    r = requests.get(f"{BASE_URL}/fills", headers=headers)
    r.raise_for_status()
    return r.json()

##
## StateClass
##
class State:
    def __init__(self):
        self.our_bid: dict[str,float|int|str] | None = None
        self.our_ask: dict[str,float|int|str] | None = None
        self.fills: list[str] = []

##
## Helpers
##
def batch_cancel(trading_state,headers):
    cancel_order(trading_state.our_bid['id'],headers)
    cancel_order(trading_state.our_ask['id'],headers)

def refresh_quotes(trading_state, headers, reason):
    print(f"REASON: {reason}")
    theo_qty = get_sizes()
    theo_prices = get_quotes(trading_state,headers)
    batch_cancel(trading_state,headers)
    return batch_place(trading_state,headers,theo_qty,theo_prices)

def batch_place(trading_state, headers, theo_qty=None, theo_prices=None):
    if theo_qty is None:
        theo_qty = get_sizes()
    if theo_prices is None:
        theo_prices = get_quotes(trading_state,headers)

    print(f"PLACING: {theo_qty}, {theo_prices}")

    trading_state.our_bid = submit_order(Config.TRADING_PRODUCT, "buy", "limit", theo_qty[0], headers, price=theo_prices[0])
    trading_state.our_ask = submit_order(Config.TRADING_PRODUCT, "sell", "limit", theo_qty[1], headers, price=theo_prices[1])

    return trading_state

##
## Config
##
class Config:
    TRADING_PRODUCT: str = "BTC-MINI"
    DEFAULT_SIZE: int = 3
    MIN_SPREAD: int = 3
    TICK_LEVEL: float = 0.10


##
## Trading Calculations
##
def get_sizes() -> tuple[int,int]:
    return (Config.DEFAULT_SIZE,Config.DEFAULT_SIZE)

def get_quotes(trading_state,headers) -> tuple[float, float]:
    """
    Get the index price and the orderbook
    What's the highest we can bid such that our half-spread >= MIN_SPREAD/2 around the index price
    Similiar for ask

    10.00
    9.00,15.00
    -> 8.00, 14.00
    """
    index_price: float = 0.0

    ##
    ## Get Index Price
    ##
    r = requests.get(f"{BASE_URL}/products")
    for product in r.json():
        if product['symbol'] == Config.TRADING_PRODUCT:
            index_price = round(product['index_price'],1)

    ##
    ## Get and Format OrderBook
    ##
    book = get_book(Config.TRADING_PRODUCT,headers)
    bids = book['bids']
    asks = book['asks']

    bid_bound = index_price - (1/2)*Config.MIN_SPREAD*Config.TICK_LEVEL
    bid_bound = math.floor(bid_bound*10)/10

    ask_bound = index_price + (1/2)*Config.MIN_SPREAD*Config.TICK_LEVEL
    ask_bound = math.ceil(ask_bound*10)/10
    
    theo_bid = bid_bound
    theo_ask = ask_bound

    for bid in bids:
        bid_price = round(bid['price'],1)
        if bid_price <= bid_bound and (trading_state.our_bid is not None and bid_price != trading_state.our_bid['price']):
            theo_bid=bid_price+Config.TICK_LEVEL
            break

    for ask in asks:
        ask_price = round(ask['price'],1)
        if ask_price >= ask_bound and (trading_state.our_ask is not None and ask_price != trading_state.our_ask['price']):
            theo_ask=ask_price-Config.TICK_LEVEL
            break

    return (round(theo_bid,1),round(theo_ask,1))

##
## Main Event Loop
##
def main():
    """
    1. Get recent fills 
    2. Compute theo quotes, we compare to our current orders.
    3. IF different, cancel and re-place
    4. ELSE do nothing
    """
    API_KEY = login(ACCOUNT_ID, PASSWORD)
    HEADERS = {"X-API-Key": API_KEY}

    trading_state: State = State()
    ##
    ## Place init orders
    ##
    batch_place(trading_state,HEADERS)

    while True:
        ##
        ## Get Recent Fills and Update State
        ##
        theo_quotes = get_quotes(trading_state,headers=HEADERS)

        fills = list_fills(HEADERS)[:2]
        for fill in fills:
            if not fill['id'] in trading_state.fills:
                trading_state.fills.append(fill['id'])
                trading_state = refresh_quotes(trading_state,HEADERS,"MISSING FILLS")

        if (trading_state.our_bid['price'] != theo_quotes[0]) or (trading_state.our_ask['price'] != theo_quotes[1]):
            trading_state = refresh_quotes(trading_state,HEADERS,"SUBOPTIMAL PRICING")







if __name__ == "__main__":
    main()