import base64

from fastapi.testclient import TestClient

from exchange.api_admin import create_admin_app
from exchange.api_public import create_public_app
from exchange.config import load_config
from exchange.state import AppState
from exchange.website import create_website_app


def make_state():
    config = load_config()
    return AppState(config)


def basic_auth_header(password: str) -> dict:
    token = base64.b64encode(f"anyuser:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_admin_auth_accepts_header_or_basic():
    state = make_state()
    client = TestClient(create_admin_app(state))

    r = client.get("/bots", headers={"X-Admin-Password": state.config.admin_password})
    assert r.status_code == 200

    r = client.get("/bots", headers=basic_auth_header(state.config.admin_password))
    assert r.status_code == 200

    r = client.get("/bots")
    assert r.status_code == 401

    r = client.get("/bots", headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 401


def test_admin_page_renders():
    state = make_state()
    client = TestClient(create_admin_app(state))
    r = client.get("/", headers=basic_auth_header(state.config.admin_password))
    assert r.status_code == 200
    assert "Mini-Exchange Admin" in r.text
    assert "BTC-MINI" in r.text


def test_admin_register_activate_via_admin_app():
    state = make_state()
    client = TestClient(create_admin_app(state))
    headers = {"X-Admin-Password": state.config.admin_password}

    r = client.post("/accounts", json={"account_id": "s1"}, headers=headers)
    assert r.status_code == 200
    key = r.json()["api_key"]
    assert r.json()["active"] is False

    r = client.get("/accounts", headers=headers)
    assert any(row["account_id"] == "s1" for row in r.json())

    r = client.post(f"/accounts/{key}/activate", headers=headers)
    assert r.json()["active"] is True


def test_website_book_page_and_data_endpoint():
    state = make_state()
    state.index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    state.record_price_tick(now=0.0)
    client = TestClient(create_website_app(state))
    auth = basic_auth_header(state.config.website_password)

    r = client.get("/", headers=auth)
    assert r.status_code == 200
    assert "BTC-MINI" in r.text and "ETH-MINI" in r.text

    r = client.get("/data/book/BTC-MINI", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["index_price"] == 80.0
    # book is empty here (no bots quoting in this bare state), so the chart
    # data source falls back to the index price to stay continuous
    assert body["sparkline"] == [80.0]
    assert "mid" in body and "spread_bps" in body and "last_trade_qty" in body
    assert body["session_volume_qty"] == 0
    assert body["session_volume_notional"] == 0.0


def test_record_price_tick_uses_book_mid_not_index_when_book_populated():
    state = make_state()
    state.index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)  # index = 80.0
    state.engine.get_or_create_account("mm1", 1000.0)
    state.engine.get_or_create_account("mm2", 1000.0)
    from exchange.models import OrderType, Side

    state.engine.submit_order("mm1", "BTC-MINI", Side.BUY, OrderType.LIMIT, 5, 78.0)
    state.engine.submit_order("mm2", "BTC-MINI", Side.SELL, OrderType.LIMIT, 5, 84.0)
    # actual book mid (81.0) diverges from the synthetic index (80.0) —
    # the chart should track the former, since that's what real order flow
    # (buys and sells) actually moves.
    state.record_price_tick(now=0.0)
    assert state.price_history["BTC-MINI"][-1] == 81.0


def test_website_script_defines_poll_before_first_use():
    # Regression: script tags execute in document order. poll()/renderChart()
    # must be defined before any inline per-product <script> block calls them,
    # or the call throws ReferenceError and the page renders nothing. The
    # order books page has its own on-demand fetch loop now (not the shared
    # poll() helper), so check it on the leaderboard page, which still uses
    # the shared helper and still sits after it in the same PAGE_TEMPLATE.
    state = make_state()
    client = TestClient(create_website_app(state))
    html = client.get("/leaderboard").text
    define_pos = html.index("async function poll")
    first_call_pos = html.index("poll('/data/leaderboard")
    assert define_pos < first_call_pos

    # The order books page's own fetch loop must at least be present and
    # well-formed (defined and invoked within the same script tag, so
    # cross-script-tag ordering can't break it the way poll() could).
    html = client.get("/").text
    assert "async function fetchAndRender" in html
    assert "function loop()" in html


def test_admin_page_script_defines_poll_before_first_use():
    state = make_state()
    client = TestClient(create_admin_app(state))
    html = client.get("/", headers=basic_auth_header(state.config.admin_password)).text
    define_pos = html.index("async function poll")
    call_pos = html.index("pollMarket(p)", html.index("for (const p of"))
    assert define_pos < call_pos


def test_market_snapshot_includes_last_trade_time():
    state = make_state()
    state.engine.get_or_create_account("s1", 1000.0)
    state.engine.get_or_create_account("s2", 1000.0)
    key1 = state.auth.issue_key("s1")
    key2 = state.auth.issue_key("s2")
    state.auth.activate(key1.key)
    state.auth.activate(key2.key)
    client = TestClient(create_public_app(state))
    client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "sell", "type": "limit", "price": 70.0, "qty": 1},
        headers={"X-API-Key": key1.key},
    )
    client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "buy", "type": "market", "qty": 1},
        headers={"X-API-Key": key2.key},
    )
    snap = state.market_snapshot("BTC-MINI")
    assert snap["last_trade_ts"] is not None


def test_website_has_no_site_wide_password_gate():
    # The website has no shared site password — a second auth layer on top
    # of per-account login (username/password, self-serve via the account
    # bar) was redundant friction, not real protection. Trading itself
    # still requires a valid, activated API key (enforced by the public
    # API, not this site).
    state = make_state()
    client = TestClient(create_website_app(state))
    assert client.get("/").status_code == 200
    assert client.get("/portfolio").status_code == 200


def test_portfolio_data_endpoint():
    state = make_state()
    state.engine.get_or_create_account("s1", 1000.0)
    state.engine.get_or_create_account("s2", 1000.0)
    r1 = state.auth.issue_key("s1")
    r2 = state.auth.issue_key("s2")
    state.auth.activate(r1.key)
    state.auth.activate(r2.key)

    public_client = TestClient(create_public_app(state))
    public_client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "sell", "type": "limit", "price": 70.0, "qty": 2},
        headers={"X-API-Key": r1.key},
    )
    public_client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "buy", "type": "market", "qty": 2},
        headers={"X-API-Key": r2.key},
    )

    website_client = TestClient(create_website_app(state))
    auth = basic_auth_header(state.config.website_password)
    r = website_client.get(f"/data/portfolio?key={r2.key}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == "s2"
    assert body["positions"]["BTC-MINI"] == {"qty": 2, "avg_cost": 70.0}
    # s2 was the taker (market buy): pays taker_fee_bps on the fill notional,
    # no realized PnL from position PnL since this only opened a position.
    expected_taker_fee = 70.0 * 2 * state.config.fees.taker_bps / 10_000
    assert abs(body["realized_pnl"] - (-expected_taker_fee)) < 1e-9
    assert body["recent_fills"][0]["counterparty"] == "s1"
    assert body["recent_fills"][0]["role"] == "taker"
    assert abs(body["recent_fills"][0]["fee"] - expected_taker_fee) < 1e-9

    r = website_client.get("/data/portfolio?key=bogus", headers=auth)
    assert r.status_code == 404


def test_public_register_is_active_immediately_no_admin_gate():
    state = make_state()
    client = TestClient(create_public_app(state))
    r = client.post("/register", json={"account_id": "s1", "password": "pw"})
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    # the returned key trades immediately, no admin activation needed
    order_r = client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "buy", "type": "limit", "price": 70.0, "qty": 1},
        headers={"X-API-Key": body["api_key"]},
    )
    assert order_r.status_code == 200


def test_public_register_twice_requires_login_not_reregister():
    state = make_state()
    client = TestClient(create_public_app(state))
    client.post("/register", json={"account_id": "s1", "password": "pw"})
    r = client.post("/register", json={"account_id": "s1", "password": "pw"})
    assert r.status_code == 409


def test_public_login_round_trip():
    state = make_state()
    client = TestClient(create_public_app(state))
    reg = client.post("/register", json={"account_id": "s1", "password": "pw"}).json()

    ok = client.post("/login", json={"account_id": "s1", "password": "pw"})
    assert ok.status_code == 200
    assert ok.json()["api_key"] == reg["api_key"]

    bad = client.post("/login", json={"account_id": "s1", "password": "wrong"})
    assert bad.status_code == 401


def test_admin_trading_account_excluded_from_leaderboard():
    state = make_state()
    state.engine.get_or_create_account("s1", 1000.0)
    rows = state.leaderboard()
    assert all(r["account_id"] != "admin" for r in rows)


def test_admin_trading_account_exists_and_is_well_capitalized():
    state = make_state()
    assert "admin" in state.engine.accounts
    assert state.engine.accounts["admin"].cash == 1_000_000.0
    assert "admin" in state.engine.unlimited_position_accounts
    client = TestClient(create_public_app(state))
    r = client.post("/login", json={"account_id": "admin", "password": state.config.admin_password})
    assert r.status_code == 200
    key = r.json()["api_key"]
    order_r = client.post(
        "/orders",
        json={"product": "BTC-MINI", "side": "buy", "type": "limit", "price": 70.0, "qty": 100},
        headers={"X-API-Key": key},
    )
    assert order_r.status_code == 200  # would be rejected for a normal account (MAX_POSITION=15)


def test_website_register_proxies_to_state():
    state = make_state()
    client = TestClient(create_website_app(state))
    auth = basic_auth_header(state.config.website_password)
    r = client.post("/data/register", json={"account_id": "s1", "password": "pw"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["account_id"] == "s1"
    assert r.json()["active"] is True
    assert "s1" in state.engine.accounts


def test_admin_synthetic_params_endpoint():
    state = make_state()
    client = TestClient(create_admin_app(state))
    headers = {"X-Admin-Password": state.config.admin_password}

    r = client.post("/synthetic/params", json={"product": "BTC-MINI", "annual_volatility": 0.9}, headers=headers)
    assert r.status_code == 200
    assert r.json()["annual_volatility"] == 0.9
    # live-mutable: state's own dict reflects the change immediately
    assert state.synthetic_params["BTC-MINI"]["annual_volatility"] == 0.9

    r = client.get("/synthetic/params", headers=headers)
    assert r.json()["BTC-MINI"]["annual_volatility"] == 0.9


def test_admin_spread_scale_endpoint():
    state = make_state()
    client = TestClient(create_admin_app(state))
    headers = {"X-Admin-Password": state.config.admin_password}

    r = client.post("/bots/spread_scale", json={"scale": 0.5}, headers=headers)
    assert r.status_code == 200
    assert state.bot_manager.global_spread_scale == 0.5

    r = client.post("/bots/spread_scale", json={"scale": -1}, headers=headers)
    assert r.status_code == 400


def test_admin_toggle_mm_bot_active():
    state = make_state()
    client = TestClient(create_admin_app(state))
    headers = {"X-Admin-Password": state.config.admin_password}
    account_id = state.bot_manager.mm_bots[0].config.account_id

    r = client.post(f"/bots/{account_id}/params", json={"active": False}, headers=headers)
    assert r.status_code == 200
    assert r.json()["config"]["active"] is False
    assert state.bot_manager.mm_bots[0].config.active is False

    r = client.post(f"/bots/{account_id}/params", json={"active": True}, headers=headers)
    assert r.json()["config"]["active"] is True


def test_admin_toggle_noise_bot_active():
    state = make_state()
    client = TestClient(create_admin_app(state))
    headers = {"X-Admin-Password": state.config.admin_password}
    account_id = state.bot_manager.noise_bots[0].config.account_id

    r = client.post(f"/bots/noise/{account_id}/params", json={"active": False}, headers=headers)
    assert r.status_code == 200
    assert r.json()["config"]["active"] is False
    assert state.bot_manager.noise_bots[0].config.active is False

    r = client.post("/bots/noise/nonexistent/params", json={"active": False}, headers=headers)
    assert r.status_code == 404


def test_inactive_noise_bot_never_trades():
    state = make_state()
    state.index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    bot = state.bot_manager.noise_bots[0]
    bot.config.active = False
    for i in range(200):
        bot.maybe_trade(state.engine, state.index_service, now=i * 0.5)
    assert len(state.engine.trade_tape) == 0


def test_admin_market_snapshot_endpoint():
    state = make_state()
    state.index_service.on_raw_tick("BTC-MINI", 80000.0, now=0.0)
    client = TestClient(create_admin_app(state))
    r = client.get("/market/BTC-MINI", headers={"X-Admin-Password": state.config.admin_password})
    assert r.status_code == 200
    body = r.json()
    assert body["index_price"] == 80.0
    assert "mid" in body and "spread_bps" in body
