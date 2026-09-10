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
(book, leaderboard, portfolio, chat, options) pushes to the browser over
one shared multi-channel WebSocket (/ws, see create_website_app) instead
of each page polling its own REST endpoint on a timer — same in-process
AppState reads either way, just a different transport.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
<title>{exchange_name}</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ background:#fff; color:#000; font-family: ui-monospace, monospace; margin:0; padding:24px; }}
  h1, h2 {{ color:#000; font-weight:600; margin:24px 0 8px; }}
  nav a {{ color:#000; text-decoration:none; margin-right:20px; border-bottom:1px solid #000; }}
  .chat-badge {{ display:inline-block; background:#b00020; color:#fff; font-size:10px; font-weight:700;
    border-radius:8px; min-width:15px; height:15px; line-height:15px; text-align:center; padding:0 3px;
    margin-left:4px; vertical-align:2px; }}
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
  .chain-underlying {{ font-size:20px; font-weight:700; margin:4px 0 16px; }}
  .chain-underlying span {{ color:#0a7d2c; }}
  .chain-table {{ border-collapse:collapse; width:auto; }}
  .chain-table th, .chain-table td {{ text-align:center; padding:4px 14px; border-bottom:1px solid #ddd; font-size:13px; }}
  .chain-table thead tr.group th {{ border-bottom:1px solid #000; font-size:11px; letter-spacing:0.06em; text-transform:uppercase; color:#555; }}
  .chain-table th.strike-col, .chain-table td.strike-col {{ font-weight:700; border-left:1px solid #000; border-right:1px solid #000; background:#f6f6f6; }}
  .chain-table td.call-cell, .chain-table td.put-cell {{ cursor:pointer; }}
  .chain-table td.call-cell:hover, .chain-table td.put-cell:hover {{ background:#eef3ff; }}
  .chain-positions {{ margin-top:20px; max-width:640px; }}
  .spread-matrix {{ border-collapse:collapse; margin-bottom:24px; }}
  .spread-matrix th, .spread-matrix td {{ text-align:center; padding:0; border:1px solid #ccc; font-size:12px; min-width:78px; height:40px; }}
  .spread-matrix th {{ background:#f6f6f6; font-weight:700; padding:4px; }}
  .spread-matrix th.corner {{ background:#fff; border:none; }}
  .spread-matrix td.diag {{ background:#f0f0f0; font-weight:700; }}
  .spread-matrix td.spread-cell {{ cursor:pointer; }}
  .spread-matrix td.spread-cell:hover {{ outline:2px solid #000; outline-offset:-2px; }}
  .spread-matrix td.na {{ color:#bbb; }}
  .spread-matrix .mm-bid {{ color:#0a5dd6; }}
  .spread-matrix .mm-ask {{ color:#c62828; }}
  .spread-matrix .mm-cell {{ display:flex; flex-direction:column; justify-content:center; height:100%; line-height:1.3; }}
  .spread-matrix tr:hover td:not(.na) {{ filter:brightness(0.95); }}
  .chain-sidebar {{ position:fixed; top:0; right:0; width:480px; max-width:92vw; height:100vh;
    background:#fff; border-left:1px solid #000; box-shadow:-6px 0 20px rgba(0,0,0,0.15);
    transform:translateX(100%); transition:transform 0.2s ease; z-index:2000; display:flex; flex-direction:column; }}
  .chain-sidebar.open {{ transform:translateX(0); }}
  .chain-sidebar-head {{ display:flex; justify-content:space-between; align-items:center; padding:8px 12px; border-bottom:1px solid #000; flex-shrink:0; }}
  .chain-sidebar-head button {{ background:#fff; color:#000; }}
  .chain-sidebar iframe {{ flex:1; border:none; width:100%; }}
</style>
</head>
<body>
<script>
// Defined before any page content: per-product inline <script> blocks
// below call these immediately as they're parsed, so definitions must
// come first — script tags run in document order.
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
  connectSocket(); // reconnect so the 'portfolio' channel starts flowing with this key
}}
function clearSession() {{
  localStorage.removeItem('exchange-api-key');
  localStorage.removeItem('exchange-account-id');
  lastSeenFillId = null;
  portfolioData = null; // otherwise stale data lingers after logout — nothing clears it on its own
  connectSocket(); // reconnect without a key — drops the 'portfolio' channel
}}

// -- shared WebSocket feed: one connection per page, multiplexing every
// channel that page needs (book/portfolio/leaderboard/chat/options)
// instead of a separate polling timer per data type. Each page sets
// window.__wsChannels (an array, e.g. ['book:BTC-MINI']) before this
// connects — see the trailing connectSocket() call placed in its own
// script tag after the page content below, which runs only after every
// page-content script has already run and had a chance to set that
// array. NOTE: never write the literal characters '</script' inside any
// comment or string in this file's inline JS — the HTML parser closes a
// <script> element on that sequence regardless of JS-level context, which
// silently truncates everything after it into plain visible page text
// (exactly what happened here the first time). ------------------------
let socket = null;
let socketReconnectTimer = null;
const channelCallbacks = {{}};
function onChannel(name, cb) {{
  (channelCallbacks[name] = channelCallbacks[name] || []).push(cb);
}}
function wantedChannels() {{
  const channels = new Set(window.__wsChannels || []);
  if (getKey()) channels.add('portfolio');
  channels.add('chat'); // always on, everywhere — see updateChatBadge below
  return Array.from(channels);
}}

// -- unread chat badge on the nav link, on every page -----------------------
let lastSeenChatId = parseInt(localStorage.getItem('exchange-last-seen-chat-id') || '0', 10);
function updateChatBadge(messages) {{
  const badge = document.getElementById('chat-badge');
  if (!badge) return;
  const unread = messages.filter((m) => m.id > lastSeenChatId).length;
  badge.textContent = unread > 0 ? String(unread) : '';
  badge.hidden = unread === 0;
}}
function markChatRead(messages) {{
  if (!messages.length) return;
  lastSeenChatId = messages[messages.length - 1].id;
  localStorage.setItem('exchange-last-seen-chat-id', String(lastSeenChatId));
  updateChatBadge(messages);
}}
onChannel('chat', updateChatBadge);
function connectSocket() {{
  if (socketReconnectTimer) {{ clearTimeout(socketReconnectTimer); socketReconnectTimer = null; }}
  if (socket) {{ try {{ socket.onclose = null; socket.close(); }} catch (e) {{}} }}
  const channels = wantedChannels();
  if (!channels.length) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let url = `${{proto}}://${{location.host}}/ws?channels=${{encodeURIComponent(channels.join(','))}}`;
  const key = getKey();
  if (key) url += `&key=${{encodeURIComponent(key)}}`;
  socket = new WebSocket(url);
  socket.onmessage = (evt) => {{
    let msg;
    try {{ msg = JSON.parse(evt.data); }} catch (e) {{ return; }}
    (channelCallbacks[msg.channel] || []).forEach(cb => {{ try {{ cb(msg.data); }} catch (e) {{}} }});
  }};
  // Fixed short backoff, not exponential — a dropped connection (server
  // restart, brief network blip) should recover fast and quietly; this
  // isn't a distributed system with a thundering-herd risk at this scale.
  socket.onclose = () => {{ socketReconnectTimer = setTimeout(connectSocket, 1000); }};
}}

// -- mute: suppresses both fill banners and the fill sound, persisted per
// browser so it survives a page reload / navigating between pages ---------
function isMuted() {{
  try {{ return localStorage.getItem('exchange-muted') === '1'; }} catch (e) {{ return false; }}
}}
function renderMuteButton() {{
  const btn = document.getElementById('mute-btn');
  if (!btn) return;
  btn.textContent = isMuted() ? 'Unmute' : 'Mute notifications';
}}
function toggleMute() {{
  try {{ localStorage.setItem('exchange-muted', isMuted() ? '0' : '1'); }} catch (e) {{}}
  renderMuteButton();
}}

// -- fill notifications: banner + sound, on every page ----------------------
// Seeded to the current max fill id on login/page-load so a student isn't
// flooded with notifications for fills that happened before this session.
let lastSeenFillId = null;
function playFillSound() {{
  if (isMuted()) return;
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
  if (isMuted()) return;
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
// Latest 'portfolio' channel payload — state.portfolio()'s shape
// (cash/balance/positions/open_orders/recent_fills/...), refreshed
// automatically whenever the server pushes a change. getAccount()/
// getOwnOrders() below read this synchronously instead of each doing
// their own cross-origin fetch — one shared feed for the whole page.
let portfolioData = null;
function handlePortfolioUpdate(d) {{
  if (d && d.error === 'invalid_key') {{
    // The key no longer resolves server-side (e.g. the server restarted
    // with a fresh in-memory session store) — drop back to the login form
    // instead of silently showing stale data forever.
    clearSession();
    renderAccountBar();
    if (window.onLogin) window.onLogin();
    const msg = document.getElementById('ab-msg');
    if (msg) msg.innerHTML = '<span class="err">session expired, please log in again</span>';
    portfolioData = null;
    return;
  }}
  portfolioData = d;
  const el = document.getElementById('ab-balance');
  if (el && d) el.textContent = '$' + d.balance.toFixed(2);
  if (d && d.recent_fills) {{
    if (lastSeenFillId === null) {{
      // first look at this account this session: seed silently, don't
      // notify for fills that already happened before now.
      lastSeenFillId = d.recent_fills.length ? Math.max(...d.recent_fills.map(f => f.id)) : 0;
    }} else {{
      const newOnes = d.recent_fills.filter(f => f.id > lastSeenFillId).sort((a, b) => a.id - b.id);
      for (const f of newOnes) {{
        showFillBanner(f);
        playFillSound();
        lastSeenFillId = Math.max(lastSeenFillId, f.id);
      }}
    }}
  }}
  if (window.onPortfolio) window.onPortfolio(d);
}}
onChannel('portfolio', handlePortfolioUpdate);

function renderAccountBar() {{
  const bar = document.getElementById('account-bar');
  if (!bar) return;
  const key = getKey(), accountId = getAccountId();
  if (key) {{
    bar.innerHTML = `Logged in as <b>${{accountId}}</b> &nbsp; balance: <span id="ab-balance">...</span> &nbsp; ` +
      `<button onclick="logout()">Log out</button>`;
  }} else {{
    bar.innerHTML =
      `Username <input id="ab-user" size="12"> Password <input id="ab-pass" type="password" size="12"> ` +
      `<button onclick="doRegister()">Register</button> <button onclick="doLogin()">Log in</button> ` +
      `<span id="ab-msg"></span>`;
  }}
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
// No explicit "refresh now" after an action (there used to be one, forcing
// every ladder to re-poll immediately) — the WS feed already pushes the
// resulting book/portfolio change within one server tick (~200ms) on its
// own, same latency the forced refresh used to achieve, automatically.
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
}}
// Tracks order ids with a cancel already in flight — without this, a
// double-click (or a stale re-render landing a second click before the
// first DELETE resolves) fires a second DELETE for the same id, which
// comes back "order is not open" (400) and surfaces as a confusing
// "cancel failed" alert for an action that actually already succeeded.
const pendingCancels = new Set();
async function cancelWorking(orderIds) {{
  const key = getKey();
  if (!key || !orderIds.length) return;
  const idsToCancel = orderIds.filter(id => !pendingCancels.has(id));
  if (!idsToCancel.length) return; // all already being cancelled — no-op
  idsToCancel.forEach(id => pendingCancels.add(id));
  const failures = [];
  try {{
    for (const id of idsToCancel) {{
      try {{
        const r = await fetch(API_BASE + '/orders/' + id, {{ method: 'DELETE', headers: {{'X-API-Key': key}} }});
        if (!r.ok) failures.push(id + ': ' + r.status);
      }} catch (e) {{
        failures.push(id + ': ' + e.message);
      }}
    }}
  }} finally {{
    idsToCancel.forEach(id => pendingCancels.delete(id));
  }}
  if (failures.length) {{
    // Silently ignoring a failed cancel (e.g. a 429 from the rate limiter)
    // is what makes a click look like it "didn't work" — surface it so a
    // student knows to retry instead of clicking blindly a few more times.
    alert('cancel failed for order(s): ' + failures.join(', '));
  }}
}}

// Shared across both products' ladders (not per-product) — halves the
// request rate against the student's own rate limit (20 req/s), which
// otherwise sits close enough to the ceiling that a manual click can
// occasionally get 429'd by background polling and silently do nothing.
// Both now just read the shared 'portfolio' channel payload (see
// handlePortfolioUpdate above) instead of each fetching their own copy —
// no caching/in-flight-dedup needed any more, it's a synchronous read of
// whatever the WS feed most recently pushed. Kept as async functions
// (and the exact same names) so every existing call site — ladder
// rendering, the options chain, flatten/cancel-all — needs no changes.
async function getOwnOrders() {{
  return portfolioData ? portfolioData.open_orders : null;
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

// Same reasoning as getOwnOrders above — one shared 'portfolio' channel
// payload covers every product's position, read synchronously.
async function getAccount() {{
  return portfolioData;
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
<div class="topbar">{nav}<div style="display:flex; align-items:center; gap:14px;"><button id="mute-btn" onclick="toggleMute()"></button><div id="account-bar"></div></div></div>
<div id="fill-banners"></div>
<script>renderAccountBar(); renderMuteButton();</script>
{body}
<script>connectSocket();</script>
</body>
</html>"""


def _ladder_block(product: str, tick: float) -> str:
    """One product's order-book column: metrics, chart, and click/drag-to-
    trade ladder. Factored out of order_books() so the Spread Matrix page
    can render the exact same widget for a single instrument instead of
    duplicating ~250 lines of ladder JS."""
    return f"""
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
  (window.__wsChannels = window.__wsChannels || []).push('book:' + product);
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

  // Pushed by the server on the 'book:{product}' channel whenever this
  // product's book/index/last-trade actually changes — no polling, no
  // "refresh now after my own action" call needed, the next server tick
  // (~200ms) already reflects any fill from this account's own click.
  async function render(d) {{
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

    renderRows();
    await renderOwnState();
  }}
  async function renderOwnState() {{
    const [own, account] = await Promise.all([loadOwnOrders(product), getAccount()]);
    ownByPrice = own;
    renderRows(); // cheap — diffed, only the working column actually changes

    const posDiv = document.getElementById('position-{product}');
    if (posDiv) {{
      const pos = account && account.positions && account.positions['{product}'];
      posDiv.textContent = pos
        ? `position: ${{pos.qty}} @ $${{pos.avg_cost.toFixed(2)}}`
        : (account ? 'position: flat' : 'position: log in to see your position');
    }}
  }}
  onChannel('book:' + product, render);
  // The book itself might not change on a fill against a resting order at
  // the same price level it was already at — but the working-order/
  // position display still needs to catch up, so it also refreshes
  // whenever the portfolio channel pushes independently of the book.
  onChannel('portfolio', renderOwnState);
}})();
</script>
"""


def _options_payload(state: AppState, chain_id: str) -> dict:
    """Current option chain summary — shared by the 'options:<chain_id>' WS
    channel (see create_website_app's ws_feed) so there's one
    implementation, not a REST copy and a WS copy that could drift apart."""
    manager = state.options_managers[chain_id]
    now = time.time()
    contracts = []
    expiry_ts = None
    for opt in manager.chain.values():
        expiry_ts = opt.expiry_ts
        book = state.engine.book_snapshot(opt.symbol, depth=1)
        contracts.append({
            "symbol": opt.symbol,
            "strike": opt.strike,
            "option_type": opt.option_type,
            "theo": state.index_service.get_index_price(opt.symbol, now),
            "bid": book["bids"][0]["price"] if book["bids"] else None,
            "ask": book["asks"][0]["price"] if book["asks"] else None,
        })
    underlying_price = state.index_service.get_index_price(manager.cfg.underlying, now)
    return {"expiry_ts": expiry_ts, "contracts": contracts, "underlying_price": underlying_price}


def _futures_payload(state: AppState) -> dict:
    """Live futures contracts + calendar spreads per underlying — shared by
    the 'futures_matrix' WS channel and the Inter-Spread page's TT-style
    spread matrix (one row/column per live contract month, off-diagonal
    cells are the calendar spread between that pair). Includes top-of-book
    bid/ask (not just the index/theo 'last') so the matrix reads like an
    actual quote grid, same as the options chain."""
    now = time.time()
    out: dict[str, dict] = {}
    for underlying in state.config.futures.underlyings:
        contracts = sorted(state.futures_manager.contracts[underlying].values(), key=lambda f: f.expiry_ts)
        spreads = sorted(state.futures_manager.calendar_spreads[underlying].values(), key=lambda c: c.symbol)
        futs_out = []
        for f in contracts:
            book = state.engine.book_snapshot(f.symbol, depth=1)
            futs_out.append({
                "symbol": f.symbol,
                "expiry_ts": f.expiry_ts,
                "last": state.index_service.get_index_price(f.symbol, now),
                "bid": book["bids"][0]["price"] if book["bids"] else None,
                "ask": book["asks"][0]["price"] if book["asks"] else None,
            })
        spreads_out = []
        for c in spreads:
            book = state.engine.book_snapshot(c.symbol, depth=1)
            spreads_out.append({
                "symbol": c.symbol,
                "near": c.near_symbol,
                "far": c.far_symbol,
                "last": state.index_service.get_index_price(c.symbol, now),
                "bid": book["bids"][0]["price"] if book["bids"] else None,
                "ask": book["asks"][0]["price"] if book["asks"] else None,
            })
        out[underlying] = {"futures": futs_out, "calendar_spreads": spreads_out}
    return out


def _find_option(state: AppState, symbol: str):
    for manager in state.options_managers.values():
        opt = manager.chain.get(symbol)
        if opt is not None:
            return opt
    return None


def create_website_app(state: AppState) -> FastAPI:
    app = FastAPI(title=f"{state.config.exchange_name} Website")

    nav = (
        '<nav><a href="/">Spot</a><a href="/options">Options Chain</a>'
        '<a href="/inter-spread">Inter-Spread</a><a href="/rfq">RFQs</a><a href="/leaderboard">Leaderboard</a>'
        '<a href="/portfolio">Portfolio</a>'
        '<a href="/chat">Chat<span id="chat-badge" class="chat-badge" hidden></span></a>'
        f'<a href="{state.config.network.admin_api_base_url}/" target="_blank">Admin</a></nav>'
    )

    def page(body: str) -> str:
        return PAGE_TEMPLATE.format(
            nav=nav, body=body, api_base_url=state.config.network.api_base_url,
            exchange_name=state.config.exchange_name,
        )

    @app.get("/", response_class=HTMLResponse)
    def order_books():
        cols = ""
        for product, cfg in state.config.products.items():
            cols += _ladder_block(product, cfg.tick_size)
        spread_symbol = state.config.spread.symbol
        cols += _ladder_block(spread_symbol, state.config.spread.tick_size)
        banner = (
            "" if state.spread_enabled else
            f'<p class="meta">The {spread_symbol} spread instrument is currently disabled by the admin. '
            "The book below will populate once it's re-enabled.</p>"
        )
        return page(f"<h2>Spot</h2>{banner}" + f'<div class="cols">{cols}</div>')

    @app.get("/inter-spread", response_class=HTMLResponse)
    def inter_spread_matrix():
        symbol = state.config.spread.symbol
        if not state.spread_enabled:
            banner = (
                f'<p class="meta">The {symbol} spread instrument is currently disabled by the admin. '
                "The book below will populate once it's re-enabled.</p>"
            )
        else:
            banner = ""
        tick = state.config.spread.tick_size
        underlyings_json = json.dumps(list(state.config.futures.underlyings))
        body = f"""
<h2>Inter-Spread</h2>
<h3>BTC / ETH cross-product spread</h3>
{banner}
<div class="cols">{_ladder_block(symbol, tick)}</div>

<h3>Futures &amp; calendar spreads</h3>
<p class="meta">TT-style spread matrix: diagonal is each contract month's own outright market;
off-diagonal is the calendar spread between that row and column (only adjacent months actually
trade — a cell with no live market shows &mdash;). Click any cell to open its ladder.
{"" if state.futures_enabled else "Currently disabled by the admin."}</p>
<div id="futures-matrix"></div>

<div id="im-sidebar" class="chain-sidebar">
  <div class="chain-sidebar-head">
    <strong id="im-sidebar-title"></strong>
    <button onclick="closeImLadder()">&times; Close</button>
  </div>
  <iframe id="im-sidebar-frame" src="about:blank"></iframe>
</div>

<script>
function openImLadder(symbol) {{
  document.getElementById('im-sidebar-title').textContent = symbol;
  document.getElementById('im-sidebar-frame').src = '/ladder/' + encodeURIComponent(symbol) + '?embed=1';
  document.getElementById('im-sidebar').classList.add('open');
}}
function closeImLadder() {{
  document.getElementById('im-sidebar').classList.remove('open');
  document.getElementById('im-sidebar-frame').src = 'about:blank';
}}
function monthLabel(f, i) {{
  // Front month / back months, TT-matrix style, with the actual expiry
  // clock time as a tooltip since these are 1-hour (not calendar-month)
  // contracts.
  return i === 0 ? 'Front' : `+${{i}}`;
}}
function mmCell(bid, ask, symbol, extraClass) {{
  const fmt = (v) => v === null || v === undefined ? null : v.toFixed(2);
  const b = fmt(bid), a = fmt(ask);
  if (b === null && a === null) {{
    return `<td class="na">&mdash;</td>`;
  }}
  return `<td class="spread-cell ${{extraClass || ''}}" onclick="openImLadder('${{symbol}}')" title="${{symbol}}">` +
    `<div class="mm-cell"><span class="mm-bid">${{b ?? '—'}}</span><span class="mm-ask">${{a ?? '—'}}</span></div></td>`;
}}
function renderFuturesMatrix(data) {{
  const underlyings = {underlyings_json};
  document.getElementById('futures-matrix').innerHTML = underlyings.map(u => {{
    const info = data[u] || {{futures: [], calendar_spreads: []}};
    const contracts = info.futures;
    if (!contracts.length) {{
      return `<div class="chain-underlying" style="font-size:15px;">${{u}}</div><p class="meta">none live</p>`;
    }}
    // near_symbol -> far_symbol -> spread info, for O(1) lookup per cell.
    const bySymbolPair = {{}};
    for (const c of info.calendar_spreads) {{
      (bySymbolPair[c.near] = bySymbolPair[c.near] || {{}})[c.far] = c;
    }}
    const headerCells = contracts.map((f, i) => `<th title="${{f.symbol}}">${{monthLabel(f, i)}}</th>`).join('');
    const rows = contracts.map((rowFut, i) => {{
      const cells = contracts.map((colFut, j) => {{
        if (i === j) {{
          return mmCell(rowFut.bid, rowFut.ask, rowFut.symbol, 'diag');
        }}
        const near = i < j ? rowFut : colFut;
        const far = i < j ? colFut : rowFut;
        const cs = (bySymbolPair[near.symbol] || {{}})[far.symbol];
        if (!cs) return `<td class="na">&mdash;</td>`;
        if (i < j) return mmCell(cs.bid, cs.ask, cs.symbol);
        // Lower triangle mirrors the same near/far spread, sign-flipped
        // (far - near instead of near - far) — same instrument, just the
        // other side of the same trade, same as a TT matrix's symmetric layout.
        const flip = (v) => v === null || v === undefined ? null : -v;
        return mmCell(flip(cs.ask), flip(cs.bid), cs.symbol);
      }}).join('');
      return `<tr><th title="${{rowFut.symbol}}">${{monthLabel(rowFut, i)}}</th>${{cells}}</tr>`;
    }}).join('');
    return `<div class="chain-underlying" style="font-size:15px;">${{u}}</div>` +
      `<div style="overflow-x:auto;"><table class="spread-matrix"><thead>` +
      `<tr><th class="corner"></th>${{headerCells}}</tr></thead><tbody>${{rows}}</tbody></table></div>`;
  }}).join('');
}}
window.__wsChannels = ['futures_matrix'];
onChannel('futures_matrix', renderFuturesMatrix);
</script>
"""
        return page(body)

    @app.get("/ladder/{symbol}", response_class=HTMLResponse)
    def generic_ladder(symbol: str, embed: bool = False):
        product_cfg = state.engine.products.get(symbol)
        if product_cfg is None:
            body = (
                f'<h2>{symbol}</h2>'
                '<p class="meta">This contract is no longer active (expired/settled, or the chain has rolled). '
                '<a href="/inter-spread">Back to Inter-Spread</a></p>'
            )
            return page(body)
        header = f'<h2>{symbol}</h2>' + ("" if embed else ' <a href="/inter-spread">Back to Inter-Spread</a>')
        content = header + f'<div class="cols">{_ladder_block(symbol, product_cfg.tick_size)}</div>'
        if embed:
            content = "<style>.topbar nav{display:none} body{padding:10px}</style>" + content
        return page(content)

    @app.get("/options", response_class=HTMLResponse)
    def options_chain(chain: str = "btc"):
        if chain not in state.options_managers:
            chain = next(iter(state.options_managers), None)
        tabs = "".join(
            f'<a href="/options?chain={cid}" style="margin-right:16px;{"font-weight:700;text-decoration:underline;" if cid == chain else ""}">{cid}</a>'
            for cid in state.options_managers
        )
        tabs_html = f'<div class="meta">{tabs}</div>' if len(state.options_managers) > 1 else ""
        if chain is None:
            return page("<h2>Options Chain</h2>" + tabs_html + '<p class="meta">No option chains configured.</p>')
        manager = state.options_managers[chain]
        contracts = sorted(manager.chain.values(), key=lambda o: (o.strike, o.option_type))
        if not contracts:
            body = (
                "<h2>Options Chain</h2>" + tabs_html +
                '<p class="meta">No active chain right now'
                + ("." if state.options_enabled[chain] else " — this chain is currently disabled by the admin.")
                + "</p>"
            )
            return page(body)
        strikes = sorted({o.strike for o in contracts})
        rows = "".join(
            f'<tr>'
            f'<td class="call-cell" id="opt-call-bid-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'<td class="call-cell" id="opt-call-theo-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'<td class="call-cell" id="opt-call-ask-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'<td class="strike-col">{strike:.2f}</td>'
            f'<td class="put-cell" id="opt-put-bid-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'<td class="put-cell" id="opt-put-theo-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'<td class="put-cell" id="opt-put-ask-{strike:.2f}" onclick="cellClick(this)"></td>'
            f'</tr>'
            for strike in strikes
        )
        body = f"""
<h2>Options Chain</h2>
{tabs_html}
<div class="chain-underlying">{manager.cfg.underlying} <span id="chain-underlying-price">...</span></div>
<p class="meta">expiry <span id="opt-expiry"></span> &middot; click any Bid/Theo/Ask cell to open that contract's ladder</p>
<div style="overflow-x:auto;">
<table class="chain-table">
<thead>
<tr class="group"><th colspan="3">Calls</th><th></th><th colspan="3">Puts</th></tr>
<tr><th>Bid</th><th>Theo</th><th>Ask</th><th class="strike-col">Strike</th><th>Bid</th><th>Theo</th><th>Ask</th></tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>

<div class="chain-positions">
<h2>Your option positions</h2>
<table><thead><tr><th>Contract</th><th>Qty</th><th>Avg cost</th></tr></thead>
<tbody id="chain-positions-body"><tr><td colspan="3">log in above to see your positions</td></tr></tbody></table>
</div>

<div id="chain-sidebar" class="chain-sidebar">
  <div class="chain-sidebar-head">
    <strong id="chain-sidebar-title"></strong>
    <button onclick="closeLadder()">&times; Close</button>
  </div>
  <iframe id="chain-sidebar-frame" src="about:blank"></iframe>
</div>

<script>
let chainContractSymbols = new Set();

function openLadder(symbol) {{
  document.getElementById('chain-sidebar-title').textContent = symbol;
  document.getElementById('chain-sidebar-frame').src = '/options/' + encodeURIComponent(symbol) + '?embed=1';
  document.getElementById('chain-sidebar').classList.add('open');
}}
function closeLadder() {{
  document.getElementById('chain-sidebar').classList.remove('open');
  document.getElementById('chain-sidebar-frame').src = 'about:blank';
}}
function cellClick(el) {{
  if (el.dataset.symbol) openLadder(el.dataset.symbol);
}}
function renderOptionCell(strike, side, c) {{
  const fmt = (v) => v === null || v === undefined ? '—' : v.toFixed(2);
  for (const field of ['bid', 'theo', 'ask']) {{
    const el = document.getElementById('opt-' + side + '-' + field + '-' + strike.toFixed(2));
    if (!el) continue;
    el.textContent = fmt(c[field]);
    el.dataset.symbol = c.symbol;
  }}
}}
async function renderChainPositions() {{
  const account = await getAccount();
  const tbody = document.getElementById('chain-positions-body');
  if (!tbody) return;
  if (!account) {{
    tbody.innerHTML = '<tr><td colspan="3">log in above to see your positions</td></tr>';
    return;
  }}
  const rows = Object.entries(account.positions).filter(([sym]) => chainContractSymbols.has(sym));
  tbody.innerHTML = rows.length
    ? rows.map(([sym, pos]) => `<tr><td>${{sym}}</td><td>${{pos.qty}}</td><td>$${{pos.avg_cost.toFixed(2)}}</td></tr>`).join('')
    : '<tr><td colspan="3">no open option positions</td></tr>';
}}
window.__wsChannels = ['options:{chain}'];
onChannel('options:{chain}', (d) => {{
  document.getElementById('opt-expiry').textContent = d.expiry_ts
    ? new Date(d.expiry_ts * 1000).toLocaleTimeString() : 'n/a';
  const priceEl = document.getElementById('chain-underlying-price');
  if (priceEl) priceEl.textContent = (d.underlying_price === null || d.underlying_price === undefined)
    ? 'n/a' : '$' + d.underlying_price.toFixed(2);
  chainContractSymbols = new Set(d.contracts.map(c => c.symbol));
  for (const c of d.contracts) {{
    renderOptionCell(c.strike, c.option_type, c);
  }}
  renderChainPositions();
}});
onChannel('portfolio', renderChainPositions);
window.onLogin = renderChainPositions;
</script>
"""
        return page(body)

    @app.get("/options/{symbol}", response_class=HTMLResponse)
    def option_ladder(symbol: str, embed: bool = False):
        opt = _find_option(state, symbol)
        if opt is None:
            body = (
                f'<h2>{symbol}</h2>'
                '<p class="meta">This contract is no longer active (expired/settled, or the chain has rolled). '
                '<a href="/options">Back to Options Chain</a></p>'
            )
            return page(body)
        header = (
            f'<h2>{symbol}</h2>'
            f'<p class="meta">{opt.option_type.upper()} &middot; strike {opt.strike:.2f} &middot; '
            f'expiry {time.strftime("%H:%M:%S UTC", time.gmtime(opt.expiry_ts))}'
            + ("" if embed else ' &middot; <a href="/options">Back to Options Chain</a>')
            + "</p>"
        )
        tick = state.engine.products[symbol].tick_size
        content = header + f'<div class="cols">{_ladder_block(symbol, tick)}</div>'
        if embed:
            # Opened inside the chain page's slide-in sidebar — the full nav
            # bar just wastes width in a 480px panel; the account bar stays
            # (same localStorage session, same origin) since trading needs it.
            content = "<style>.topbar nav{display:none} body{padding:10px}</style>" + content
        return page(content)

    @app.get("/rfq", response_class=HTMLResponse)
    def rfq_page():
        body = """
<h2>RFQs</h2>
<p class="meta">Ask for a firm two-way price on any size instead of working the book yourself —
any other account can quote you back, you pick the one you like. Quotes are private: you only see
your own quote on someone else's RFQ, and only the requester sees every quote on their own.</p>

<h3>Request a quote</h3>
<div style="display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap;">
  <input id="rfq-product" placeholder="product (e.g. BTC-MINI)" style="width:170px;">
  <select id="rfq-side"><option value="buy">buy</option><option value="sell">sell</option></select>
  <input id="rfq-qty" type="number" min="1" placeholder="qty" style="width:80px;">
  <input id="rfq-ttl" type="number" min="5" max="300" value="30" placeholder="ttl (s)" style="width:90px;">
  <button onclick="createRfq()">Request quote</button>
</div>
<div id="rfq-msg" class="meta"></div>

<h3>Open RFQs</h3>
<table>
<thead><tr><th>ID</th><th>Requester</th><th>Product</th><th>Side</th><th>Qty</th><th>Left</th><th>Expires</th><th>Quotes / action</th></tr></thead>
<tbody id="rfq-list"><tr><td colspan="8">none open right now</td></tr></tbody>
</table>

<script>
function fmtCountdown(expiresAt) {
  const s = Math.round(expiresAt - Date.now() / 1000);
  return s <= 0 ? 'expired' : s + 's';
}
async function createRfq() {
  const key = getKey();
  const msgEl = document.getElementById('rfq-msg');
  if (!key) { msgEl.innerHTML = '<span class="err">log in above first</span>'; return; }
  const product = document.getElementById('rfq-product').value.trim();
  const side = document.getElementById('rfq-side').value;
  const qty = parseInt(document.getElementById('rfq-qty').value, 10);
  const ttl_seconds = parseFloat(document.getElementById('rfq-ttl').value) || 30;
  if (!product || !qty || qty <= 0) { msgEl.innerHTML = '<span class="err">product and a positive qty are required</span>'; return; }
  try {
    const r = await fetch(API_BASE + '/rfqs', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-API-Key': key},
      body: JSON.stringify({product, side, qty, ttl_seconds}),
    });
    const d = await r.json();
    msgEl.innerHTML = r.ok ? '' : `<span class="err">${d.detail || r.status}</span>`;
  } catch (e) {
    msgEl.innerHTML = `<span class="err">${e.message}</span>`;
  }
}
async function submitQuote(rfqId, inputId) {
  const key = getKey();
  if (!key) { alert('log in above first'); return; }
  const price = parseFloat(document.getElementById(inputId).value);
  if (!isFinite(price)) { alert('enter a price'); return; }
  const r = await fetch(`${API_BASE}/rfqs/${rfqId}/quotes`, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-API-Key': key},
    body: JSON.stringify({price}),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(`quote rejected: ${d.detail || r.status}`); }
}
async function acceptQuote(rfqId, quoteId) {
  const key = getKey();
  if (!key) { alert('log in above first'); return; }
  const r = await fetch(`${API_BASE}/rfqs/${rfqId}/quotes/${quoteId}/accept`, {
    method: 'POST', headers: {'X-API-Key': key},
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(`accept failed: ${d.detail || r.status}`); }
}
async function cancelRfq(rfqId) {
  const key = getKey();
  if (!key) { alert('log in above first'); return; }
  const r = await fetch(`${API_BASE}/rfqs/${rfqId}`, {method: 'DELETE', headers: {'X-API-Key': key}});
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(`cancel failed: ${d.detail || r.status}`); }
}
function renderRfqQuotesCell(rfq) {
  if (rfq.own) {
    if (!rfq.quotes.length) return '<span class="meta">waiting for quotes…</span> ' +
      `<button onclick="cancelRfq(${rfq.id})" style="font-size:11px; padding:1px 6px;">Cancel</button>`;
    return rfq.quotes.map(q =>
      `<div>${q.account_id} @ ${q.price.toFixed(2)} x${q.qty} [${q.status}]` +
      (q.status === 'open' ? ` <button onclick="acceptQuote(${rfq.id},${q.id})" style="font-size:11px; padding:1px 6px;">Accept</button>` : '') +
      `</div>`
    ).join('') + `<button onclick="cancelRfq(${rfq.id})" style="font-size:11px; padding:1px 6px; margin-top:4px;">Cancel RFQ</button>`;
  }
  const mine = rfq.quotes[0]; // only ever contains our own quote(s), per the API's visibility rule
  if (mine) return `<span class="meta">your quote: ${mine.price.toFixed(2)} x${mine.qty} [${mine.status}]</span>`;
  const inputId = `rfq-quote-price-${rfq.id}`;
  return `<input id="${inputId}" type="number" step="any" placeholder="your price" style="width:90px;"> ` +
    `<button onclick="submitQuote(${rfq.id}, '${inputId}')" style="font-size:11px; padding:1px 6px;">Quote</button>`;
}
function renderRfqs(rows) {
  const tbody = document.getElementById('rfq-list');
  tbody.innerHTML = rows.length ? rows.map(rfq => `<tr>` +
    `<td>${rfq.id}</td><td>${rfq.account_id}${rfq.own ? ' (you)' : ''}</td><td>${rfq.product}</td>` +
    `<td>${rfq.side}</td><td>${rfq.qty}</td><td>${rfq.remaining_qty}</td>` +
    `<td>${fmtCountdown(rfq.expires_at)}</td><td>${renderRfqQuotesCell(rfq)}</td></tr>`
  ).join('') : '<tr><td colspan="8">none open right now</td></tr>';
}
window.__wsChannels = ['rfqs'];
onChannel('rfqs', renderRfqs);
</script>
"""
        return page(body)

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard_page():
        body = """
<h2>Leaderboard</h2>
<table><thead><tr><th>#</th><th>Account</th><th>Balance</th><th>Equity</th><th>Positions</th></tr></thead>
<tbody id="lb"></tbody></table>
<script>
window.__wsChannels = ['leaderboard'];
onChannel('leaderboard', (rows) => {
  document.getElementById('lb').innerHTML = rows.map((r, i) =>
    `<tr><td>${i+1}</td>` +
    `<td><a href="/portfolio/${encodeURIComponent(r.account_id)}">${r.account_id}</a></td>` +
    `<td>$${r.cash.toFixed(2)}</td>` +
    `<td>$${r.equity.toFixed(2)}</td><td>${JSON.stringify(r.positions)}</td></tr>`
  ).join('');
});
</script>
"""
        return page(body)

    @app.get("/portfolio/{account_id}", response_class=HTMLResponse)
    def view_portfolio(account_id: str):
        """Read-only view of *someone else's* portfolio — same data the
        leaderboard already exposes with no auth (account_id/cash/equity/
        positions), extended to the same fill/order detail your own
        `/portfolio` page shows, since none of that is any more sensitive
        than what the leaderboard already publishes. No trading controls
        (no flatten/cancel — this isn't your account)."""
        if account_id not in state.engine.accounts:
            return page(f'<h2>Portfolio: {account_id}</h2><p class="meta">no such account.</p>')
        channel = f"portfolio_of:{account_id}"
        body = f"""
<h2>Portfolio: {account_id}</h2>
<div class="metrics" id="pf-summary"></div>

<h2>Positions</h2>
<table><thead><tr><th>Product</th><th>Qty</th><th>Avg cost</th></tr></thead>
<tbody id="pf-positions"></tbody></table>

<h2>Open orders</h2>
<table><thead><tr><th>ID</th><th>Product</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Remaining</th><th>Status</th></tr></thead>
<tbody id="pf-orders"></tbody></table>

<h2>Recent fills</h2>
<table><thead><tr><th>Product</th><th>Side</th><th>Role</th><th>Price</th><th>Qty</th><th>Counterparty</th><th>Time</th></tr></thead>
<tbody id="pf-fills"></tbody></table>

<script>
window.__wsChannels = ['{channel}'];
onChannel('{channel}', (d) => {{
  if (!d) return;
  const fmt = (v) => v.toFixed(2);
  document.getElementById('pf-summary').innerHTML =
    `<div>account <span>${{d.account_id}}</span></div>` +
    `<div>frozen <span>${{d.frozen}}</span></div>` +
    `<div>balance <span>$${{fmt(d.balance)}}</span></div>` +
    `<div>equity <span>$${{fmt(d.equity)}}</span></div>`;
  document.getElementById('pf-positions').innerHTML = Object.entries(d.positions).map(([p, pos]) =>
    `<tr><td>${{p}}</td><td>${{pos.qty}}</td><td>$${{pos.avg_cost.toFixed(2)}}</td></tr>`
  ).join('') || '<tr><td colspan="3">flat</td></tr>';
  document.getElementById('pf-orders').innerHTML = d.open_orders.map(o =>
    `<tr><td>${{o.id}}</td><td>${{o.product}}</td><td>${{o.side}}</td><td>${{o.type}}</td>` +
    `<td>${{o.qty}}</td><td>${{o.price ?? ''}}</td><td>${{o.remaining_qty}}</td><td>${{o.status}}</td></tr>`
  ).join('') || '<tr><td colspan="8">none</td></tr>';
  document.getElementById('pf-fills').innerHTML = d.recent_fills.map(f =>
    `<tr><td>${{f.product}}</td><td>${{f.side}}</td><td>${{f.role}}</td><td>${{f.price.toFixed(2)}}</td>` +
    `<td>${{f.qty}}</td><td>${{f.counterparty}}</td><td>${{new Date(f.timestamp * 1000).toLocaleTimeString()}}</td></tr>`
  ).join('') || '<tr><td colspan="7">none yet</td></tr>';
}});
</script>
"""
        return page(body)

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio_page():
        body = """
<h2>Portfolio</h2>
<div class="metrics" id="pf-summary"></div>

<h2>Positions <button onclick="flattenAllPositions()" style="font-size:12px; padding:2px 8px; margin-left:8px;">Flatten all</button></h2>
<table><thead><tr><th>Product</th><th>Qty</th><th>Avg cost</th></tr></thead>
<tbody id="pf-positions"></tbody></table>

<h2>Open orders <button onclick="cancelAllOrders()" style="font-size:12px; padding:2px 8px; margin-left:8px;">Cancel all</button></h2>
<table><thead><tr><th>ID</th><th>Product</th><th>Side</th><th>Type</th><th>Qty</th><th>Price</th><th>Remaining</th><th>Status</th></tr></thead>
<tbody id="pf-orders"></tbody></table>

<h2>Recent fills</h2>
<table><thead><tr><th>Product</th><th>Side</th><th>Role</th><th>Price</th><th>Qty</th><th>Fee</th><th>Counterparty</th><th>Time</th></tr></thead>
<tbody id="pf-fills"></tbody></table>

<script>
window.__wsChannels = [];
function loadPortfolio() {
  // No fetch any more — just shows the "log in" placeholder when logged
  // out; when logged in, the shared 'portfolio' WS channel (auto-added by
  // wantedChannels() whenever a key is present) pushes render(d) on its own.
  if (!getKey()) {
    document.getElementById('pf-summary').innerHTML = '<div>log in above to see your portfolio</div>';
  }
}
function render(d) {
  if (!d || d.error) return; // handlePortfolioUpdate (shared script) already handles the error case
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
onChannel('portfolio', render);
window.onLogin = loadPortfolio;
loadPortfolio();

async function cancelAllOrders() {
  const key = getKey();
  if (!key) { alert('log in above first'); return; }
  const orders = await getOwnOrders();
  const openIds = (orders || [])
    .filter(o => o.status === 'open' || o.status === 'partially_filled')
    .map(o => o.id);
  if (!openIds.length) { alert('no open orders to cancel'); return; }
  if (!confirm(`Cancel ${openIds.length} open order(s)?`)) return;
  await cancelWorking(openIds);
}

async function flattenAllPositions() {
  const key = getKey();
  if (!key) { alert('log in above first'); return; }
  const account = await getAccount();
  const entries = Object.entries((account && account.positions) || {}).filter(([, pos]) => pos.qty !== 0);
  if (!entries.length) { alert('no open positions to flatten'); return; }
  if (!confirm(`Flatten ${entries.length} position(s) with market orders?`)) return;
  for (const [product, pos] of entries) {
    const side = pos.qty > 0 ? 'sell' : 'buy';
    try {
      const r = await fetch(API_BASE + '/orders', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-API-Key': key},
        body: JSON.stringify({product, side, type: 'market', qty: Math.abs(pos.qty)}),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        alert(`flatten ${product} failed: ${d.detail || r.status}`);
      }
    } catch (e) {
      alert(`flatten ${product} failed: ${e.message}`);
    }
  }
}
</script>
"""
        return page(body)

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page():
        body = """
<h2>Chat</h2>
<div id="chat-log" style="border:1px solid #000; height:420px; overflow-y:auto; padding:8px; font-size:13px; margin-bottom:8px;"></div>
<div style="display:flex; gap:8px;">
  <input id="chat-input" placeholder="message" style="flex:1;" maxlength="300"
         onkeydown="if (event.key === 'Enter') sendChat();">
  <button onclick="sendChat()">Send</button>
</div>
<div id="chat-msg" class="meta"></div>
<script>
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
let lastRenderedChatId = null;
function renderChatLog(messages) {
  const log = document.getElementById('chat-log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.innerHTML = messages.map(m =>
    `<div><span style="color:#666;">${new Date(m.timestamp * 1000).toLocaleTimeString()}</span> ` +
    `<b>${escapeHtml(m.account_id)}</b>: ${escapeHtml(m.text)}</div>`
  ).join('');
  const newestId = messages.length ? messages[messages.length - 1].id : null;
  if (atBottom || lastRenderedChatId === null || newestId !== lastRenderedChatId) {
    log.scrollTop = log.scrollHeight;
  }
  lastRenderedChatId = newestId;
}
window.__wsChannels = ['chat'];
onChannel('chat', renderChatLog);
onChannel('chat', markChatRead); // visiting this page reads everything currently loaded

async function sendChat() {
  const key = getKey();
  const msgEl = document.getElementById('chat-msg');
  if (!key) { msgEl.innerHTML = '<span class="err">log in above to chat</span>'; return; }
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  try {
    const r = await fetch(API_BASE + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-API-Key': key},
      body: JSON.stringify({text}),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      msgEl.innerHTML = `<span class="err">${d.detail || r.status}</span>`;
      return;
    }
    msgEl.innerHTML = '';
    input.value = '';
  } catch (e) {
    msgEl.innerHTML = `<span class="err">${e.message}</span>`;
  }
}
</script>
"""
        return page(body)

    @app.post("/data/register")
    def data_register(body: RegisterIn, request: Request):
        client_ip = request.client.host if request.client else None
        try:
            key = state.register_student(body.account_id, body.password, client_ip)
        except AccountExistsError:
            raise HTTPException(status_code=409, detail="account already registered, use login")
        record = state.auth.key_for_account(body.account_id)
        return {"account_id": body.account_id, "api_key": key, "active": record.active if record else True}

    def _channel_data(channel: str, key: str | None) -> object:
        """Dispatch for the /ws feed below — one function per channel
        name, each reusing exactly the same state read the REST routes
        these replaced used to call. Returning None means "nothing to
        send for this channel right now" (distinct from a real falsy
        payload like an empty list), so ws_feed's dedup-by-content check
        never mistakes "no data yet" for "data changed to nothing"."""
        if channel == "leaderboard":
            return state.leaderboard()
        if channel == "chat":
            return list(state.chat_messages)
        if channel.startswith("options:"):
            chain_id = channel[len("options:"):]
            if chain_id not in state.options_managers:
                return None
            return _options_payload(state, chain_id)
        if channel == "futures_matrix":
            return _futures_payload(state)
        if channel == "rfqs":
            viewer_account_id = None
            if key is not None:
                record = state.auth.resolve(key)
                viewer_account_id = record.account_id if record else None
            return [state.rfq_view(r, viewer_account_id or "") for r in state.rfq_manager.list_open_rfqs()]
        if channel.startswith("book:"):
            product = channel[len("book:"):]
            if product not in state.engine.products:
                return None
            return state.market_snapshot(product)
        if channel == "portfolio":
            if key is None:
                return None
            record = state.auth.resolve(key)
            if record is None:
                return {"error": "invalid_key"}
            return state.portfolio(record.account_id)
        if channel.startswith("portfolio_of:"):
            # Read-only view of someone else's portfolio — no key needed,
            # same public-by-design data the leaderboard already exposes
            # (see the /portfolio/{account_id} route's docstring).
            account_id = channel[len("portfolio_of:"):]
            if account_id not in state.engine.accounts:
                return None
            return state.portfolio(account_id)
        return None

    @app.websocket("/ws")
    async def ws_feed(websocket: WebSocket):
        await websocket.accept()
        channels = [c for c in (websocket.query_params.get("channels") or "").split(",") if c]
        key = websocket.query_params.get("key")
        last_sent: dict[str, str] = {}
        try:
            while True:
                for channel in channels:
                    data = _channel_data(channel, key)
                    if data is None:
                        continue
                    payload = json.dumps({"channel": channel, "data": data}, default=str)
                    # Only send when the payload actually changed — most
                    # channels (leaderboard, an idle option contract's
                    # book) don't change every tick, and there's no reason
                    # to make the client re-render identical data.
                    if last_sent.get(channel) != payload:
                        last_sent[channel] = payload
                        await websocket.send_text(payload)
                await asyncio.sleep(0.2)
        except WebSocketDisconnect:
            pass

    return app
