# miniX API Reference

Three separate HTTP services, all part of the same process (`python run.py`
starts all three together):

| Service | Default bind | Purpose | Auth |
|---|---|---|---|
| Public API | `0.0.0.0:8000` | Trading — everything a student/bot uses | `X-API-Key` header (most routes) |
| Admin API | `0.0.0.0:8001` | Running the exchange — accounts, market events, bots | `X-Admin-Password` header or HTTP Basic |
| Website | `0.0.0.0:8090` | Browser UI (ladder, options chain, portfolio) | none (public URLs); trading itself goes browser → Public API directly |

Base URLs (host/port) come from `config/config.yaml`'s `network` section and
can be overridden per-value with `API_PORT`, `ADMIN_API_PORT`,
`WEBSITE_PORT`, `API_HOST`, etc. — see `RUNNING.md`. Everything below
assumes local defaults (`127.0.0.1`); replace the host with your real
server address when trading against a hosted instance.

> **Note:** `RUNNING.md` documents the website as port 8080; the config
> default is actually **8090**. If you can't reach the site on 8080, try
> 8090, or check `config/config.yaml` → `network.website.port` directly.

## Contents

- [Shared data shapes](#shared-data-shapes)
- [Public API](#public-api-port-8000)
- [Admin API](#admin-api-port-8001)
- [Website JSON endpoints](#website-json-endpoints-port-8090)
- [Error reference](#error-reference)
- [Rate limiting](#rate-limiting)
- [Instruments you can't discover via `GET /products`](#instruments-you-cant-discover-via-get-products)

---

## Shared data shapes

These field shapes recur across routes below.

**Order**
```jsonc
{
  "id": 4411,
  "product": "BTC-MINI",
  "side": "buy",            // "buy" | "sell"
  "type": "limit",          // "limit" | "market"
  "qty": 5,
  "price": 77.5,            // null for a market order, or once filled/cancelled it still shows the original limit price (null if it was a market order)
  "remaining_qty": 0,
  "status": "filled"        // "open" | "partially_filled" | "filled" | "cancelled" | "rejected"
}
```

**Fill** (as returned from a per-account endpoint like `GET /fills` — already resolved to *your* side/role)
```jsonc
{
  "id": 812,
  "product": "BTC-MINI",
  "side": "buy",             // your side in this fill
  "role": "taker",           // "maker" | "taker" — determines the fee sign
  "price": 77.54,
  "qty": 5,
  "fee": 0.0775,             // positive = you paid; a maker fee is usually negative (rebate)
  "counterparty": "mm_BTC-MINI_2",
  "timestamp": 1788843600.5
}
```

**Position** (as it appears inside `positions`, keyed by product symbol; only nonzero positions are included)
```jsonc
{ "qty": 5, "avg_cost": 77.54 }
```

**Book snapshot** (`GET /book/{product}` and equivalents) — price levels, best-first, aggregated, depth-capped (10 by default)
```jsonc
{
  "bids": [{"price": 77.4, "qty": 3}, {"price": 77.3, "qty": 4}],
  "asks": [{"price": 77.7, "qty": 3}, {"price": 77.8, "qty": 4}]
}
```

---

## Public API (port 8000)

CORS is enabled on this API for the website's own origin (`allow_methods=["*"]`,
preflight cached 600s) — a browser page can call it directly with
`X-API-Key`, no server-side proxy needed.

### Auth

Every route below marked **auth required** needs the header:

```
X-API-Key: <your key>
```

Get a key from `POST /register` or `POST /login`. An invalid/inactive key
→ `401 {"detail": "invalid or inactive API key"}`. A valid key that's
over its rate limit → `429 {"detail": "rate limit exceeded"}` (see
[Rate limiting](#rate-limiting)).

### `POST /register`

No auth. Self-serve — active immediately, no admin approval step, deposits
`starting_cash` (1000.0 by default).

Request:
```json
{ "account_id": "my_handle", "password": "pick-a-password" }
```

Response `200`:
```json
{ "account_id": "my_handle", "api_key": "GGJXahCRZsJsY4gR3DRnxXNUy_5pR5Sh", "active": true }
```

Response `409` (account_id already taken): `{"detail": "account already registered, use /login"}`

### `POST /login`

No auth. Same credentials as registration; returns the same key every time
(doesn't rotate it).

Request: `{ "account_id": "my_handle", "password": "pick-a-password" }`

Response `200`: `{ "account_id": "my_handle", "api_key": "..." }`

Response `401`: `{"detail": "bad username or password"}`

> **Register-or-login pattern**, used by every bot in `notebook/`:
> ```python
> r = requests.post(f"{BASE_URL}/register", json={"account_id": account_id, "password": password})
> if r.status_code == 409:
>     r = requests.post(f"{BASE_URL}/login", json={"account_id": account_id, "password": password})
> r.raise_for_status()
> api_key = r.json()["api_key"]
> ```
> A plain `login()`-only call, with no register fallback, will 401 forever
> on a brand-new `account_id` — this bit `notebook/arb.py` in practice
> before it was fixed to use this pattern.

### `GET /products`

No auth. Lists the two configured spot products (**not** the spread
instrument or option contracts — see
[Instruments you can't discover via GET /products](#instruments-you-cant-discover-via-get-products)).

Response `200`:
```json
[
  {"symbol": "BTC-MINI", "underlying": "BTC/USD", "contract_size": 0.001, "max_position": 15, "index_price": 77.51},
  {"symbol": "ETH-MINI", "underlying": "ETH/USD", "contract_size": 0.03, "max_position": 15, "index_price": 72.92}
]
```

`index_price` is the current fair-value/index price (not the traded book's
own mid — those can and should drift apart).

### `GET /book/{product}`

Auth required. `404 {"detail": "unknown product"}` if `product` isn't a
live product (typo, or an expired option contract).

Response `200`: a [book snapshot](#shared-data-shapes).

### `WS /book/{product}/stream`

WebSocket. Auth via **query parameter**, not a header:

```
ws://127.0.0.1:8000/book/BTC-MINI/stream?api_key=<your key>
```

Pushes a full book snapshot every **0.5s** (`websocket.send_json(...)`,
2 Hz) until the client disconnects. Closes with code `4404` if `product`
is unknown, or `4401` if `api_key` is missing/invalid. No documented
per-connection cap is actually enforced server-side despite
`rate_limit.ws_connections_per_key: 1` existing in config — don't rely on
the server to stop you from opening more than one.

```python
import json, websocket
ws = websocket.create_connection(f"ws://127.0.0.1:8000/book/BTC-MINI/stream?api_key={api_key}")
msg = json.loads(ws.recv())
```

### `POST /orders`

Auth required.

Request:
```jsonc
{
  "product": "BTC-MINI",
  "side": "buy",        // "buy" | "sell"
  "type": "limit",      // "limit" | "market"
  "qty": 2,              // positive integer, contracts (not notional)
  "price": 77.50         // required for "limit"; omit/null for "market"
}
```

A market order is IOC: fills what it can against the book right now,
drops the rest (no `price` field needed or accepted as a limit).

Response `200`: an [Order](#shared-data-shapes).

Response `400` — `{"detail": "<reason>"}`, where `<reason>` is one of:
- `"unknown product {product}"`
- `"qty must be a positive integer"`
- `"limit order requires a price"`
- `"price must be positive, got {price}"` — except on a product that
  explicitly allows negative prices (currently just the BTC-ETH spread
  instrument, since its fair value is `btc_index - eth_index` and can
  legitimately cross zero)
- `"price {price} is not a multiple of tick_size ({tick_size}) for {product}"`
- `"unknown account {account_id}"`
- `"account is frozen"`
- `"order would breach MAX_POSITION ({max_position}) for {product}"` —
  checked against worst-case exposure including your resting orders, not
  just your current filled position
- `"{product} is currently disabled"` — the spread instrument or an
  option contract when an admin has turned it off

### `DELETE /orders/{order_id}`

Auth required. Cancels an order you own that's still `open` or
`partially_filled`.

Response `200`: the (now `cancelled`) [Order](#shared-data-shapes).

Response `400` — `{"detail": "<reason>"}`: `"no such order"`,
`"not your order"`, or `"order is not open"` (already filled/cancelled —
cancelling twice, including a race with your own second click, lands
here; treat it as "already handled," not a failure).

### `GET /orders`

Auth required, no params. Every order you've ever submitted, any status,
as a list of [Order](#shared-data-shapes).

### `GET /orders/{order_id}`

Auth required. A single order you own.

Response `404` if it doesn't exist or belongs to someone else:
`{"detail": "no such order"}`.

### `GET /fills`

Auth required, no params. Every fill you were party to (either side), as
a list of [Fill](#shared-data-shapes) — already resolved to your own
`side`/`role`/`counterparty`, not the raw maker/taker record.

### `GET /account`

Auth required, no params.

Response `200`:
```json
{
  "account_id": "my_handle",
  "cash": 998.20,
  "balance": 998.20,
  "realized_pnl": -1.80,
  "positions": { "BTC-MINI": {"qty": 5, "avg_cost": 77.54} },
  "unrealized_pnl": 3.10,
  "equity": 1001.30,
  "frozen": false
}
```
`cash` and `balance` are always the same value (both present for
convenience). `positions` only lists nonzero holdings. `equity` = cash +
unrealized PnL against current index prices.

### `GET /leaderboard`

No auth. Ranked by `equity` descending. Excludes bot accounts (`mm_`,
`noise_`, `arb_` prefixes) and the house `admin` account.

```json
[
  {"account_id": "quant1", "cash": 1023.40, "equity": 1051.20, "positions": {"BTC-MINI": 3}}
]
```

---

## Admin API (port 8001)

No CORS, no rate limiting on this API — it's meant for the presenter's own
scripts/curl, not browser JS from student pages. Every route needs one of:

```
X-Admin-Password: <admin.password from config.yaml>
```
or HTTP Basic auth (any username, that same password). If both are sent,
only `X-Admin-Password` is checked.

### Accounts

| Route | Body | Response |
|---|---|---|
| `POST /accounts` | `{"account_id": str}` | `{"account_id", "api_key", "active": false}` — creates the account **inactive**, with starting cash, no password set. Calling again for an existing `account_id` just returns the same key (idempotent). |
| `POST /accounts/{key}/activate` | — | `{"account_id", "active": true}`. `404 {"detail": "no such key"}` on an unknown key. |
| `POST /accounts/{key}/deactivate` | — | `{"account_id", "active": false}` |
| `POST /accounts/{account_id}/regenerate_key` | — | `{"account_id", "api_key" (new), "active"}` — invalidates the old key |
| `POST /accounts/{account_id}/freeze` | — | `{"account_id", "frozen": true}`. `404` on unknown account. |
| `POST /accounts/{account_id}/unfreeze` | — | `{"account_id", "frozen": false}` |
| `GET /accounts/{account_id}/orders` | — | `[{"id", "product", "side", "status", "remaining_qty"}, ...]` — empty list (not 404) for an unknown account |
| `DELETE /accounts/{account_id}/orders` | — | `{"killed": [order_id, ...]}` — cancels every resting order for the account |
| `GET /accounts` | — | `[{"account_id", "api_key", "active"}, ...]` for **every** account — plaintext keys, no pagination. Treat this route itself as sensitive. |

This is the admin-driven registration path — for setting up accounts ahead
of time without a password (e.g. seeding a roster). Self-serve
registration via `POST /register` on the public API is the normal path
and doesn't need any of this.

### Market events

| Route | Body | Notes |
|---|---|---|
| `POST /events/shock` | `{"product", "target_offset", "ramp_seconds"=1.0, "hold_seconds"=5.0, "name"="shock"}` | Ramps the index price by `target_offset`, holds, decays back. No product validation — a bad `product` raises uncaught (500). |
| `POST /events/drift` | `{"product", "drift", "duration_seconds", "name"="drift"}` | Sustained directional drift. |
| `POST /events/spread` | `{"kind": "widen"\|"invert"\|"converge", "magnitude", "ramp_seconds"=2.0, "hold_seconds"=8.0, "name"="spread"}` | Affects **both** BTC-MINI and ETH-MINI together (moves them apart/together/flips sign) — this is what makes the BTC-ETH spread legitimately cross zero. `400` on a bad `kind`. |
| `POST /events/liquidity` | `{"product", "kind": "withdraw"\|"flood", "duration_seconds", "magnitude"=2.0}` | Temporarily thins or floods that product's resting book depth. `400` on a bad `kind`. |

Response shape for all four is a small confirmation dict (e.g.
`{"name", "product"}` or `{"name", "products": [...]}`) — the actual
effect is asynchronous, driven by the feed loop.

### Feed control

| Route | Body | Response |
|---|---|---|
| `POST /feed/mode` | `{"product", "mode": "live"\|"replay"}` | `{"product", "mode"}`. `400` on any other `mode` string. No check that `product` exists. |
| `POST /feed/replay_speed` | `{"speed": float}` | `{"speed"}` — global, not per-product |
| `POST /synthetic/params` | `{"product", "annual_volatility"?, "annual_drift"?}` | Only overwrites fields you actually send. `400` on unknown product. |
| `GET /synthetic/params` | — | `{product: {"annual_volatility", "annual_drift"}, ...}` for every product |

### Market makers / noise / arb bots

| Route | Body | Response |
|---|---|---|
| `POST /bots` | `{"product", "base_spread_frac"=0.004, "quote_size"=3, "skew_sensitivity"=0.05, "requote_interval"=2.0}` | Spawns a new MM bot. `{"account_id", "product", "config"}`. `400` if `product` isn't a configured spot product. |
| `GET /bots` | — | `[{"account_id", "product", "config"}, ...]` — every MM bot |
| `POST /bots/{account_id}/params` | `{"base_spread_frac"?, "quote_size"?, "active"?}` | Only non-null fields applied. `404` if no such bot. |
| `POST /bots/noise` | `{"product", "arrival_rate_per_sec"=0.3, "max_size"=2}` | Spawns a noise bot |
| `GET /bots/noise` | — | same shape as `/bots` |
| `POST /bots/noise/{account_id}/params` | `{"arrival_rate_per_sec"?, "max_size"?, "active"?}` | `404` if not found |
| `GET /bots/arb` | — | every arb bot |
| `POST /bots/arb/{account_id}/params` | `{"threshold_ticks"?, "correction_qty"?, "check_interval"?, "active"?}` | `404` if not found |
| `POST /bots/spread_scale` | `{"scale": float}` | Global multiplier on every MM bot's quoted spread width. `400` if `scale <= 0`. |
| `GET /bots/spread_scale` | — | `{"global_spread_scale"}` |

Every bot's `config` in these responses is the bot's full dataclass dump —
more fields than any single request body accepts (e.g. `product`,
`account_id`, `active`), useful for inspecting current state before
tweaking one field.

### Market data (admin view)

| Route | Notes |
|---|---|
| `GET /market/{product}` | `404` for anything not in the **static** configured product list — this means it 404s for the spread symbol and live option contracts even though they're real, tradeable products. Use the website's `GET /data/book/{product}` for those instead (see below) — it checks the live engine, not the static config. |

Response (when it resolves): book, `index_price`, staleness, last trade,
mid/spread_bps, a sparkline (up to 120 points), session volume — the same
shape the website's ladder polls.

### Spread & options instrument toggles

| Route | Body | Response |
|---|---|---|
| `GET /instruments/spread` | — | `{"symbol", "enabled"}` |
| `POST /instruments/spread/enabled` | `{"enabled": bool}` | `{"symbol", "enabled"}` |
| `GET /instruments/options` | — | `{"enabled", "underlying", "contracts": [{"symbol", "strike", "option_type", "expiry_ts", "theo", "bid", "ask"}, ...]}` |
| `POST /instruments/options/enabled` | `{"enabled": bool}` | `{"enabled"}` |

`GET /instruments/options` is the one admin-side route that *does* list
every live contract with strike/theo/bid/ask in one call — handy for
scripting against the chain from the admin side even though students
can't see this route.

### `GET /`

The admin dashboard itself (HTML) — tabs for everything above, same-origin
`fetch` calls to the JSON routes. Useful as a live reference for exact
request bodies if this doc and the source ever drift.

---

## Website JSON endpoints (port 8090)

These back the browser UI. No auth on any of them except `/data/portfolio`
(a query-param API key, not a header) — they're meant to be read by the
page's own JS, not treated as a stable trading API, but they're
convenient for scripting a presenter dashboard.

| Route | Params | Response |
|---|---|---|
| `GET /data/book/{product}` | — | Same shape as admin's `GET /market/{product}`, but checks the **live engine** — works for the spread symbol and option contracts, unlike the admin route. `404 {"detail": "unknown product"}` if truly unknown. |
| `GET /data/leaderboard` | — | Identical to the public API's `GET /leaderboard` |
| `GET /data/options` | — | `{"expiry_ts", "contracts": [{"symbol", "strike", "option_type", "theo", "bid", "ask"}, ...], "underlying_price"}` — everything needed to render the options chain in one call, including the live BTC index price |
| `GET /data/portfolio` | `?key=<api_key>` | `{"account_id", "cash", "balance", "realized_pnl", "positions", "unrealized_pnl", "equity", "frozen", "recent_fills": [...] (last 50), "open_orders": [...]}` — everything the portfolio page shows, one call |
| `POST /data/register` | `{"account_id", "password"}` | Mirrors the public API's `/register`; the site's own JS actually calls the public API directly instead, so this route mostly exists for parity |

---

## Error reference

Every error response across all three services is `{"detail": "<message>"}`
on a non-2xx status. Common ones:

| Status | Meaning | Where |
|---|---|---|
| `400` | Request rejected by business logic (see per-route reason strings above) | Public + Admin |
| `401` | Missing/invalid `X-API-Key`, `X-Admin-Password`, or bad login credentials | Public + Admin |
| `404` | Unknown product / order / account / API key | Public + Admin + Website |
| `409` | `account_id` already registered | Public (`/register`) |
| `422` | Request body failed Pydantic validation (wrong type, missing required field) — FastAPI's default shape, not this app's | All three |
| `429` | Rate limit exceeded (see below) | Public only |

## Rate limiting

Public API only, per `X-API-Key`, token bucket: `20` requests/second
sustained, `40` burst (`config.yaml` → `rate_limit`). Exceeding it gets
`429 {"detail": "rate limit exceeded"}`, sometimes with a `Retry-After`
header. Every bot in `notebook/` retries `429` with exponential backoff
instead of treating it as fatal — a shared `requests.Session` firing
several calls per tick will trip this occasionally under normal operation,
it isn't a bug:

```python
def _request(method, url, **kwargs):
    delay = 0.25
    r = session.request(method, url, **kwargs)
    while r.status_code == 429 and attempt < 8:
        wait = float(r.headers.get("Retry-After", delay))
        time.sleep(wait)
        delay *= 2
        attempt += 1
        r = session.request(method, url, **kwargs)
    return r
```

The Admin API has no rate limiting at all.

## Instruments you can't discover via `GET /products`

`GET /products` only lists the two YAML-configured spot products
(`BTC-MINI`, `ETH-MINI`). Two more tradeable products exist but are
deliberately left off that list:

- **The BTC-ETH spread** (`BTC-ETH-MINI` by default, `config.yaml` →
  `spread.symbol`) — trade it directly via `GET /book/BTC-ETH-MINI` and
  `POST /orders` like any other product; its `max_position` isn't exposed
  anywhere in the public API, so a bot trading it has to hardcode the
  value from `config.yaml` and keep it in sync. Its fair value is
  `btc_index - eth_index`, computed client-side from `GET /products`'
  two index prices — there's no server endpoint that hands you this
  number directly.

- **The 15-minute BTC options chain** — symbols look like
  `BTC-0515-77.00C` (`BTC-{expiry HHMM UTC}-{strike:.2f}{C|P}`). Neither
  the symbol list nor the strike ladder is available from the public API.
  A bot has to either read the website's unauthenticated
  `GET /data/options` (not really "the API," but convenient), or derive
  candidate symbols itself from the same rules the server uses
  (`window_seconds`, `strikes_each_side`, `strike_increment` — hardcode
  these from `config.yaml`'s `options:` block) and confirm each guess is
  currently live via `GET /book/{symbol}` (a `404` means that
  strike/window isn't trading right now). See `notebook/vol_trader.py`
  for a working example of the second approach.

Both instruments can be toggled on/off by an admin
(`POST /instruments/spread/enabled`, `POST /instruments/options/enabled`)
— `POST /orders` against either while disabled gets rejected with
`"{product} is currently disabled"`.
