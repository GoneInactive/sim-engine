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

### RFQs

Ask the rest of the exchange for a firm two-way price on any size instead
of working the book yourself — any other account (student or bot) can
respond with a quote; you pick whichever one you like and accept it. This
executes immediately, bilaterally, at the quoted price — it never touches
the product's order book, so it doesn't move the visible market and can't
be front-run off the book.

Quote visibility is asymmetric on purpose: the requester sees every quote
on their own RFQ (needed to compare and pick a winner); anyone else only
ever sees their own quote(s) — not competing dealers' prices.

| Route | Body | Response |
|---|---|---|
| `POST /rfqs` | `{"product", "side": "buy"\|"sell", "qty", "ttl_seconds"=30}` | Creates an RFQ. `ttl_seconds` is clamped to [5, 300]. `400` if `product` is unknown/disabled, `qty` isn't a positive integer, or you already have 5 open RFQs. |
| `GET /rfqs` | `?product=` (optional filter) | Every currently **open** RFQ (yours and everyone else's) as a list of [RFQ](#rfq-shape). |
| `GET /rfqs/{rfq_id}` | — | A single RFQ (any status). `404` if it doesn't exist. |
| `DELETE /rfqs/{rfq_id}` | — | Cancels your own open RFQ (and withdraws any quotes on it). `400` — `"not your RFQ"` or `"RFQ is not open"`. |
| `POST /rfqs/{rfq_id}/quotes` | `{"price", "qty"?}` | Quotes a firm price to fill (part of) someone else's RFQ. `qty` defaults to the RFQ's full remaining size. `400` — `"cannot quote your own RFQ"`, `"RFQ is not open"`, `"qty exceeds the RFQ's remaining size (...)"`, or a price-sign error (same rule as `POST /orders`). |
| `DELETE /rfqs/{rfq_id}/quotes/{quote_id}` | — | Withdraws your own still-open quote. |
| `POST /rfqs/{rfq_id}/quotes/{quote_id}/accept` | — | **Requester only.** Executes the trade at the quote's price. Response: `{"fill": <Fill>, "rfq": <RFQ>}`. `400` if you're not the requester, the RFQ/quote isn't open any more, or the product got disabled since the quote was posted. Any other quote left sized larger than what remains of the RFQ after this fill is automatically withdrawn (it can no longer be fully honored). |

#### RFQ shape

```jsonc
{
  "id": 7,
  "account_id": "quant1",       // the requester
  "own": true,                   // true iff this is your own RFQ (viewer-relative)
  "product": "BTC-MINI",
  "side": "buy",                  // the direction the requester wants to trade
  "qty": 5,
  "remaining_qty": 5,             // drops as quotes are accepted; RFQ auto-fills at 0
  "created_at": 1789022665.07,
  "expires_at": 1789022695.07,
  "status": "open",               // "open" | "filled" | "expired" | "cancelled"
  "quotes": [                     // every quote if you're the requester, else only your own
    {"id": 3, "rfq_id": 7, "account_id": "mm_hopeful", "price": 77.52, "qty": 5,
     "timestamp": 1789022670.1, "status": "open"}
  ]
}
```

An RFQ never appears in `GET /book/{product}` or the sparkline/volume
stats a normal fill feeds — it's a private negotiation between two
accounts, settled through the same ledger/fee/MAX_POSITION rules as any
other fill, but off-book. The website's `rfqs` WS channel (see below)
mirrors this same visibility rule for the browser UI at `/rfq`.

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

Every option contract, futures contract, and calendar spread also gets its
own MM bot **and** `noise_bots.options`/`noise_bots.futures`-many noise
bots (`config.yaml` → `noise_bots:`, default 2 each) automatically as soon
as it's created — you don't need to call `POST /bots`/`POST /bots/noise`
for these yourself, and `GET /bots` / `GET /bots/noise` will list
`mm_<symbol>_N` / `noise_<symbol>_N` accounts for them alongside the base
products' bots. They're torn down automatically when that instrument
expires/settles or the chain rolls, same lifecycle as its MM bot.

### Market data (admin view)

| Route | Notes |
|---|---|
| `GET /market/{product}` | Checks the **live engine** (`state.engine.products`), not just the static config list — this resolves for the spread symbol, live option contracts, live futures contracts, and calendar spreads too, not just `BTC-MINI`/`ETH-MINI`. `404 {"detail": "unknown product"}` for anything not currently live (unknown symbol, or a real symbol that's expired/rolled off). The website's `GET /data/book/{product}` (see below) is the same check, just unauthenticated — either works for any live instrument. |
| `GET /products` | Every currently-live tradeable symbol as a flat sorted list of strings (base products + spread + every live option/futures/calendar-spread symbol) — what the admin panel's own bot-spawn forms populate their product dropdown from. |

Response (when it resolves): book, `index_price`, staleness, last trade,
mid/spread_bps, a sparkline (up to 120 points), session volume — the same
shape the website's ladder polls.

### Spread, options & futures instrument toggles

| Route | Body | Response |
|---|---|---|
| `GET /instruments/spread` | — | `{"symbol", "enabled"}` |
| `POST /instruments/spread/enabled` | `{"enabled": bool}` | `{"symbol", "enabled"}` |
| `GET /instruments/options` | — | `{chain_id: {"enabled", "underlying", "contracts": [{"symbol", "strike", "option_type", "expiry_ts", "theo", "bid", "ask"}, ...]}, ...}` — **one entry per configured chain** (`btc`, `eth`, `btc_eth_spread` by default), not a single flat object. |
| `POST /instruments/options/{chain_id}/enabled` | `{"enabled": bool}` | `{"chain_id", "enabled"}`. `404 {"detail": "no such options chain"}` for an unknown `chain_id`. |
| `GET /instruments/futures` | — | `{"enabled", "underlyings": {underlying: {"futures": [{"symbol", "expiry_ts", "last"}, ...], "calendar_spreads": [{"symbol", "near", "far", "last"}, ...]}, ...}}` |
| `POST /instruments/futures/enabled` | `{"enabled": bool}` | `{"enabled"}` — applies to **every** underlying's futures ladder at once (there's no per-underlying toggle, unlike options' per-chain one). |

`GET /instruments/options` and `GET /instruments/futures` are the two
admin-side routes that list every live contract (with strike/theo/bid/ask
for options; symbol/expiry/last for futures and calendar spreads) in one
call — handy for scripting against either chain from the admin side even
though students can't see these routes. Note `GET /instruments/futures`
doesn't include bid/ask (just the theo `last`) — hit `GET /market/{symbol}`
per-contract, or the website's `futures_matrix` WS channel (see below), if
you need top-of-book.

### `GET /`

The admin dashboard itself (HTML) — tabs for everything above, same-origin
`fetch` calls to the JSON routes. Useful as a live reference for exact
request bodies if this doc and the source ever drift.

---

## Website JSON endpoints (port 8090)

The old per-page REST polling routes (`GET /data/book/{product}`,
`GET /data/leaderboard`, `GET /data/options`, `GET /data/portfolio`) are
**gone** — the website now pushes everything over one shared multi-channel
WebSocket instead. `POST /data/register` is the only plain REST route left.
No auth on either except the WS's own `key` query param.

| Route | Params | Response |
|---|---|---|
| `POST /data/register` | `{"account_id", "password"}` | Mirrors the public API's `/register`; the site's own JS actually calls the public API directly instead, so this route mostly exists for parity |
| `WS /ws` | `?channels=<comma-separated>&key=<api_key optional>` | Subscribes to one or more channels (below); each pushes `{"channel": "<name>", "data": <payload>}` as a text frame whenever that channel's payload actually changes (polled server-side every 0.2s, but deduped — a quiet channel doesn't spam identical frames). `key` is only needed for the `portfolio` channel. |

### `/ws` channels

Not a stable public API (same caveat as the old `/data/*` routes — this is
what the site's own JS uses), but the only place left to read this data
without polling per-product REST, and genuinely convenient for a live
dashboard or a bot that wants push updates instead of tight-polling
`GET /book/{product}`:

| Channel | Payload (the `data` field) |
|---|---|
| `book:<product>` | Same shape as admin's `GET /market/{product}` (book, `index_price`, staleness, last trade, mid/spread_bps, sparkline, session volume) — works for **any** live product: base products, the spread symbol, a live option contract, a live futures contract, or a calendar spread. |
| `options:<chain_id>` | `{"expiry_ts", "contracts": [{"symbol", "strike", "option_type", "theo", "bid", "ask"}, ...], "underlying_price"}` for that chain (`btc`, `eth`, `btc_eth_spread` by default — see `config.yaml` → `options:`). Unknown/disabled `chain_id` just never sends anything on that channel rather than erroring. |
| `futures_matrix` | `{underlying: {"futures": [{"symbol", "expiry_ts", "last", "bid", "ask"}, ...], "calendar_spreads": [{"symbol", "near", "far", "last", "bid", "ask"}, ...]}, ...}` for every configured futures underlying — the same data behind the Inter-Spread page's spread matrix. |
| `leaderboard` | Identical to the public API's `GET /leaderboard` |
| `rfqs` | Every open [RFQ](#rfq-shape), same requester-only quote-visibility rule as the public API (resolved from the WS `key` param; no `key` means every RFQ's `quotes` comes back empty). Backs the `/rfq` page. |
| `portfolio` | Requires `key`; `{"account_id", "cash", "balance", "realized_pnl", "positions", "unrealized_pnl", "equity", "frozen", "recent_fills": [...] (last 50), "open_orders": [...]}`, or `{"error": "invalid_key"}` if `key` doesn't resolve |
| `chat` | The last 200 chat messages, `[{"id", "account_id", "text", "timestamp"}, ...]` |

```python
import json, websocket
ws = websocket.create_connection("ws://127.0.0.1:8090/ws?channels=options:btc,futures_matrix")
while True:
    msg = json.loads(ws.recv())
    print(msg["channel"], msg["data"])
```

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
(`BTC-MINI`, `ETH-MINI`). Several more tradeable products exist but are
deliberately left off that list:

- **The BTC-ETH spread** (`BTC-ETH-MINI` by default, `config.yaml` →
  `spread.symbol`) — trade it directly via `GET /book/BTC-ETH-MINI` and
  `POST /orders` like any other product; its `max_position` isn't exposed
  anywhere in the public API, so a bot trading it has to hardcode the
  value from `config.yaml` and keep it in sync. Its fair value is
  `btc_index - eth_index`, computed client-side from `GET /products`'
  two index prices — there's no server endpoint that hands you this
  number directly.

- **Options chains** — there are three independent 15-minute chains by
  default (`config.yaml` → `options:` — `btc` on `BTC-MINI`, `eth` on
  `ETH-MINI`, `btc_eth_spread` on the `BTC-ETH-MINI` spread itself), each
  with its own window length, strikes, and implied vol. Symbols look like
  `BTC-0515-77.00C` (`{chain-id prefix}-{expiry HHMM UTC}-{strike:.2f}{C|P}`
  — see `OptionsChainManager._symbol`). Neither the chain list, the symbol
  list, nor the strike ladder is available from the public API. A bot has
  to either subscribe to the website's `options:<chain_id>` WS channel
  (see [`/ws` channels](#ws-channels) above — not really "the API," but
  convenient and pushed live), or derive candidate symbols itself from the
  same rules the server uses (`window_seconds`, `strikes_each_side`,
  `strike_increment` — hardcode these from `config.yaml`'s `options:`
  block, per chain) and confirm each guess is currently live via
  `GET /book/{symbol}` (a `404` means that strike/window isn't trading
  right now). See `notebook/vol_trader.py` for a working example of the
  second approach (hardcoded against the `btc` chain).

- **Futures & calendar spreads** — `config.yaml` → `futures:` keeps
  `num_live` (5 by default) 1-hour futures contracts rolling per
  underlying (`BTC-MINI`, `ETH-MINI`), each `window_seconds` apart and
  cash-settled at the underlying's index price on expiry, plus one
  calendar spread auto-registered between every adjacent pair of live
  contracts. Futures symbols look like `BTC-FUT-091507`
  (`{underlying's base ticker}-FUT-{expiry as MMDDHH UTC}` — see
  `FuturesChainManager._contract_symbol`); calendar spread symbols are
  just the two legs joined, e.g. `BTC-FUT-091507_BTC-FUT-091607-CAL`
  (`{near symbol}_{far symbol}-CAL`), and price as `near - far` (both
  already contract-scaled), so unlike every other instrument here they
  legitimately trade at a negative price. Same story as options — no
  public-API listing endpoint — subscribe to the website's
  `futures_matrix` WS channel to discover what's currently live (symbols,
  expiries, top-of-book), or reconstruct the ladder yourself from
  `window_seconds`/`num_live` and confirm each guess via
  `GET /book/{symbol}`.

All of the above can be toggled on/off by an admin
(`POST /instruments/spread/enabled`, `POST /instruments/options/{chain_id}/enabled`,
`POST /instruments/futures/enabled`) — `POST /orders` against any of them
while disabled gets rejected with `"{product} is currently disabled"`.
Disabling never tears down an in-flight chain/ladder early; it just stops
new contracts from being created once the current ones expire/roll, so
open positions always get to settle normally.
