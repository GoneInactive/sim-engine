"""Website — build-spec.md §9, extended with click-to-trade.

Pages are viewable by anyone with the URL — no site-wide password. The
build-spec's original "single shared password is fine" framing predates
per-account login; once every student registers their own username/
password (self-serve, no admin approval step) via the account bar shown
on every page, a second shared site password was redundant friction on
top of it rather than adding real protection. That login's API key is
stored in the browser and used to call the public trading API directly
from JS — also a departure from the spec's original "read-only site"
framing, per an explicit request to make the ladder tradable.

Trading calls go straight from the browser to the public API's own origin
(a different port), not through this backend, so CORS is enabled there
(see api_public.py) rather than proxied through here. Read-only data
(book, leaderboard, portfolio) still renders from the shared AppState
directly, no HTTP hop needed since this runs in the same process.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .auth import AccountExistsError
from .state import AppState


class RegisterIn(BaseModel):
    account_id: str
    password: str


PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<title>Mini-Exchange</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ background:#fff; color:#000; font-family: ui-monospace, monospace; margin:0; padding:24px; }}
  h1, h2 {{ color:#000; font-weight:600; margin:24px 0 8px; }}
  nav a {{ color:#000; text-decoration:none; margin-right:20px; border-bottom:1px solid #000; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:12px; }}
  th, td {{ text-align:left; padding:3px 10px; border-bottom:1px solid #000; font-size:13px; }}
  th {{ font-weight:600; }}
  .meta {{ margin-bottom:8px; font-size:13px; }}
  .stale {{ font-weight:bold; }}
  .cols {{ display:flex; gap:32px; flex-wrap:wrap; }}
  .col {{ flex:1; min-width:420px; }}
  .metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:2px 16px; font-size:13px; margin-bottom:8px; }}
  .metrics div span {{ font-weight:600; }}
  svg.chart {{ border:1px solid #000; cursor:crosshair; }}
  input {{ font-family:inherit; border:1px solid #000; padding:3px 5px; }}
  button {{ font-family:inherit; border:1px solid #000; background:#000; color:#fff; padding:3px 10px; cursor:pointer; }}
  button:hover {{ background:#333; }}
  .topbar {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; border-bottom:1px solid #000; padding-bottom:10px; margin-bottom:16px; }}
  .topbar nav {{ border-bottom:none; }}
  #account-bar {{ font-size:13px; text-align:right; }}
  #account-bar span.err {{ color:#b00020; }}
  #fill-banners {{ position:fixed; top:70px; right:16px; display:flex; flex-direction:column; gap:8px; z-index:1000; }}
  .fill-banner {{ background:#000; color:#fff; padding:10px 16px; font-size:13px; border:1px solid #000; max-width:320px; }}
  .fill-banner .side-buy {{ color:#7dffb0; font-weight:600; }}
  .fill-banner .side-sell {{ color:#ff9d9d; font-weight:600; }}
  .ladder {{ width:auto; min-width:320px; }}
  .ladder td, .ladder th {{ text-align:center; padding:2px 10px; }}
  .ladder td.price {{ font-weight:600; border-left:1px solid #000; border-right:1px solid #000; cursor:default; }}
  .ladder td.bid {{ background:#bcd6ff; cursor:pointer; }}
  .ladder td.bid.filled {{ background:#eaf3ff; }}
  .ladder td.ask {{ background:#ffc7c7; cursor:pointer; }}
  .ladder td.ask.filled {{ background:#ffefef; }}
  .ladder td.working {{ font-weight:600; cursor:pointer; }}
  .ladder td.working.own-bid, .ladder td.working.own-ask {{ user-select:none; -webkit-user-drag:element; }}
  .ladder td.working.own-bid {{ background:#5b9bff; color:#fff; cursor:grab; }}
  .ladder td.working.own-ask {{ background:#ff6b6b; color:#fff; cursor:grab; }}
  .ladder td.working.drag-over {{ outline:2px dashed #000; outline-offset:-2px; }}
  .ladder td.last-buy {{ background:#b6f2c0 !important; }}
  .ladder td.last-sell {{ background:#ffb3b3 !important; }}
  .ladder tbody tr:hover td {{ filter:brightness(0.96); }}
  .ladder-scroll {{ overflow-y:auto; border:1px solid #000; }}
</style>
</head>
<body>
<script>
// Defined before any page content: per-product inline <script> blocks
// below call these immediately as they're parsed, so definitions must
// come first — script tags run in document order.
async function poll(url, render, ms) {{
  async function tick() {{
    try {{ render(await (await fetch(url)).json()); }} catch (e) {{}}
    setTimeout(tick, ms);
  }}
  tick();
}}
function timeSince(ts) {{
  if (ts === null || ts === undefined) return 'n/a';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}}

// -- shared login (used by the ladder and the portfolio page) --------------
const API_BASE = {api_base_url!r};
function getKey() {{ return localStorage.getItem('exchange-api-key'); }}
function getAccountId() {{ return localStorage.getItem('exchange-account-id'); }}
function setSession(accountId, key) {{
  localStorage.setItem('exchange-api-key', key);
  localStorage.setItem('exchange-account-id', accountId);
  lastSeenFillId = null; // reseed fill tracking for the (possibly new) account
}}
function clearSession() {{
  localStorage.removeItem('exchange-api-key');
  localStorage.removeItem('exchange-account-id');
  lastSeenFillId = null;
}}

// -- fill notifications: banner + sound, on every page ----------------------
// Seeded to the current max fill id on login/page-load so a student isn't
// flooded with notifications for fills that happened before this session.
let lastSeenFillId = null;
function playFillSound() {{
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  }} catch (e) {{}}
}}
function showFillBanner(f) {{
  const container = document.getElementById('fill-banners');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'fill-banner';
  const sideClass = f.side === 'buy' ? 'side-buy' : 'side-sell';
  el.innerHTML = `Filled <span class="${{sideClass}}">${{f.side.toUpperCase()}}</span> ${{f.qty}} ${{f.product}} ` +
    `@ $${{f.price.toFixed(2)}} (${{f.role}}, fee $${{f.fee.toFixed(4)}})`;
  container.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}}
async function pollFills() {{
  const key = getKey();
  if (!key) {{
    lastSeenFillId = null;
  }} else {{
    try {{
      const r = await fetch(API_BASE + '/fills', {{ headers: {{'X-API-Key': key}} }});
      if (r.ok) {{
        const fills = await r.json();
        if (lastSeenFillId === null) {{
          // first look at this account this session: seed silently, don't
          // notify for fills that already happened before now.
          lastSeenFillId = fills.length ? Math.max(...fills.map(f => f.id)) : 0;
        }} else {{
          const newOnes = fills.filter(f => f.id > lastSeenFillId).sort((a, b) => a.id - b.id);
          for (const f of newOnes) {{
            showFillBanner(f);
            playFillSound();
            lastSeenFillId = Math.max(lastSeenFillId, f.id);
          }}
        }}
      }}
    }} catch (e) {{}}
  }}
  setTimeout(pollFills, 1500);
}}
pollFills();

function renderAccountBar() {{
  const bar = document.getElementById('account-bar');
  if (!bar) return;
  const key = getKey(), accountId = getAccountId();
  if (key) {{
    bar.innerHTML = `Logged in as <b>${{accountId}}</b> &nbsp; balance: <span id="ab-balance">...</span> &nbsp; ` +
      `<button onclick="logout()">Log out</button>`;
    pollBalance();
  }} else {{
    bar.innerHTML =
      `Username <input id="ab-user" size="12"> Password <input id="ab-pass" type="password" size="12"> ` +
      `<button onclick="doRegister()">Register</button> <button onclick="doLogin()">Log in</button> ` +
      `<span id="ab-msg"></span>`;
  }}
}}
async function pollBalance() {{
  const key = getKey();
  if (!key) return;
  try {{
    const r = await fetch(API_BASE + '/account', {{ headers: {{ 'X-API-Key': key }} }});
    if (r.status === 401) {{
      // Session no longer resolves server-side (e.g. the server restarted —
      // accounts are in-memory only) — drop back to the login form instead
      // of silently spinning forever.
      clearSession();
      renderAccountBar();
      if (window.onLogin) window.onLogin();
      const msg = document.getElementById('ab-msg');
      if (msg) msg.innerHTML = '<span class="err">session expired, please log in again</span>';
      return;
    }}
    if (!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('ab-balance');
    if (el) el.textContent = '$' + d.balance.toFixed(2);
  }} catch (e) {{}}
  setTimeout(pollBalance, 3000);
}}
async function doRegister() {{
  const account_id = document.getElementById('ab-user').value;
  const password = document.getElementById('ab-pass').value;
  if (!account_id || !password) {{
    document.getElementById('ab-msg').innerHTML = '<span class="err">enter a username and password</span>';
    return;
  }}
  const r = await fetch(API_BASE + '/register', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{account_id, password}}),
  }});
  const d = await r.json();
  if (!r.ok) {{
    document.getElementById('ab-msg').innerHTML = `<span class="err">${{d.detail}}</span>`;
    return;
  }}
  setSession(account_id, d.api_key);
  renderAccountBar();
  if (window.onLogin) window.onLogin();
}}
async function doLogin() {{
  const account_id = document.getElementById('ab-user').value;
  const password = document.getElementById('ab-pass').value;
  const r = await fetch(API_BASE + '/login', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{account_id, password}}),
  }});
  const d = await r.json();
  if (!r.ok) {{
    document.getElementById('ab-msg').innerHTML = `<span class="err">${{d.detail}}</span>`;
    return;
  }}
  setSession(account_id, d.api_key);
  renderAccountBar();
  if (window.onLogin) window.onLogin();
}}
function logout() {{
  clearSession();
  renderAccountBar();
  if (window.onLogin) window.onLogin();
}}

// -- chart: bigger, with y-axis, high/low markers, and mouse hover ---------
const chartState = {{}};
function renderChart(product, values) {{
  const svg = document.getElementById('chart-' + product);
  if (!svg) return;
  const w = 480, h = 140, marginLeft = 50, marginTop = 10, marginBottom = 10, marginRight = 10;
  const plotW = w - marginLeft - marginRight, plotH = h - marginTop - marginBottom;
  if (!values || values.length < 2) {{
    svg.innerHTML = `<text x="10" y="${{h / 2}}" font-size="11">not enough data yet</text>`;
    chartState[product] = null;
    return;
  }}
  const min = Math.min(...values), max = Math.max(...values);
  const range = (max - min) || 1;
  const y = (v) => marginTop + plotH * (1 - (v - min) / range);
  const x = (i) => marginLeft + plotW * (i / (values.length - 1));

  let hi = 0, lo = 0;
  values.forEach((v, i) => {{ if (v > values[hi]) hi = i; if (v < values[lo]) lo = i; }});

  let grid = '';
  const mid = (max + min) / 2;
  [max, mid, min].forEach((val) => {{
    const yy = y(val).toFixed(1);
    grid += `<line x1="${{marginLeft}}" y1="${{yy}}" x2="${{w - marginRight}}" y2="${{yy}}" stroke="#e5e5e5"/>`;
    grid += `<text x="2" y="${{(+yy + 3)}}" font-size="10">${{val.toFixed(2)}}</text>`;
  }});

  const path = values.map((v, i) => (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ',' + y(v).toFixed(1)).join(' ');
  const markers = `
    <circle cx="${{x(hi).toFixed(1)}}" cy="${{y(values[hi]).toFixed(1)}}" r="3" fill="#0a7d2c"/>
    <text x="${{x(hi).toFixed(1)}}" y="${{(y(values[hi]) - 6).toFixed(1)}}" font-size="10" text-anchor="middle">${{values[hi].toFixed(2)}}</text>
    <circle cx="${{x(lo).toFixed(1)}}" cy="${{y(values[lo]).toFixed(1)}}" r="3" fill="#b00020"/>
    <text x="${{x(lo).toFixed(1)}}" y="${{(y(values[lo]) + 12).toFixed(1)}}" font-size="10" text-anchor="middle">${{values[lo].toFixed(2)}}</text>
  `;

  svg.innerHTML = grid +
    `<path d="${{path}}" fill="none" stroke="#000" stroke-width="1.5"/>` +
    markers +
    `<g id="hover-${{product}}" style="display:none">
       <line y1="${{marginTop}}" y2="${{marginTop + plotH}}" stroke="#999" stroke-dasharray="2,2"/>
       <circle r="3" fill="#000"/>
       <rect class="tt-bg" width="76" height="16" fill="#fff" stroke="#000"/>
       <text class="tt-text" font-size="10"></text>
     </g>`;

  chartState[product] = {{ values, marginLeft, marginTop, plotW, plotH, min, range, w }};
}}
function onChartHover(evt, product) {{
  const s = chartState[product];
  const svg = document.getElementById('chart-' + product);
  if (!s || !svg) return;
  const rect = svg.getBoundingClientRect();
  const scaleX = s.w / rect.width;
  const mx = (evt.clientX - rect.left) * scaleX;
  let frac = (mx - s.marginLeft) / s.plotW;
  frac = Math.max(0, Math.min(1, frac));
  const idx = Math.round(frac * (s.values.length - 1));
  const val = s.values[idx];
  const px = s.marginLeft + s.plotW * (idx / (s.values.length - 1));
  const py = s.marginTop + s.plotH * (1 - (val - s.min) / s.range);

  const g = document.getElementById('hover-' + product);
  if (!g) return;
  g.style.display = 'block';
  const line = g.querySelector('line');
  line.setAttribute('x1', px); line.setAttribute('x2', px);
  const circle = g.querySelector('circle');
  circle.setAttribute('cx', px); circle.setAttribute('cy', py);
  const secondsAgo = s.values.length - 1 - idx;
  g.querySelector('.tt-text').textContent = '$' + val.toFixed(2) + ' (' + secondsAgo + 's ago)';
  let tx = px + 6;
  if (tx + 76 > s.w) tx = px - 82;
  const ty = Math.max(s.marginTop, py - 20);
  g.querySelector('.tt-bg').setAttribute('x', tx);
  g.querySelector('.tt-bg').setAttribute('y', ty);
  g.querySelector('.tt-text').setAttribute('x', tx + 4);
  g.querySelector('.tt-text').setAttribute('y', ty + 11);
}}
function onChartLeave(product) {{
  const g = document.getElementById('hover-' + product);
  if (g) g.style.display = 'none';
}}

// -- ladder trading: global, parameterized by product (not per-product
// closures) so multiple ladders on one page don't clobber each other's
// handlers via a shared window.trade name. --------------------------------
// refreshAllLadders forces every ladder's next-tick data fetch to happen
// right now instead of waiting out the poll interval — called after this
// user's own trade/cancel/drag actions, since that's the lag people
// actually notice (you click, and your own order doesn't show up for up
// to a full poll interval).
function refreshAllLadders() {{
  for (const k in window) {{
    if (k.startsWith('refreshLadder_') && typeof window[k] === 'function') {{
      try {{ window[k](); }} catch (e) {{}}
    }}
  }}
}}
async function trade(product, side, price, qtyOverride) {{
  const key = getKey();
  if (!key) {{ alert('log in first (top of page)'); return; }}
  const qty = qtyOverride ?? (parseInt(document.getElementById('qty-' + product).value) || 1);
  try {{
    const r = await fetch(API_BASE + '/orders', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-API-Key': key}},
      body: JSON.stringify({{product, side, type: 'limit', price, qty}}),
    }});
    if (!r.ok) {{
      let detail = r.status;
      try {{ detail = (await r.json()).detail || detail; }} catch (e2) {{}}
      alert('order rejected: ' + detail);
    }}
  }} catch (e) {{
    // A network error here previously threw all the way out of the
    // onclick handler with no feedback — the click just silently did
    // nothing and the student had no reason to believe a retry would help.
    alert('order request failed: ' + e.message);
  }}
  ownOrdersCache = null;
  refreshAllLadders();
}}
async function cancelWorking(orderIds) {{
  const key = getKey();
  if (!key || !orderIds.length) return;
  const failures = [];
  for (const id of orderIds) {{
    try {{
      const r = await fetch(API_BASE + '/orders/' + id, {{ method: 'DELETE', headers: {{'X-API-Key': key}} }});
      if (!r.ok) failures.push(id + ': ' + r.status);
    }} catch (e) {{
      failures.push(id + ': ' + e.message);
    }}
  }}
  if (failures.length) {{
    // Silently ignoring a failed cancel (e.g. a 429 from the rate limiter)
    // is what makes a click look like it "didn't work" — surface it so a
    // student knows to retry instead of clicking blindly a few more times.
    alert('cancel failed for order(s): ' + failures.join(', '));
  }}
  ownOrdersCache = null; // force a fresh fetch, don't show a stale cached copy
  refreshAllLadders();
}}

// Shared across both products' ladders (not per-product) — halves the
// request rate against the student's own rate limit (20 req/s), which
// otherwise sits close enough to the ceiling that a manual click can
// occasionally get 429'd by background polling and silently do nothing.
let ownOrdersCache = null;
let ownOrdersCacheAt = 0;
async function getOwnOrders() {{
  const key = getKey();
  if (!key) return null;
  const now = Date.now();
  if (ownOrdersCache && now - ownOrdersCacheAt < 150) return ownOrdersCache;
  try {{
    const r = await fetch(API_BASE + '/orders', {{ headers: {{'X-API-Key': key}} }});
    if (!r.ok) return null;
    ownOrdersCache = await r.json();
    ownOrdersCacheAt = now;
    return ownOrdersCache;
  }} catch (e) {{ return null; }}
}}
async function loadOwnOrders(product) {{
  const orders = await getOwnOrders();
  if (!orders) return {{}};
  const byPrice = {{}};
  for (const o of orders) {{
    if (o.product !== product || (o.status !== 'open' && o.status !== 'partially_filled')) continue;
    const p = o.price.toFixed(2);
    if (!byPrice[p]) byPrice[p] = {{ qty: 0, ids: [], side: o.side }};
    byPrice[p].qty += o.remaining_qty;
    byPrice[p].ids.push(o.id);
    byPrice[p].side = o.side;
  }}
  return byPrice;
}}

// Shared across both products' ladders (not per-product) so showing
// position/avg-entry above each ladder doesn't double the request rate —
// one /account fetch covers every product's position in one response.
let accountCache = null;
let accountCacheAt = 0;
async function getAccount() {{
  const key = getKey();
  if (!key) return null;
  const now = Date.now();
  if (accountCache && now - accountCacheAt < 150) return accountCache;
  try {{
    const r = await fetch(API_BASE + '/account', {{ headers: {{'X-API-Key': key}} }});
    if (!r.ok) return null;
    accountCache = await r.json();
    accountCacheAt = now;
    return accountCache;
  }} catch (e) {{ return null; }}
}}

// -- drag-to-reprice: drag a working cell onto another row's price to
// cancel it and re-place the same side/qty at the new level. -------------
// isDragging pauses the per-product poll's tbody rewrite (see renderRows
// below) — without this, the 1s refresh replaces the dragged <td> mid-drag
// and the browser silently cancels the drag before a drop can land.
let dragPayload = null;
let isDragging = false;
function dragWorkingStart(evt, product, ids, side, qty) {{
  dragPayload = {{ product, ids, side, qty }};
  isDragging = true;
  evt.dataTransfer.effectAllowed = 'move';
  // Required for the browser to treat this as a real drag operation that
  // fires dragover/drop on other elements — without calling setData,
  // some browsers (Firefox in particular) fall back to just dragging a
  // ghost image of the cell and never dispatch drop at all.
  evt.dataTransfer.setData('text/plain', JSON.stringify({{ product, ids, side, qty }}));
}}
function dragWorkingEnd() {{
  // Rendering resumes on the next poll tick (within ~1s) now that
  // isDragging is false — no need to force an immediate re-render here.
  isDragging = false;
  dragPayload = null;
}}
function dragOverPrice(evt) {{
  evt.preventDefault();
  evt.currentTarget.classList.add('drag-over');
}}
function dragLeavePrice(evt) {{
  evt.currentTarget.classList.remove('drag-over');
}}
async function dropReprice(evt, product, price) {{
  evt.preventDefault();
  evt.currentTarget.classList.remove('drag-over');
  isDragging = false;
  if (!dragPayload || dragPayload.product !== product) return;
  const {{ ids, side, qty }} = dragPayload;
  dragPayload = null;
  await cancelWorking(ids);
  await trade(product, side, price, qty);
}}
</script>
<div class="topbar">{nav}<div id="account-bar"></div></div>
<div id="fill-banners"></div>
<script>renderAccountBar();</script>
{body}
</body>
</html>"""


def create_website_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Mini-Exchange Website")

    nav = (
        '<nav><a href="/">Order Books</a><a href="/leaderboard">Leaderboard</a>'
        '<a href="/portfolio">Portfolio</a>'
        f'<a href="{state.config.network.admin_api_base_url}/" target="_blank">Admin</a></nav>'
    )

    def page(body: str) -> str:
        return PAGE_TEMPLATE.format(nav=nav, body=body, api_base_url=state.config.network.api_base_url)

    @app.get("/", response_class=HTMLResponse)
    def order_books():
        cols = ""
        for product, cfg in state.config.products.items():
            tick = cfg.tick_size
            cols += f"""
<div class="col">
<h2>{product}</h2>
<div class="metrics" id="metrics-{product}"></div>
<div class="meta">chart: book midpoint (moves with actual buys/sells, not the index)</div>
<svg class="chart" id="chart-{product}" width="480" height="140" viewBox="0 0 480 140"
     onmousemove="onChartHover(event, '{product}')" onmouseleave="onChartLeave('{product}')"></svg>
<div class="meta">Qty <input id="qty-{product}" value="1" size="3" style="width:50px;">
  Depth (ticks) <input id="depth-{product}" value="30" size="3" style="width:50px;">
  Rows visible <input id="rows-{product}" value="18" size="3" style="width:50px;">
  <button onclick="window['applyLadderSize_{product}']()">Apply</button>
  <button id="autocenter-btn-{product}" onclick="window['toggleAutoCenter_{product}']()">Auto-center: off</button></div>
<div class="meta" id="position-{product}">position: flat</div>
<div class="ladder-scroll" id="ladder-scroll-{product}" style="max-height:432px;">
<table class="ladder"><thead><tr><th>Working</th><th>Bid</th><th>Price</th><th>Ask</th></tr></thead>
<tbody id="ladder-{product}"></tbody></table>
</div>
</div>
<script>
(function() {{
  const product = '{product}';
  const tick = {tick};
  const scrollEl = document.getElementById('ladder-scroll-{product}');
  const rowHeightPx = 24;

  // The row range is persistent across poll ticks — re-centering on every
  // update would fight anyone trying to scroll away from the touch. It
  // only grows, when the user scrolls near an edge ("infinite scroll"),
  // never recenters or shrinks on its own.
  let range = null; // {{ minTick, maxTick }}
  let lastData = null;
  let ownByPrice = {{}};
  let initialized = false;

  function depthTicks() {{
    return parseInt(document.getElementById('depth-{product}').value) || 30;
  }}

  // Row/cell DOM nodes are kept and reused across renders, keyed by price —
  // only cells whose displayed value actually changed get touched. A full
  // innerHTML rebuild every ~200ms would, on every tick, destroy and
  // recreate the exact <td> the user might be mid-click on: per the
  // click-event spec, if the element under the pointer at mousedown is
  // gone by mouseup, no click fires at all — silently, nothing to catch.
  // That's the real mechanism behind "sometimes need to click twice."
  const rowElements = new Map(); // price key -> row entry (tr/workingTd/bidTd/priceTd/askTd/state)

  function makeRowEntry(price, key2) {{
    const tr = document.createElement('tr');
    const workingTd = document.createElement('td');
    const bidTd = document.createElement('td');
    const priceTd = document.createElement('td');
    const askTd = document.createElement('td');
    tr.appendChild(workingTd);
    tr.appendChild(bidTd);
    tr.appendChild(priceTd);
    tr.appendChild(askTd);

    bidTd.addEventListener('click', () => trade(product, 'buy', price));
    askTd.addEventListener('click', () => trade(product, 'sell', price));
    for (const td of [bidTd, priceTd, askTd]) {{
      td.addEventListener('dragover', dragOverPrice);
      td.addEventListener('dragleave', dragLeavePrice);
      td.addEventListener('drop', (evt) => dropReprice(evt, product, price));
    }}
    priceTd.textContent = key2; // static for this row's lifetime

    return {{ tr, workingTd, bidTd, priceTd, askTd, state: {{}} }};
  }}

  function renderRows() {{
    if (!range || !lastData) return;
    if (isDragging) return; // don't touch the dragged <td> mid-drag
    const d = lastData;
    const bidByPrice = {{}};
    d.book.bids.forEach(b => bidByPrice[b.price.toFixed(2)] = b.qty);
    const askByPrice = {{}};
    d.book.asks.forEach(a => askByPrice[a.price.toFixed(2)] = a.qty);

    const tbody = document.getElementById('ladder-{product}');
    const seen = new Set();

    for (let i = range.maxTick; i >= range.minTick; i--) {{
      const price = Math.round(i * tick * 100) / 100;
      const key2 = price.toFixed(2);
      seen.add(key2);
      const bidQty = bidByPrice[key2];
      const askQty = askByPrice[key2];
      const own = ownByPrice[key2];
      const isLastTrade = d.last_trade !== null && d.last_trade !== undefined && Math.abs(d.last_trade - price) < tick / 2;
      const priceClass = 'price' + (isLastTrade ? (d.last_trade_side === 'buy' ? ' last-buy' : ' last-sell') : '');

      let entry = rowElements.get(key2);
      if (!entry) {{
        entry = makeRowEntry(price, key2);
        rowElements.set(key2, entry);
      }}

      const workingKey = own ? own.qty + ':' + own.side + ':' + own.ids.join(',') : '';
      if (entry.state.working !== workingKey) {{
        entry.state.working = workingKey;
        entry.workingTd.textContent = own ? own.qty : '';
        entry.workingTd.className = 'working' + (own ? (own.side === 'buy' ? ' own-bid' : ' own-ask') : '');
        entry.workingTd.draggable = !!own;
        entry.workingTd.ondragstart = own ? (evt) => dragWorkingStart(evt, product, own.ids, own.side, own.qty) : null;
        entry.workingTd.ondragend = own ? dragWorkingEnd : null;
        entry.workingTd.onclick = own ? () => cancelWorking(own.ids) : null;
      }}

      const bidKey = bidQty ?? '';
      if (entry.state.bid !== bidKey) {{
        entry.state.bid = bidKey;
        entry.bidTd.textContent = bidQty ?? '';
        entry.bidTd.className = 'bid' + (bidQty ? ' filled' : '');
      }}

      if (entry.state.priceClass !== priceClass) {{
        entry.state.priceClass = priceClass;
        entry.priceTd.className = priceClass;
      }}

      const askKey = askQty ?? '';
      if (entry.state.ask !== askKey) {{
        entry.state.ask = askKey;
        entry.askTd.textContent = askQty ?? '';
        entry.askTd.className = 'ask' + (askQty ? ' filled' : '');
      }}

      // appendChild on an existing child just moves it — cheap, and never
      // destroys/recreates the node, so an in-progress click stays valid.
      tbody.appendChild(entry.tr);
    }}

    for (const [key2, entry] of rowElements) {{
      if (!seen.has(key2)) {{
        entry.tr.remove();
        rowElements.delete(key2);
      }}
    }}
  }}

  function buildRange(price) {{
    const centerTick = Math.round(price / tick);
    const depth = depthTicks();
    range = {{ minTick: centerTick - depth, maxTick: centerTick + depth }};
  }}
  function centerOn(price) {{
    buildRange(price);
    renderRows();
    setTimeout(() => {{ scrollEl.scrollTop = (scrollEl.scrollHeight - scrollEl.clientHeight) / 2; }}, 0);
  }}
  function currentCenterPrice() {{
    if (!lastData) return null;
    const d = lastData;
    const bestBid = d.book.bids.length ? d.book.bids[0].price : d.mid;
    const bestAsk = d.book.asks.length ? d.book.asks[0].price : d.mid;
    let center = d.mid ?? d.index_price ?? bestBid ?? bestAsk ?? 0;
    // Defense in depth: the engine now rejects non-positive prices at
    // submission, but if the book's own mid is ever nonsensical anyway
    // (non-positive, or wildly off the index), don't let the ladder center
    // itself on it — every click there would just reinforce the mess.
    if (!(center > 0) || (d.index_price && Math.abs(center - d.index_price) > d.index_price * 0.5)) {{
      center = d.index_price ?? center;
    }}
    return center;
  }}
  // Auto-center: when on, every tick snaps the view back to the touch —
  // a continuous "follow" mode, as opposed to the old one-shot Center
  // button. Off by default so a manual scroll away from the touch sticks.
  let autoCenter = false;
  window['toggleAutoCenter_{product}'] = () => {{
    autoCenter = !autoCenter;
    const btn = document.getElementById('autocenter-btn-{product}');
    if (btn) btn.textContent = 'Auto-center: ' + (autoCenter ? 'on' : 'off');
    if (autoCenter) {{
      const price = currentCenterPrice();
      if (price !== null) centerOn(price);
    }}
  }};
  window['applyLadderSize_{product}'] = () => {{
    const rows = parseInt(document.getElementById('rows-{product}').value) || 18;
    scrollEl.style.maxHeight = (rows * rowHeightPx) + 'px';
    const price = currentCenterPrice();
    if (price !== null) buildRange(price);
    renderRows();
  }};

  // "Infinite scroll" only grows the range on each extend — with nothing
  // capping it, a volatile session (price moving -> user scrolling to
  // chase it) accumulates more and more rows to fully rebuild every poll
  // tick, for the rest of the session. This caps the total span, trimming
  // the far side (off-screen, so no visible jump) instead of letting it
  // grow forever.
  const maxSpanTicks = 400;
  scrollEl.addEventListener('scroll', () => {{
    if (!range) return;
    const extend = 20;
    if (scrollEl.scrollTop < 100) {{
      range.maxTick += extend;
      if (range.maxTick - range.minTick > maxSpanTicks) {{
        range.minTick = range.maxTick - maxSpanTicks; // trim far bottom, off-screen up here
      }}
      const prevHeight = scrollEl.scrollHeight;
      renderRows();
      scrollEl.scrollTop += scrollEl.scrollHeight - prevHeight; // keep viewport steady
    }} else if (scrollEl.scrollTop + scrollEl.clientHeight > scrollEl.scrollHeight - 100) {{
      range.minTick -= extend;
      let trimmedFromTop = 0;
      if (range.maxTick - range.minTick > maxSpanTicks) {{
        const newMaxTick = range.minTick + maxSpanTicks;
        trimmedFromTop = range.maxTick - newMaxTick; // trim far top, off-screen down here
        range.maxTick = newMaxTick;
      }}
      renderRows();
      if (trimmedFromTop > 0) {{
        scrollEl.scrollTop -= trimmedFromTop * rowHeightPx; // keep viewport steady
      }}
    }}
  }});

  // A fixed function (not the shared poll() helper) so it can be called
  // both on a fast timer AND immediately after this user's own actions
  // (trade/cancel/drag) — waiting out a full poll interval after your own
  // click is the lag that actually gets noticed; refreshing on-demand
  // right after the action resolves makes your own orders feel instant
  // regardless of the ambient poll rate.
  let inFlight = false;
  async function fetchAndRender() {{
    if (inFlight) return;
    inFlight = true;
    try {{
      const d = await (await fetch('/data/book/{product}')).json();
      const metrics = document.getElementById('metrics-{product}');
      const fmt = (v) => v === null || v === undefined ? 'n/a' : v.toFixed(2);
      metrics.innerHTML =
        `<div>index <span>$${{fmt(d.index_price)}}</span></div>` +
        `<div>mid <span>$${{fmt(d.mid)}}</span></div>` +
        `<div>spread <span>${{d.spread_bps === null ? 'n/a' : d.spread_bps.toFixed(1) + ' bps'}}</span></div>` +
        `<div>last trade <span>$${{fmt(d.last_trade)}} x ${{d.last_trade_qty ?? 'n/a'}}</span></div>` +
        `<div>last trade time <span>${{timeSince(d.last_trade_ts)}}</span></div>` +
        `<div>session volume <span>${{d.session_volume_qty}} ct ($${{d.session_volume_notional.toFixed(2)}})</span></div>` +
        (d.stale ? '<div class="stale">STALE</div>' : '');

      renderChart(product, d.sparkline);

      lastData = d;
      if (autoCenter) {{
        const price = currentCenterPrice();
        if (price !== null) centerOn(price);
        initialized = true;
      }} else if (!range) {{
        const price = currentCenterPrice();
        if (!initialized) {{
          centerOn(price);
          initialized = true;
        }} else {{
          buildRange(price);
        }}
      }}

      ownByPrice = await loadOwnOrders(product);
      renderRows();

      const posDiv = document.getElementById('position-{product}');
      if (posDiv) {{
        const account = await getAccount();
        const pos = account && account.positions && account.positions['{product}'];
        posDiv.textContent = pos
          ? `position: ${{pos.qty}} @ $${{pos.avg_cost.toFixed(2)}}`
          : (account ? 'position: flat' : 'position: log in to see your position');
      }}
    }} catch (e) {{
      // ignore — next tick (or the next on-demand refresh) will retry
    }} finally {{
      inFlight = false;
    }}
  }}
  window['refreshLadder_{product}'] = fetchAndRender;

  (function loop() {{
    fetchAndRender().finally(() => setTimeout(loop, 200));
  }})();
}})();
</script>
"""
        return page(f'<div class="cols">{cols}</div>')

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard_page():
        body = """
<h2>Leaderboard</h2>
<table><thead><tr><th>#</th><th>Account</th><th>Balance</th><th>Equity</th><th>Positions</th></tr></thead>
<tbody id="lb"></tbody></table>
<script>
poll('/data/leaderboard', (rows) => {
  document.getElementById('lb').innerHTML = rows.map((r, i) =>
    `<tr><td>${i+1}</td><td>${r.account_id}</td><td>$${r.cash.toFixed(2)}</td>` +
    `<td>$${r.equity.toFixed(2)}</td><td>${JSON.stringify(r.positions)}</td></tr>`
  ).join('');
}, 2000);
</script>
"""
        return page(body)

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page():
        body = """
<h2>Portfolio</h2>
<div class="metrics" id="pf-summary"></div>

<h2>Positions</h2>
<table><thead><tr><th>Product</th><th>Qty</th><th>Avg cost</th></tr></thead>
<tbody id="pf-positions"></tbody></table>

<h2>Open orders</h2>
<table><thead><tr><th>ID</th><th>Product</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Remaining</th><th>Status</th></tr></thead>
<tbody id="pf-orders"></tbody></table>

<h2>Recent fills</h2>
<table><thead><tr><th>Product</th><th>Side</th><th>Role</th><th>Price</th><th>Qty</th><th>Fee</th><th>Counterparty</th><th>Time</th></tr></thead>
<tbody id="pf-fills"></tbody></table>

<script>
function loadPortfolio() {
  const key = getKey();
  if (!key) {
    document.getElementById('pf-summary').innerHTML = '<div>log in above to see your portfolio</div>';
    return;
  }
  poll('/data/portfolio?key=' + encodeURIComponent(key), render, 2000);
}
function render(d) {
  if (d.detail) {
    document.getElementById('pf-summary').innerHTML = `<div>${d.detail}</div>`;
    return;
  }
  const fmt = (v) => v.toFixed(2);
  document.getElementById('pf-summary').innerHTML =
    `<div>account <span>${d.account_id}</span></div>` +
    `<div>frozen <span>${d.frozen}</span></div>` +
    `<div>balance <span>$${fmt(d.balance)}</span></div>` +
    `<div>realized pnl <span>$${fmt(d.realized_pnl)}</span></div>` +
    `<div>unrealized pnl <span>$${fmt(d.unrealized_pnl)}</span></div>` +
    `<div>equity <span>$${fmt(d.equity)}</span></div>`;
  document.getElementById('pf-positions').innerHTML = Object.entries(d.positions).map(([p, pos]) =>
    `<tr><td>${p}</td><td>${pos.qty}</td><td>$${pos.avg_cost.toFixed(2)}</td></tr>`
  ).join('');
  document.getElementById('pf-orders').innerHTML = d.open_orders.map(o =>
    `<tr><td>${o.id}</td><td>${o.product}</td><td>${o.side}</td><td>${o.type}</td>` +
    `<td>${o.qty}</td><td>${o.price ?? ''}</td><td>${o.remaining_qty}</td><td>${o.status}</td></tr>`
  ).join('');
  document.getElementById('pf-fills').innerHTML = d.recent_fills.map(f =>
    `<tr><td>${f.product}</td><td>${f.side}</td><td>${f.role}</td><td>${f.price.toFixed(2)}</td>` +
    `<td>${f.qty}</td><td>${f.fee >= 0 ? '$' + f.fee.toFixed(4) : '+$' + (-f.fee).toFixed(4)}</td>` +
    `<td>${f.counterparty}</td><td>${new Date(f.timestamp * 1000).toLocaleTimeString()}</td></tr>`
  ).join('');
}
window.onLogin = loadPortfolio;
loadPortfolio();
</script>
"""
        return page(body)

    @app.get("/data/book/{product}")
    def data_book(product: str):
        if product not in state.config.products:
            raise HTTPException(status_code=404, detail="unknown product")
        return state.market_snapshot(product)

    @app.get("/data/leaderboard")
    def data_leaderboard():
        return state.leaderboard()

    @app.post("/data/register")
    def data_register(body: RegisterIn):
        try:
            key = state.register_student(body.account_id, body.password)
        except AccountExistsError:
            raise HTTPException(status_code=409, detail="account already registered, use login")
        return {"account_id": body.account_id, "api_key": key, "active": True}

    @app.get("/data/portfolio")
    def data_portfolio(key: str):
        record = state.auth.resolve(key)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown or inactive API key")
        return state.portfolio(record.account_id)

    return app
