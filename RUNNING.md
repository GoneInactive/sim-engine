# Building and Running

## Prerequisites

- Python 3.11+

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Config

All network settings (ports, bind addresses, passwords, DB URL) live in
[config/config.yaml](config/config.yaml). Defaults are `127.0.0.1` for
local use. Every value can also be overridden by an env var (see
`src/exchange/config.py` for the full list, e.g. `API_PORT`,
`ADMIN_PASSWORD`, `WEBSITE_PASSWORD`).

To point this at a real server later, change the `host` fields under
`network` in the config (or set `API_HOST=0.0.0.0` etc.) and update the
`public.*_base_url` values to the server's real IP or hostname. No code
changes needed.

## Tests

```bash
.venv/bin/pytest tests/ -q
```

## Run

```bash
.venv/bin/python run.py
```

This starts, in one process:

- Public API on `http://127.0.0.1:8000`
- Admin API on `http://127.0.0.1:8001`
- Website on `http://127.0.0.1:8080`
- Synthetic price feed (GBM random walk) + a random background event scheduler
- Market maker and noise bots

Stop with Ctrl+C, or `pkill -f "python run.py"` if running in the
background.

## Accessing the website

`http://127.0.0.1:8080` — a clickable trading ladder for BTC-MINI and
ETH-MINI side by side (Working / Bid / Price / Ask, click a Bid to sell or
an Ask to buy at that level, click your own Working qty to cancel it), a
chart with y-axis, high/low markers, and mouse hover, mid/spread/last
trade. `/leaderboard` and `/portfolio` (positions, PnL, open orders,
recent fills with counterparty).

The whole site is password-gated with HTTP Basic auth (username can be
anything, password is `website.password` from config.yaml). On top of
that, trading requires logging into a trading account via the bar at the
top of every page — register with any username/password (active
immediately, deposits $1,000, no admin approval needed) or log back in
with existing credentials. The admin account itself is also a trading
account, seeded with $1,000,000 — log in as username `admin` with the
admin panel password to trade as the house.

## Accessing the admin panel

`http://127.0.0.1:8001` — a form-based admin page (register/activate
accounts, freeze, kill orders, trigger market events, feed mode, bot
params). Password-gated with HTTP Basic, same credential as the JSON API
below (`admin.password` from config.yaml).

## Accessing the admin API

Base URL `http://127.0.0.1:8001`. Every request needs either the header
`X-Admin-Password` or HTTP Basic auth, set to `admin.password` from
config.yaml.

Register a student account the admin-driven way (inactive until
activated — for setting up accounts ahead of time without a password;
self-serve registration via the website doesn't need this):

```bash
curl -X POST http://127.0.0.1:8001/accounts \
  -H "X-Admin-Password: <password from config.yaml>" \
  -H "Content-Type: application/json" \
  -d '{"account_id":"student1"}'
```

Returns an `api_key`, inactive by default. Activate it:

```bash
curl -X POST http://127.0.0.1:8001/accounts/<key>/activate \
  -H "X-Admin-Password: <password from config.yaml>"
```

Trigger a market event, e.g. a price shock:

```bash
curl -X POST http://127.0.0.1:8001/events/shock \
  -H "X-Admin-Password: <password from config.yaml>" \
  -H "Content-Type: application/json" \
  -d '{"product":"BTC-MINI","target_offset":5,"ramp_seconds":1,"hold_seconds":3}'
```

Other admin endpoints: `/accounts/{key}/deactivate`,
`/accounts/{id}/freeze`, `/accounts/{id}/unfreeze`,
`/accounts/{id}/orders` (GET to view, DELETE to kill),
`/events/drift`, `/events/spread`, `/events/liquidity`,
`/feed/mode`, `/feed/replay_speed`, `/bots/{id}/params`, `/bots`.

## Accessing the public (student) API

Base URL `http://127.0.0.1:8000`. Self-serve register/login (active
immediately, no admin step, deposits $1,000):

```bash
curl -X POST http://127.0.0.1:8000/register -H "Content-Type: application/json" \
  -d '{"account_id":"student1","password":"pw"}'
curl -X POST http://127.0.0.1:8000/login -H "Content-Type: application/json" \
  -d '{"account_id":"student1","password":"pw"}'
```

Every other request needs the header `X-API-Key` set to that key.

```bash
curl http://127.0.0.1:8000/products
curl http://127.0.0.1:8000/book/BTC-MINI -H "X-API-Key: <key>"
curl -X POST http://127.0.0.1:8000/orders -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"product":"BTC-MINI","side":"buy","type":"market","qty":2}'
curl http://127.0.0.1:8000/account -H "X-API-Key: <key>"
curl http://127.0.0.1:8000/leaderboard
```

Rate limited per key (`rate_limit` in config.yaml, default 20 req/s,
burst 40). WS book stream: `ws://127.0.0.1:8000/book/{product}/stream?api_key=<key>`.
