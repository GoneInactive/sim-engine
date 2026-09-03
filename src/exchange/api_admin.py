"""Admin API — build-spec.md §7. Separate, more privileged auth from the
student API (a single shared admin password, checked on every request via
the X-Admin-Password header).

Request models are defined at module scope, not nested inside
create_admin_app: with `from __future__ import annotations`, a locally
scoped class name is unresolvable when FastAPI evaluates the string
annotation, and the body param silently gets treated as a query param
instead.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .state import AppState

security = HTTPBasic(auto_error=False)


class RegisterIn(BaseModel):
    account_id: str


class ShockIn(BaseModel):
    product: str
    target_offset: float
    ramp_seconds: float = 1.0
    hold_seconds: float = 5.0
    name: str = "shock"


class DriftIn(BaseModel):
    product: str
    drift: float
    duration_seconds: float
    name: str = "drift"


class SpreadIn(BaseModel):
    kind: str  # widen | invert | converge
    magnitude: float
    ramp_seconds: float = 2.0
    hold_seconds: float = 8.0
    name: str = "spread"


class LiquidityIn(BaseModel):
    product: str
    kind: str  # withdraw | flood
    duration_seconds: float
    magnitude: float = 2.0


class FeedModeIn(BaseModel):
    product: str
    mode: str  # live | replay


class ReplaySpeedIn(BaseModel):
    speed: float


class BotParamsIn(BaseModel):
    base_spread_frac: Optional[float] = None
    quote_size: Optional[int] = None
    active: Optional[bool] = None


class SpawnBotIn(BaseModel):
    product: str
    base_spread_frac: float = 0.004
    quote_size: int = 3
    skew_sensitivity: float = 0.05
    requote_interval: float = 2.0


class SpawnNoiseBotIn(BaseModel):
    product: str
    arrival_rate_per_sec: float = 0.3
    max_size: int = 2


class NoiseBotParamsIn(BaseModel):
    arrival_rate_per_sec: Optional[float] = None
    max_size: Optional[int] = None
    active: Optional[bool] = None


class ArbBotParamsIn(BaseModel):
    threshold_ticks: Optional[float] = None
    correction_qty: Optional[int] = None
    check_interval: Optional[float] = None
    active: Optional[bool] = None


class SyntheticParamsIn(BaseModel):
    product: str
    annual_volatility: Optional[float] = None
    annual_drift: Optional[float] = None


class SpreadScaleIn(BaseModel):
    scale: float


def create_admin_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Mini-Exchange Admin API")

    def admin_auth(
        x_admin_password: Optional[str] = Header(default=None),
        credentials: Optional[HTTPBasicCredentials] = Depends(security),
    ) -> None:
        # Accepts either the X-Admin-Password header (curl / scripts) or
        # HTTP Basic (the browser admin page below) — same password either way.
        if x_admin_password is not None:
            if state.auth.check_admin_password(x_admin_password):
                return
            raise HTTPException(status_code=401, detail="bad admin password")
        if credentials is not None and state.auth.check_admin_password(credentials.password):
            return
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"}
        )

    # -- account management ------------------------------------------------
    @app.post("/accounts", dependencies=[Depends(admin_auth)])
    def register_account(body: RegisterIn):
        key = state.admin_issue_key(body.account_id)
        return {"account_id": body.account_id, "api_key": key, "active": False}

    @app.post("/accounts/{key}/activate", dependencies=[Depends(admin_auth)])
    def activate_account(key: str):
        try:
            record = state.auth.activate(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such key")
        return {"account_id": record.account_id, "active": record.active}

    @app.post("/accounts/{key}/deactivate", dependencies=[Depends(admin_auth)])
    def deactivate_account(key: str):
        try:
            record = state.auth.deactivate(key)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such key")
        return {"account_id": record.account_id, "active": record.active}

    @app.post("/accounts/{account_id}/regenerate_key", dependencies=[Depends(admin_auth)])
    def regenerate_key(account_id: str):
        if account_id not in state.engine.accounts:
            raise HTTPException(status_code=404, detail="no such account")
        record = state.auth.regenerate_key(account_id)
        return {"account_id": record.account_id, "api_key": record.key, "active": record.active}

    @app.post("/accounts/{account_id}/freeze", dependencies=[Depends(admin_auth)])
    def freeze_account(account_id: str):
        account = state.engine.accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="no such account")
        account.frozen = True
        return {"account_id": account_id, "frozen": True}

    @app.post("/accounts/{account_id}/unfreeze", dependencies=[Depends(admin_auth)])
    def unfreeze_account(account_id: str):
        account = state.engine.accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="no such account")
        account.frozen = False
        return {"account_id": account_id, "frozen": False}

    @app.get("/accounts/{account_id}/orders", dependencies=[Depends(admin_auth)])
    def list_account_orders(account_id: str):
        return [
            {"id": o.id, "product": o.product, "side": o.side.value, "status": o.status.value, "remaining_qty": o.remaining_qty}
            for o in state.engine.orders_by_account.get(account_id, {}).values()
        ]

    @app.delete("/accounts/{account_id}/orders", dependencies=[Depends(admin_auth)])
    def kill_account_orders(account_id: str):
        killed = state.engine.kill_account_orders(account_id)
        return {"killed": [o.id for o in killed]}

    # -- market events (§6.3) ------------------------------------------------
    @app.post("/events/shock", dependencies=[Depends(admin_auth)])
    def trigger_shock(body: ShockIn):
        event = state.index_service.trigger_price_shock(
            body.product, body.target_offset, time.time(), body.ramp_seconds, body.hold_seconds, body.name
        )
        return {"name": event.name, "product": event.product}

    @app.post("/events/drift", dependencies=[Depends(admin_auth)])
    def trigger_drift(body: DriftIn):
        event = state.index_service.trigger_bull_bear(
            body.product, body.drift, body.duration_seconds, time.time(), body.name
        )
        return {"name": event.name, "product": event.product}

    @app.post("/events/spread", dependencies=[Depends(admin_auth)])
    def trigger_spread(body: SpreadIn):
        try:
            btc_event, eth_event = state.index_service.trigger_spread_event(
                body.kind, body.magnitude, time.time(), ramp_seconds=body.ramp_seconds, hold_seconds=body.hold_seconds, name=body.name
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"name": body.name, "products": [btc_event.product, eth_event.product]}

    @app.post("/events/liquidity", dependencies=[Depends(admin_auth)])
    def trigger_liquidity(body: LiquidityIn):
        try:
            state.bot_manager.trigger_liquidity_event(
                body.product, body.kind, body.duration_seconds, time.time(), body.magnitude
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"product": body.product, "kind": body.kind}

    # -- feed control (§7) ---------------------------------------------------
    @app.post("/feed/mode", dependencies=[Depends(admin_auth)])
    def set_feed_mode(body: FeedModeIn):
        if body.mode not in ("live", "replay"):
            raise HTTPException(status_code=400, detail="mode must be 'live' or 'replay'")
        state.feed_mode[body.product] = body.mode
        return {"product": body.product, "mode": body.mode}

    @app.post("/feed/replay_speed", dependencies=[Depends(admin_auth)])
    def set_replay_speed(body: ReplaySpeedIn):
        state.replay_speed = body.speed
        return {"speed": body.speed}

    # -- bot params (§7) ------------------------------------------------------
    @app.post("/bots/{account_id}/params", dependencies=[Depends(admin_auth)])
    def set_bot_params(account_id: str, body: BotParamsIn):
        for bot in state.bot_manager.mm_bots:
            if bot.config.account_id == account_id:
                if body.base_spread_frac is not None:
                    bot.config.base_spread_frac = body.base_spread_frac
                if body.quote_size is not None:
                    bot.config.quote_size = body.quote_size
                if body.active is not None:
                    bot.config.active = body.active
                return {"account_id": account_id, "config": bot.config.__dict__}
        raise HTTPException(status_code=404, detail="no such MM bot")

    @app.post("/bots", dependencies=[Depends(admin_auth)])
    def spawn_bot(body: SpawnBotIn):
        if body.product not in state.config.products:
            raise HTTPException(status_code=400, detail="unknown product")
        bot = state.bot_manager.spawn_mm_bot(
            body.product, body.base_spread_frac, body.quote_size, body.skew_sensitivity, body.requote_interval
        )
        return {"account_id": bot.config.account_id, "product": bot.product, "config": bot.config.__dict__}

    @app.get("/bots", dependencies=[Depends(admin_auth)])
    def list_bots():
        return [
            {"account_id": b.config.account_id, "product": b.product, "config": b.config.__dict__}
            for b in state.bot_manager.mm_bots
        ]

    @app.post("/bots/spread_scale", dependencies=[Depends(admin_auth)])
    def set_spread_scale(body: SpreadScaleIn):
        if body.scale <= 0:
            raise HTTPException(status_code=400, detail="scale must be positive")
        state.bot_manager.global_spread_scale = body.scale
        return {"global_spread_scale": state.bot_manager.global_spread_scale}

    @app.get("/bots/spread_scale", dependencies=[Depends(admin_auth)])
    def get_spread_scale():
        return {"global_spread_scale": state.bot_manager.global_spread_scale}

    @app.post("/synthetic/params", dependencies=[Depends(admin_auth)])
    def set_synthetic_params(body: SyntheticParamsIn):
        if body.product not in state.config.products:
            raise HTTPException(status_code=400, detail="unknown product")
        return state.set_synthetic_params(body.product, body.annual_volatility, body.annual_drift)

    @app.get("/synthetic/params", dependencies=[Depends(admin_auth)])
    def get_synthetic_params():
        return state.synthetic_params

    @app.post("/bots/noise", dependencies=[Depends(admin_auth)])
    def spawn_noise_bot(body: SpawnNoiseBotIn):
        if body.product not in state.config.products:
            raise HTTPException(status_code=400, detail="unknown product")
        bot = state.bot_manager.spawn_noise_bot(body.product, body.arrival_rate_per_sec, body.max_size)
        return {"account_id": bot.config.account_id, "product": bot.product, "config": bot.config.__dict__}

    @app.get("/bots/noise", dependencies=[Depends(admin_auth)])
    def list_noise_bots():
        return [
            {"account_id": b.config.account_id, "product": b.product, "config": b.config.__dict__}
            for b in state.bot_manager.noise_bots
        ]

    @app.post("/bots/noise/{account_id}/params", dependencies=[Depends(admin_auth)])
    def set_noise_bot_params(account_id: str, body: NoiseBotParamsIn):
        for bot in state.bot_manager.noise_bots:
            if bot.config.account_id == account_id:
                if body.arrival_rate_per_sec is not None:
                    bot.config.arrival_rate_per_sec = body.arrival_rate_per_sec
                if body.max_size is not None:
                    bot.config.max_size = body.max_size
                if body.active is not None:
                    bot.config.active = body.active
                return {"account_id": account_id, "config": bot.config.__dict__}
        raise HTTPException(status_code=404, detail="no such noise bot")

    @app.get("/bots/arb", dependencies=[Depends(admin_auth)])
    def list_arb_bots():
        return [
            {"account_id": b.config.account_id, "product": b.product, "config": b.config.__dict__}
            for b in state.bot_manager.arb_bots
        ]

    @app.post("/bots/arb/{account_id}/params", dependencies=[Depends(admin_auth)])
    def set_arb_bot_params(account_id: str, body: ArbBotParamsIn):
        for bot in state.bot_manager.arb_bots:
            if bot.config.account_id == account_id:
                if body.threshold_ticks is not None:
                    bot.config.threshold_ticks = body.threshold_ticks
                if body.correction_qty is not None:
                    bot.config.correction_qty = body.correction_qty
                if body.check_interval is not None:
                    bot.config.check_interval = body.check_interval
                if body.active is not None:
                    bot.config.active = body.active
                return {"account_id": account_id, "config": bot.config.__dict__}
        raise HTTPException(status_code=404, detail="no such arb bot")

    @app.get("/accounts", dependencies=[Depends(admin_auth)])
    def list_accounts():
        return [
            {"account_id": r.account_id, "api_key": r.key, "active": r.active}
            for r in state.auth.list_keys()
        ]

    @app.get("/market/{product}", dependencies=[Depends(admin_auth)])
    def market_snapshot(product: str):
        if product not in state.config.products:
            raise HTTPException(status_code=404, detail="unknown product")
        return state.market_snapshot(product)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin_auth)])
    def admin_page():
        products = list(state.config.products.keys())
        product_options = "".join(f'<option value="{p}">{p}</option>' for p in products)
        market_cols = "".join(
            f"""
<div class="col">
<h3>{p}</h3>
<div class="metrics" id="mkt-metrics-{p}"></div>
<div style="font-size:11px;">chart: book midpoint</div>
<svg class="spark" id="mkt-spark-{p}" width="280" height="40" viewBox="0 0 280 40" preserveAspectRatio="none"></svg>
</div>"""
            for p in products
        )
        return (
            ADMIN_PAGE.replace("__PRODUCT_OPTIONS__", product_options)
            .replace("__MARKET_COLS__", market_cols)
            .replace("__PRODUCTS_JSON__", str(products))
            .replace("__WEBSITE_URL__", state.config.network.website_base_url)
        )

    return app


ADMIN_PAGE = """<!doctype html>
<html>
<head>
<title>Mini-Exchange Admin</title>
<style>
  * { box-sizing:border-box; }
  body { background:#fff; color:#000; font-family: ui-monospace, monospace; margin:0; padding:24px; max-width:1000px; }
  h1 { margin:0 0 16px; }
  h2 { margin:32px 0 8px; border-bottom:1px solid #000; padding-bottom:4px; }
  .row { display:flex; gap:24px; flex-wrap:wrap; }
  fieldset { border:1px solid #000; padding:12px; min-width:260px; flex:1; }
  legend { padding:0 6px; }
  label { display:block; margin-top:8px; font-size:13px; }
  input, select { width:100%; padding:4px; border:1px solid #000; background:#fff; color:#000; font-family:inherit; }
  button { margin-top:10px; padding:6px 12px; border:1px solid #000; background:#000; color:#fff; cursor:pointer; font-family:inherit; }
  button:hover { background:#333; }
  pre { background:#f2f2f2; border:1px solid #000; padding:8px; white-space:pre-wrap; word-break:break-all; font-size:12px; }
  table { border-collapse:collapse; width:100%; margin-top:8px; }
  th, td { text-align:left; padding:4px 8px; border-bottom:1px solid #000; font-size:13px; }
  .cols { display:flex; gap:32px; flex-wrap:wrap; }
  .col { flex:1; min-width:300px; }
  .metrics { display:grid; grid-template-columns:1fr 1fr; gap:2px 16px; font-size:13px; margin-bottom:6px; }
  .metrics div span { font-weight:600; }
  svg.spark { border:1px solid #000; }
  .stale { font-weight:bold; }
  nav a { color:#000; text-decoration:none; margin-right:20px; border-bottom:1px solid #000; }
</style>
</head>
<body>
<h1>Mini-Exchange Admin</h1>
<nav><a href="__WEBSITE_URL__" target="_blank">Website</a></nav>

<h2>Market</h2>
<div class="cols">__MARKET_COLS__</div>

<h2>Accounts</h2>
<div class="row">
  <fieldset>
    <legend>Register account</legend>
    <label>Account ID<input id="reg-id"></label>
    <button onclick="registerAccount()">Register</button>
  </fieldset>
  <fieldset>
    <legend>Activate / deactivate key</legend>
    <label>API key<input id="act-key"></label>
    <button onclick="activateKey()">Activate</button>
    <button onclick="deactivateKey()">Deactivate</button>
  </fieldset>
  <fieldset>
    <legend>Regenerate key</legend>
    <label>Account ID<input id="regen-id"></label>
    <button onclick="regenerateKey()">Regenerate</button>
  </fieldset>
  <fieldset>
    <legend>Freeze / unfreeze / kill orders</legend>
    <label>Account ID<input id="fz-id"></label>
    <button onclick="freezeAccount()">Freeze</button>
    <button onclick="unfreezeAccount()">Unfreeze</button>
    <button onclick="killOrders()">Kill orders</button>
  </fieldset>
</div>
<button onclick="loadAccounts()">Refresh account list</button>
<table><thead><tr><th>Account</th><th>API key</th><th>Active</th></tr></thead><tbody id="accounts-table"></tbody></table>

<h2>Market events</h2>
<div class="row">
  <fieldset>
    <legend>Price shock</legend>
    <label>Product<select id="shock-product">__PRODUCT_OPTIONS__</select></label>
    <label>Target offset ($)<input id="shock-offset" value="5"></label>
    <label>Ramp seconds<input id="shock-ramp" value="1"></label>
    <label>Hold seconds<input id="shock-hold" value="5"></label>
    <button onclick="triggerShock()">Trigger shock</button>
  </fieldset>
  <fieldset>
    <legend>Bull / bear drift</legend>
    <label>Product<select id="drift-product">__PRODUCT_OPTIONS__</select></label>
    <label>Drift ($, negative = bear)<input id="drift-amount" value="20"></label>
    <label>Duration seconds<input id="drift-duration" value="30"></label>
    <button onclick="triggerDrift()">Trigger drift</button>
  </fieldset>
  <fieldset>
    <legend>BTC/ETH spread</legend>
    <label>Kind
      <select id="spread-kind"><option>widen</option><option>invert</option><option>converge</option></select>
    </label>
    <label>Magnitude ($ or 0..1 for converge)<input id="spread-magnitude" value="5"></label>
    <button onclick="triggerSpread()">Trigger spread event</button>
  </fieldset>
  <fieldset>
    <legend>Liquidity event</legend>
    <label>Product<select id="liq-product">__PRODUCT_OPTIONS__</select></label>
    <label>Kind<select id="liq-kind"><option>withdraw</option><option>flood</option></select></label>
    <label>Duration seconds<input id="liq-duration" value="20"></label>
    <label>Magnitude<input id="liq-magnitude" value="3"></label>
    <button onclick="triggerLiquidity()">Trigger liquidity event</button>
  </fieldset>
</div>

<h2>Feed control</h2>
<div class="row">
  <fieldset>
    <legend>Feed mode</legend>
    <label>Product<select id="feed-product">__PRODUCT_OPTIONS__</select></label>
    <label>Mode<select id="feed-mode"><option>live</option><option>replay</option></select></label>
    <button onclick="setFeedMode()">Set mode</button>
  </fieldset>
  <fieldset>
    <legend>Replay speed</legend>
    <label>Speed multiplier<input id="replay-speed" value="1.0"></label>
    <button onclick="setReplaySpeed()">Set speed</button>
  </fieldset>
  <fieldset>
    <legend>Synthetic price volatility</legend>
    <label>Product<select id="vol-product">__PRODUCT_OPTIONS__</select></label>
    <label>Annual volatility (e.g. 0.55)<input id="vol-value"></label>
    <label>Annual drift (blank = unchanged)<input id="drift-value"></label>
    <button onclick="setSyntheticParams()">Set</button>
  </fieldset>
  <fieldset>
    <legend>MM spread tightness (global)</legend>
    <label>Scale (1.0 = normal, &lt;1 tighter, &gt;1 wider)<input id="spread-scale-value" value="1.0"></label>
    <button onclick="setSpreadScale()">Set</button>
  </fieldset>
</div>

<h2>Market maker bots</h2>
<div class="row">
  <fieldset>
    <legend>Spawn MM bot</legend>
    <label>Product<select id="spawn-mm-product">__PRODUCT_OPTIONS__</select></label>
    <label>Base spread frac<input id="spawn-mm-spread" value="0.004"></label>
    <label>Quote size<input id="spawn-mm-size" value="3"></label>
    <label>Skew sensitivity<input id="spawn-mm-skew" value="0.05"></label>
    <label>Requote interval (s)<input id="spawn-mm-interval" value="2.0"></label>
    <button onclick="spawnMmBot()">Spawn MM bot</button>
  </fieldset>
  <fieldset>
    <legend>Adjust MM bot</legend>
    <label>Bot account ID (e.g. mm_BTC-MINI_0)<input id="bot-id"></label>
    <label>Base spread frac (blank = unchanged)<input id="bot-spread"></label>
    <label>Quote size (blank = unchanged)<input id="bot-size"></label>
    <label>Active<select id="bot-active"><option value="">unchanged</option><option value="true">on</option><option value="false">off</option></select></label>
    <button onclick="setBotParams()">Update bot</button>
  </fieldset>
</div>
<button onclick="loadBots()">Refresh MM bot list</button>
<table><thead><tr><th>Account</th><th>Product</th><th>Spread frac</th><th>Size</th><th>Active</th><th></th></tr></thead><tbody id="bots-table"></tbody></table>

<h2>Liquidity-taking (noise) bots</h2>
<div class="row">
  <fieldset>
    <legend>Spawn noise bot</legend>
    <label>Product<select id="spawn-noise-product">__PRODUCT_OPTIONS__</select></label>
    <label>Arrival rate (per sec)<input id="spawn-noise-rate" value="0.3"></label>
    <label>Max size<input id="spawn-noise-maxsize" value="2"></label>
    <button onclick="spawnNoiseBot()">Spawn noise bot</button>
  </fieldset>
</div>
<button onclick="loadNoiseBots()">Refresh noise bot list</button>
<table><thead><tr><th>Account</th><th>Product</th><th>Arrival rate/s</th><th>Max size</th><th>Active</th><th></th></tr></thead><tbody id="noise-bots-table"></tbody></table>

<h2>Arb bot</h2>
<p style="font-size:13px;">Unlimited cash, no position cap. Steps in with a market order whenever the book drifts more than
<code>threshold_ticks</code> from the index — a backstop so a well-funded student can't hold the traded price away
from fair value. One spawns per product automatically.</p>
<button onclick="loadArbBots()">Refresh arb bot list</button>
<table><thead><tr><th>Account</th><th>Product</th><th>Threshold (ticks)</th><th>Correction qty</th><th>Active</th><th></th></tr></thead><tbody id="arb-bots-table"></tbody></table>

<h2>Result</h2>
<pre id="result">no result yet</pre>

<script>
function val(id) { return document.getElementById(id).value; }

function requireVal(id, label) {
  const v = val(id);
  if (!v) {
    document.getElementById('result').textContent = 'enter ' + label + ' first';
    return null;
  }
  return v;
}

async function callApi(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const text = await res.text();
  document.getElementById('result').textContent = res.status + ' ' + text;
  return res;
}

function registerAccount() {
  const id = requireVal('reg-id', 'an account ID');
  if (!id) return;
  callApi('POST', '/accounts', { account_id: id }).then(loadAccounts);
}

function activateKey() {
  const key = requireVal('act-key', 'an API key');
  if (key) callApi('POST', '/accounts/' + key + '/activate').then(loadAccounts);
}
function deactivateKey() {
  const key = requireVal('act-key', 'an API key');
  if (key) callApi('POST', '/accounts/' + key + '/deactivate').then(loadAccounts);
}
function regenerateKey() {
  const id = requireVal('regen-id', 'an account ID');
  if (id) callApi('POST', '/accounts/' + id + '/regenerate_key').then(loadAccounts);
}
function freezeAccount() {
  const id = requireVal('fz-id', 'an account ID');
  if (id) callApi('POST', '/accounts/' + id + '/freeze');
}
function unfreezeAccount() {
  const id = requireVal('fz-id', 'an account ID');
  if (id) callApi('POST', '/accounts/' + id + '/unfreeze');
}
function killOrders() {
  const id = requireVal('fz-id', 'an account ID');
  if (id) callApi('DELETE', '/accounts/' + id + '/orders');
}

function pollMarket(product) {
  poll('/market/' + product, (d) => {
    const metrics = document.getElementById('mkt-metrics-' + product);
    const fmt = (v) => v === null || v === undefined ? 'n/a' : v.toFixed(2);
    metrics.innerHTML =
      `<div>index <span>$${fmt(d.index_price)}</span></div>` +
      `<div>mid <span>$${fmt(d.mid)}</span></div>` +
      `<div>spread <span>${d.spread_bps === null ? 'n/a' : d.spread_bps.toFixed(1) + ' bps'}</span></div>` +
      `<div>last trade <span>$${fmt(d.last_trade)} x ${d.last_trade_qty ?? 'n/a'}</span></div>` +
      `<div>last trade time <span>${timeSince(d.last_trade_ts)}</span></div>` +
      `<div>session volume <span>${d.session_volume_qty} ct ($${d.session_volume_notional.toFixed(2)})</span></div>` +
      (d.stale ? '<div class="stale">STALE</div>' : '');
    const spark = document.getElementById('mkt-spark-' + product);
    const path = sparklinePath(d.sparkline, 280, 40);
    spark.innerHTML = path ? `<path d="${path}" fill="none" stroke="#000" stroke-width="1.5"/>` : '';
  }, 1000);
}
function sparklinePath(values, w, h) {
  if (!values.length) return '';
  const min = Math.min(...values), max = Math.max(...values);
  const range = (max - min) || 1;
  const step = w / Math.max(1, values.length - 1);
  return values.map((v, i) => {
    const x = (i * step).toFixed(1);
    const y = (h - ((v - min) / range) * h).toFixed(1);
    return (i === 0 ? 'M' : 'L') + x + ',' + y;
  }).join(' ');
}
function timeSince(ts) {
  if (ts === null || ts === undefined) return 'n/a';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}
async function poll(url, render, ms) {
  async function tick() {
    try { render(await (await fetch(url)).json()); } catch (e) {}
    setTimeout(tick, ms);
  }
  tick();
}
for (const p of __PRODUCTS_JSON__) { pollMarket(p); }

async function loadAccounts() {
  const res = await fetch('/accounts');
  const rows = await res.json();
  document.getElementById('accounts-table').innerHTML = rows.map(r =>
    `<tr><td>${r.account_id}</td><td>${r.api_key}</td><td>${r.active}</td></tr>`
  ).join('');
}

async function loadBots() {
  const res = await fetch('/bots');
  const rows = await res.json();
  document.getElementById('bots-table').innerHTML = rows.map(r =>
    `<tr><td>${r.account_id}</td><td>${r.product}</td><td>${r.config.base_spread_frac}</td>` +
    `<td>${r.config.quote_size}</td><td>${r.config.active}</td>` +
    `<td><button onclick="toggleMmBot('${r.account_id}', ${!r.config.active})">${r.config.active ? 'Turn off' : 'Turn on'}</button></td></tr>`
  ).join('');
}

function toggleMmBot(accountId, newActive) {
  callApi('POST', '/bots/' + accountId + '/params', { active: newActive }).then(loadBots);
}

function spawnMmBot() {
  callApi('POST', '/bots', {
    product: val('spawn-mm-product'),
    base_spread_frac: parseFloat(val('spawn-mm-spread')),
    quote_size: parseInt(val('spawn-mm-size')),
    skew_sensitivity: parseFloat(val('spawn-mm-skew')),
    requote_interval: parseFloat(val('spawn-mm-interval')),
  }).then(loadBots);
}

async function loadNoiseBots() {
  const res = await fetch('/bots/noise');
  const rows = await res.json();
  document.getElementById('noise-bots-table').innerHTML = rows.map(r =>
    `<tr><td>${r.account_id}</td><td>${r.product}</td><td>${r.config.arrival_rate_per_sec}</td>` +
    `<td>${r.config.max_size}</td><td>${r.config.active}</td>` +
    `<td><button onclick="toggleNoiseBot('${r.account_id}', ${!r.config.active})">${r.config.active ? 'Turn off' : 'Turn on'}</button></td></tr>`
  ).join('');
}

function toggleNoiseBot(accountId, newActive) {
  callApi('POST', '/bots/noise/' + accountId + '/params', { active: newActive }).then(loadNoiseBots);
}

async function loadArbBots() {
  const res = await fetch('/bots/arb');
  const rows = await res.json();
  document.getElementById('arb-bots-table').innerHTML = rows.map(r =>
    `<tr><td>${r.account_id}</td><td>${r.product}</td><td>${r.config.threshold_ticks}</td>` +
    `<td>${r.config.correction_qty}</td><td>${r.config.active}</td>` +
    `<td><button onclick="toggleArbBot('${r.account_id}', ${!r.config.active})">${r.config.active ? 'Turn off' : 'Turn on'}</button></td></tr>`
  ).join('');
}

function toggleArbBot(accountId, newActive) {
  callApi('POST', '/bots/arb/' + accountId + '/params', { active: newActive }).then(loadArbBots);
}

function spawnNoiseBot() {
  callApi('POST', '/bots/noise', {
    product: val('spawn-noise-product'),
    arrival_rate_per_sec: parseFloat(val('spawn-noise-rate')),
    max_size: parseInt(val('spawn-noise-maxsize')),
  }).then(loadNoiseBots);
}

function triggerShock() {
  callApi('POST', '/events/shock', {
    product: val('shock-product'),
    target_offset: parseFloat(val('shock-offset')),
    ramp_seconds: parseFloat(val('shock-ramp')),
    hold_seconds: parseFloat(val('shock-hold')),
  });
}

function triggerDrift() {
  callApi('POST', '/events/drift', {
    product: val('drift-product'),
    drift: parseFloat(val('drift-amount')),
    duration_seconds: parseFloat(val('drift-duration')),
  });
}

function triggerSpread() {
  callApi('POST', '/events/spread', {
    kind: val('spread-kind'),
    magnitude: parseFloat(val('spread-magnitude')),
  });
}

function triggerLiquidity() {
  callApi('POST', '/events/liquidity', {
    product: val('liq-product'),
    kind: val('liq-kind'),
    duration_seconds: parseFloat(val('liq-duration')),
    magnitude: parseFloat(val('liq-magnitude')),
  });
}

function setFeedMode() {
  callApi('POST', '/feed/mode', { product: val('feed-product'), mode: val('feed-mode') });
}

function setReplaySpeed() {
  callApi('POST', '/feed/replay_speed', { speed: parseFloat(val('replay-speed')) });
}

function setSyntheticParams() {
  const body = { product: val('vol-product') };
  const vol = val('vol-value');
  const drift = val('drift-value');
  if (vol) body.annual_volatility = parseFloat(vol);
  if (drift) body.annual_drift = parseFloat(drift);
  callApi('POST', '/synthetic/params', body);
}

function setSpreadScale() {
  callApi('POST', '/bots/spread_scale', { scale: parseFloat(val('spread-scale-value')) });
}

function setBotParams() {
  const body = {};
  if (val('bot-spread')) body.base_spread_frac = parseFloat(val('bot-spread'));
  if (val('bot-size')) body.quote_size = parseInt(val('bot-size'));
  if (val('bot-active')) body.active = val('bot-active') === 'true';
  callApi('POST', '/bots/' + val('bot-id') + '/params', body).then(loadBots);
}

loadAccounts();
loadBots();
loadNoiseBots();
loadArbBots();
</script>
</body>
</html>"""
