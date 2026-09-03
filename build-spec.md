# Mini-Exchange Workshop — Build Spec

## 0. Context (for whoever picks this up)

This is a mock crypto exchange built for a live workshop at a university quant
trading club, run by the founder/PM of the club's market-making fund. The
presenter cannot show their actual production trading system (legal/IP), so
instead they built a fictional exchange to teach on:

1. Basic statistical arbitrage — how a BTC product and an ETH product should
   trade relative to each other.
2. Trading system design — presenter walks through their own (generic, non-
   proprietary) trading system live against this exchange.
3. Alpha decay — the presenter's signal gets handed to ~10-20 students at
   once via a Jupyter notebook; they get to watch it stop working in real
   time as everyone trades the same edge.
4. Left running multi-day after the talk so students can try to find the
   relationship themselves and trade it.

This doc is the full spec: exchange, price feed, bots, API, website, admin
tooling, and the student notebook. It's meant to be handed to an
implementation agent (Claude Code) to build end to end.

---

## 1. Goals / non-goals

**In scope**
- Two tradable products, cash-settled, spot-style (no margin, no leverage,
  no liquidations).
- Real live BTC/USD and ETH/USD prices from Kraken as the reference
  ("index"/"mark") price for each product.
- A real limit order book / matching engine per product — trades happen on
  *our* book, not Kraken's. Our traded price can and should diverge from the
  Kraken index based on our own order flow (this is intentional — it's the
  basis/market-making lesson).
- Market maker bots and noise bots per product to keep the book alive
  24/7 for a multi-day run.
- An admin-only control surface to inject scripted shocks and liquidity
  events, and to run a pre-recorded/manipulated historical replay during the
  live talk (see §6).
- A password-gated website: live order book + leaderboard.
- A REST/WS API for students to connect trading bots (via a notebook).
- Runs unattended on a single Hetzner VM for several days.

**Explicitly out of scope**
- Margin, leverage, liquidations, funding rates.
- Market abuse rules of any kind (spoofing, wash trading, etc. are all
  allowed — "no market rules" was an explicit decision. This is itself a
  bonus lesson if it happens).
- KYC/real money/anything touching actual funds. This is a closed, fake-
  money simulation only.

---

## 2. Products

| Product | Underlying ref. | Contract size | Approx. notional/contract* |
|---|---|---|---|
| `BTC-MINI` | BTC/USD (Kraken) | 0.001 BTC | ~$75–80 |
| `ETH-MINI` | ETH/USD (Kraken) | 0.03 ETH | ~$70–75 |

\* At the time of writing BTC ≈ $77.5k and ETH ≈ $2.43k, putting both
contracts in the same ballpark (~$75), so a roughly 1:1 contract-count
hedge is dollar-neutral *today*. **This ratio will drift as the real
BTC/ETH price ratio drifts** — that's intentional, not a bug. Don't
hardcode an expected hedge ratio anywhere; it's part of what students are
meant to discover, and part of what makes it go stale for them (alpha
decay lesson #2, for free).

Quantities are **integers only** — no fractional contracts. Order size is
always a whole number of contracts.

---

## 3. Accounts, positions, cash

- Each participant (student or team) gets one account.
- Starting cash: **$1,000**, shared across both products (not $1,000 per
  product).
- Positions are signed integers per product: positive = long, negative =
  short. Shorting is free (no borrow cost).
- **Symmetric position cap**: `abs(position) <= MAX_POSITION` per product,
  enforced at order-acceptance time (reject/reduce-only orders that would
  breach it). `MAX_POSITION` is a config value per product — pick something
  that lets ~13 contracts fit inside $1,000 of notional per product as a
  starting point, tune during load testing.
- **Cash accounting rule**: cash balance only changes when a position is
  *closed or reduced* (realized PnL on that trade gets added/subtracted from
  cash). Opening or adding to a position does **not** debit cash upfront.
  - **Flip-through-zero example**: account is long +5 BTC-MINI at avg entry
    $75. A sell order for 8 contracts fills at $80. This is **two legs**,
    not one: first, close the existing +5 at $80 (realize `5 * (80-75) =
    $25` into cash), then open a **new** short position of -3 at $80 (no
    cash impact, cost basis = $80). Implement fills that cross zero as an
    explicit split into a closing leg + opening leg — don't try to net PnL
    across the whole fill in one step.
- **Leaderboard metric** = `cash + unrealized_pnl`, where unrealized PnL is
  computed by marking open positions to the current **index price** (§4),
  not the last traded price on our book.

### Open decision — flag, don't silently resolve
There is currently **no buying-power check**. Because cash isn't reserved
on order entry, `MAX_POSITION` is the *only* thing bounding a student's
risk — at ~$75/contract and a generous `MAX_POSITION`, a student can hold
notional well in excess of their $1,000 balance with zero capital behind
it. This may be intentional (matches the "no market rules" philosophy) but
build it as a **config toggle** (`ENFORCE_BUYING_POWER: bool`, default
`false`) so it can be flipped without a code change if it causes problems
mid-event.

### Open decision — zero/negative equity
No auto-freeze is specified. Default behavior: **do nothing special**,
let equity go negative, it just shows on the leaderboard. Build a
`FREEZE_ON_ZERO_EQUITY: bool` config flag (default `false`) in case the
presenter wants to flip it live.

---

## 4. Price feed (index / mark price)

One index price per product, used for: MM bot quoting, leaderboard
mark-to-market, and admin-event injection. This is **separate** from the
traded price on our own order book.

### 4.1 Live mode (default, multi-day run)
- WebSocket client to Kraken's public market data feed for `BTC/USD` and
  `ETH/USD` (no API key required).
- Maintain a continuously-updated mid/last price per product from the
  Kraken stream.
- Scale to contract terms: `index_price = kraken_price * contract_size`.

### 4.2 Staleness fallback ("shitty SMA")
- Track time since last Kraken update per product.
- If no update for `> STALE_THRESHOLD_SECONDS` (config, start around 5–10s):
  switch that product's index into **fallback mode**.
- Fallback mode: continue the index price forward using a simple moving
  average of the last N ticks of **our own already-computed index price**
  (not raw Kraken ticks) (config `SMA_WINDOW`), i.e. just holds roughly
  flat / drifts with recent trend — deliberately crude, not a real model.
- On reconnect: **blend back in** rather than hard-snapping — linearly
  interpolate from the fallback price to the live Kraken price over a short
  window (config `RECONNECT_BLEND_SECONDS`, start around 5–10s) so the
  index doesn't teleport and cause a spurious fill/liquidation-style event
  against resting orders. Log every stale→live transition.
- Implement Kraken WS reconnect with exponential backoff regardless of the
  above — staleness handling and reconnect logic are separate concerns.

### 4.3 Historical replay mode (for the live talk demo only)
- Ahead of the workshop: run a script to pull ~24h of real Kraken BTC/USD
  and ETH/USD trade/quote history and store it locally.
- Build a second script to **manipulate** that dataset — inject a small
  number of scripted jump/shock events at known timestamps (see §6) into an
  otherwise-real price path.
- Replay mode plays this dataset back through the exact same "index price"
  interface as live mode, at a configurable speed multiplier (so an hour of
  history can be compressed into a few minutes live on stage), with the
  scripted shocks landing on cue.
- This is how the presenter demos "watch the market maker react to a
  shock" live and reliably, without depending on real BTC/ETH actually doing
  something interesting during the 5-minute window they're on stage.
- After the live demo segment ends, the feed service switches back to
  **live mode** (§4.1) for the multi-day open period. This should be a
  single admin action (see §7), not a redeploy.

---

## 5. Matching engine

- One order book per product. Standard **price-time priority** limit order
  book.
- Order types: `limit` and `market` (market = IOC against current book).
  Nothing fancier needed (no stop orders, no iceberg, etc.).
- On order acceptance: enforce integer quantity, enforce `MAX_POSITION`
  (post-trade position must stay within the symmetric cap), reject
  otherwise.
- On match: generate fills for both sides, update positions, realize PnL
  into cash for the reducing side of any position (per §3's cash rule).
- Persist: order book state (or reconstructible from an event log), full
  trade tape, per-account order/fill history.
- Expose book state and trade tape over both REST (snapshot) and WS
  (stream) — students will want to stream the book, the website will want
  to stream it too.

---

## 6. Bots

Per product (so this is all ×2 for BTC-MINI and ETH-MINI):

### 6.1 Market makers — 5 bots
- Quote both sides of the book around the current **index price** (§4),
  not around the last traded price — so MM bots are the mechanism that
  keeps our book's price anchored to reality and correct any dislocation
  caused by student order flow.
- Configurable base spread per bot (vary slightly bot-to-bot so the book
  has some natural depth/shape instead of 5 identical stacked quotes).
- **Inventory skew**: each bot adjusts its quote midpoint away from index
  in proportion to its own current position (long → skew offers down /
  bids down to encourage selling back to flat; short → skew up), classic
  MM inventory management. This is also a good thing for the presenter to
  point at live and explain.
- Requote on a short timer (e.g. every 1–3s) and/or whenever index price
  moves more than some threshold.

### 6.2 Noise / flow bots — 3 bots
- Random small orders (mostly marketable/limit-near-touch), Poisson
  arrival process, no signal — this is the "non-adverse" flow the market
  makers are supposed to be able to profit from safely, in contrast to a
  student running an actual signal.

### 6.3 Admin-injected events
Event types the admin can trigger live (via API/admin panel, §7),
independent of whether the feed is in replay or live mode. All events run
through the **same code path** in both replay and live mode — the live
demo must exercise the identical logic that runs during the unattended
multi-day period, not a separate replay-only implementation.

- **Price shock**: push the index price for a product by a configured
  amount over a configured duration (a fast synthetic jump). Implementation
  is a temporary additive/multiplicative offset applied on top of whatever
  §4 mode is currently producing. On expiry, **decay back to the
  underlying index using the same linear-interpolation blend mechanism as
  the §4.2 reconnect blend** (config `SHOCK_DECAY_SECONDS`) rather than
  snapping back — a hard snap-back is itself a second, unscripted shock in
  the opposite direction and will confuse MM bot inventory skew / trip
  spurious reactions.
- **Liquidity event**: temporarily change MM bot behavior — either
  *withdraw* (widen spreads / reduce quoted size / some bots go fully
  passive for a window — simulates a liquidity crunch) or *flood* (tighten
  spreads / increase quoted size — simulates a liquidity injection).
  Implement as a temporary multiplier on MM bot spread/size params with a
  duration, applied per-product.
- **Bull / bear market**: sustained directional drift applied to a
  product's index price over an extended duration (config: drift rate,
  duration), as opposed to price shock's instantaneous jump. Same offset
  mechanism as price shock, but the offset ramps continuously instead of
  jumping, and decays the same way on expiry.
- **BTC/ETH spread widen / invert / converge**: a *joint*, two-product
  event — applies offsetting offsets to the BTC-MINI and ETH-MINI index
  prices simultaneously so the cross-product basis moves without both legs
  just moving together:
  - *widen*: push the two index prices apart from their current ratio by a
    configured amount over a duration.
  - *invert*: push the spread through zero to the opposite sign.
  - *converge*: pull the spread toward zero (or toward a configured
    target ratio) over a duration.
  Implemented as two simultaneous, opposite-signed price-shock-style
  offsets (one per product) driven by a single admin action and a single
  event name, so they land/decay together and show up as one event in the
  trade tape. This is the core "statistical arbitrage" teaching event
  (§0.1) — the mechanism students are meant to discover should be exactly
  what this event demonstrates when the admin points it out live.

All event types should be nameable/loggable ("shock_1", "liquidity_pull_1",
"bull_1", "spread_invert_1" etc.) so they show up distinctly in the trade
tape / any post-event analysis, and so they can be scripted at specific
timestamps for the replay demo.

---

## 7. Admin API / control panel

Separate, more privileged auth from student accounts. Needs to support,
at minimum:

- **Validate/approve student accounts** — issued API keys start inactive
  (or in a pending state) and an admin action activates them; lets the
  presenter control exactly when/whether a given key can trade, and gives
  a single obvious lever to deactivate a misbehaving account instead of
  only being able to kill its open orders.
- Trigger a price shock on a product (§6.3).
- Trigger a liquidity event on a product (§6.3).
- Trigger a bull/bear market drift on a product (§6.3).
- Trigger a BTC/ETH spread widen/invert/converge event (§6.3).
- Switch a product's feed between `live` / `replay` / (fallback is
  automatic, not manually triggered).
- Set replay playback speed.
- Freeze/unfreeze a specific account (manual override, independent of the
  `FREEZE_ON_ZERO_EQUITY` config from §3).
- Adjust bot parameters live (spread, size, on/off) without a restart —
  useful both for the live demo and for general tuning during the multi-day
  run.
- View/kill any account's open orders (in case a bad student script starts
  spamming the book).

A simple password-gated web panel is enough — this doesn't need to be
pretty, it's presenter-only tooling.

---

## 8. Public API (for students)

- Auth: one API key per student/team, issued ahead of time, starts
  inactive until an admin validates/activates it (§7).
- Rate-limited per key: **20 req/s on REST (burst 40)**, **one persistent
  WS connection per key** — protects the book from a runaway loop in
  someone's notebook (this **will** happen). Notebook examples (§10) should
  show basic 429 backoff so students' own scripts don't hammer a limit
  they hit accidentally.
- Endpoints needed:
  - `GET /products` — list products, contract size, current index price.
  - `GET /book/{product}` — current order book snapshot.
  - `WS /book/{product}/stream` — streaming book updates.
  - `POST /orders` — submit order (`product`, `side`, `type`, `price`,
    `qty`).
  - `DELETE /orders/{id}` — cancel.
  - `GET /orders` / `GET /orders/{id}` — own order status.
  - `GET /fills` — own fill history.
  - `GET /account` — own cash, positions, unrealized/realized PnL.
  - `GET /leaderboard` — all accounts ranked by `cash + unrealized_pnl`
    (handles/team names only, not raw identities, if that matters for the
    club).

---

## 9. Website

Password-gated (single shared password is fine, this isn't sensitive).

Pages:
- **Order book view** per product — live bid/ask ladder, last trade price,
  index price shown alongside it (so the basis between traded price and
  index is visible — this is a good thing to leave on screen during the
  talk).
- **Leaderboard** — ranked by `cash + unrealized_pnl`, auto-refreshing.
- Optional but nice: a small live chart of traded price vs. index price
  over time per product, to visually show market makers pulling the book
  back toward the index after a shock/noise flow — reuse the visual
  language from the lead-lag dashboard mockup built earlier in this
  project if useful (dark terminal aesthetic, teal/amber accents already
  established there).

Read-only. Trading only happens through the API/notebook, not the website.

---

## 10. Student notebook

Given out at the "hand out the notebook" moment in the talk (see §0). Should
contain:

- Auth setup (API key → client).
- Example: fetch `GET /products`, `GET /book/{product}`.
- Example: submit a limit order, cancel an order.
- Example: poll/stream fills and account state.
- Example: pull the leaderboard.

**Deliberately does not include**: the presenter's actual fair-value model
or trading signal. The notebook is a *connectivity* kit only — students
derive the BTC/ETH relationship themselves (lesson 1), and only see the
presenter's own system as a live walkthrough (lesson 2), not as code they
receive. This sequencing is intentional — don't collapse it.

---

## 11. Infra

- Single Hetzner VM, everything containerized (docker-compose is fine at
  this scale — no need for orchestration overhead for a few-day event).
- Suggested services: `feed-service` (§4), `matching-engine` (§5, one
  process or one per product), `bots` (§6, can be one process spawning N
  bot workers), `api` (§8), `admin-api` (§7), `website` (§9), plus a
  datastore (Postgres is fine; order book itself can be in-memory with a
  Postgres-backed event log for durability/restart-recovery).
- Needs to survive a VM/process restart mid-event without losing account
  state — persist accounts/positions/cash and replay the trade log on boot,
  don't rely purely on in-memory state for anything that isn't the live
  order book itself.
- Load-test before the actual day: 2 products × (5 MM bots + 3 noise bots)
  + up to ~20 concurrent students hitting the API/WS is the target
  concurrency — cheap to simulate synthetically ahead of time, and cheap
  insurance against a stuck matching engine mid-talk.
- **Invariant/reconciliation check**: a periodic background job that
  verifies, per product, that the sum of all account positions nets
  against resting book exposure + bot inventory. Cheap to build alongside
  the matching engine (step 1) and high-value for catching cash/position
  bugs during the unattended multi-day run before they compound.
- **Admin action logging**: every admin action (shock, liquidity event,
  bull/bear drift, spread event, freeze, bot param change, account
  validation) gets written into the same event log as trades, not a
  separate audit trail — needed for post-event "alpha decay" analysis and
  for debugging anything odd found after an unattended overnight stretch.
- **Basic monitoring/alerting**: a heartbeat check (e.g. a Slack webhook)
  on matching-engine crash/restart or on a product's feed being stuck in
  fallback mode for longer than a configured threshold. Nothing elaborate
  — just enough that a multi-day unattended failure doesn't go unnoticed
  until someone happens to check the site.

---

## 12. Build order

**Phase 0 — foundation**
1. Matching engine + account/position/cash core (no feed dependency yet,
   can be tested with a manually-set mark price).
2. Feed service — live Kraken WS mode first, then staleness/SMA fallback.
3. Bot layer (MM + noise), wired to the feed service's index price.

**Phase 1 — validation checkpoint.** Don't move on to Phase 2 until all
three of these are true and demonstrated, in this priority order:
4. **Website** (order book view, leaderboard) is built and live against
   real feed/engine data — read-only, per §9.
5. **MM bots behave as expected** — visually confirmed on the website:
   quotes track the index price, inventory skew is visible, spreads look
   sane, book has natural depth/shape (not 5 stacked identical quotes).
6. **Admin account exists and can perform its core tasks**: validate/
   activate a student account, trigger a price shock, trigger a bull/bear
   market drift, trigger a BTC/ETH spread widen/invert/converge event (all
   §6.3/§7) — each one verified to visibly move the website's order book
   view.

**Phase 2 — student-facing + remaining admin surface**
7. Public API + rate limiting (§8).
8. Remaining admin API/panel surface not already covered in Phase 1
   (freeze/unfreeze account, bot param tuning, kill orders, feed
   live/replay switch, replay speed) (§7).
9. Student notebook (§10).

**Phase 3 — replay content + hardening**
10. Historical data collection + manipulation script for the live-demo
    replay dataset (§4.3) — can happen in parallel with Phase 0 once the
    feed service's replay interface is defined.
11. Load test, then a full dry run (replay mode with scripted shocks, a
    couple of scripted bot accounts acting as "students") before the
    actual event.

---

## 13. Open decisions to confirm before/during build

- [ ] Buying-power enforcement on/off by default (§3).
- [ ] Freeze-on-zero-equity on/off by default (§3).
- [x] `MAX_POSITION` = **15** contracts per product (tune from load test +
      desired notional-vs-$1,000-balance ratio if needed).
- [ ] `STALE_THRESHOLD_SECONDS`, `SMA_WINDOW`, `RECONNECT_BLEND_SECONDS`
      (§4.2) — reasonable defaults given, tune during testing.
- [x] `SHOCK_DECAY_SECONDS` (§6.3) — reuse the same default as
      `RECONNECT_BLEND_SECONDS`, tune during testing.
- [x] Rate limiting (§8) — 20 req/s per key on REST (burst 40), 1
      persistent WS connection per key.
- [ ] Exact number/timing of scripted shocks in the historical replay
      dataset (§4.3) — depends on how long the live demo segment is.
- [ ] Student roster size / whether teams share an account or each person
      gets one (affects `MAX_POSITION` sizing and total expected load).