"""Public student API — build-spec.md §8."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .auth import AccountExistsError, ApiKeyRecord
from .engine import OrderRejected
from .ledger import equity, unrealized_pnl
from .models import OrderType, Side
from .rate_limit import TokenBucketLimiter
from .rfq import DEFAULT_TTL_SECONDS, RFQError
from .state import AppState


class OrderIn(BaseModel):
    product: str
    side: Side
    type: OrderType
    qty: int
    price: Optional[float] = None


class RegisterIn(BaseModel):
    account_id: str
    password: str


class LoginIn(BaseModel):
    account_id: str
    password: str


class ChatIn(BaseModel):
    text: str


class RFQIn(BaseModel):
    product: str
    side: Side
    qty: int
    ttl_seconds: float = DEFAULT_TTL_SECONDS


class QuoteIn(BaseModel):
    price: float
    qty: Optional[int] = None


def create_public_app(state: AppState) -> FastAPI:
    app = FastAPI(title=f"{state.config.exchange_name} Public API")
    limiter = TokenBucketLimiter(state.config.rate_limit.requests_per_second, state.config.rate_limit.burst)

    # The website (a different origin/port) trades on a student's behalf
    # from the browser — the ladder and portfolio pages call this API
    # directly with the student's own key, not through the website's
    # backend, so the browser needs CORS clearance for that origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[state.config.network.website_base_url],
        allow_methods=["*"],
        allow_headers=["*"],
        # Without this, every cross-origin call carrying X-API-Key (i.e.
        # every trade/cancel/orders/account/fills request) gets its own
        # OPTIONS preflight round trip, and the browser re-preflights on
        # every single call rather than caching it. Invisible on localhost
        # (sub-ms either way) but on a real network this doubles the
        # latency of every click — max_age lets the browser cache the
        # preflight result instead of repeating it.
        max_age=600,
    )

    def auth_dep(x_api_key: str = Header(...)) -> ApiKeyRecord:
        record = state.auth.resolve(x_api_key)
        if record is None:
            raise HTTPException(status_code=401, detail="invalid or inactive API key")
        if not limiter.allow(x_api_key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return record

    @app.post("/register")
    def register(body: RegisterIn, request: Request):
        """Self-serve: username + password. Active immediately, no admin
        approval step — deposits the starting cash and generates a key
        right away, unless this IP has already self-registered more than
        AuthStore.MAX_SELF_SERVE_ACCOUNTS_PER_IP accounts, in which case the
        new account is created but stays inactive until an admin approves it
        (POST /accounts/{key}/activate on the admin API)."""
        client_ip = request.client.host if request.client else None
        try:
            key = state.register_student(body.account_id, body.password, client_ip)
        except AccountExistsError:
            raise HTTPException(status_code=409, detail="account already registered, use /login")
        record = state.auth.key_for_account(body.account_id)
        return {"account_id": body.account_id, "api_key": key, "active": record.active if record else True}

    @app.post("/login")
    def login(body: LoginIn):
        key = state.login_student(body.account_id, body.password)
        if key is None:
            raise HTTPException(status_code=401, detail="bad username or password")
        return {"account_id": body.account_id, "api_key": key}

    @app.get("/products")
    def list_products():
        now = time.time()
        return [
            {
                "symbol": symbol,
                "underlying": p.underlying,
                "contract_size": p.contract_size,
                "leverage": p.leverage,
                "index_price": state.index_service.get_index_price(symbol, now),
            }
            for symbol, p in state.config.products.items()
        ]

    @app.get("/book/{product}")
    def get_book(product: str, auth: ApiKeyRecord = Depends(auth_dep)):
        if product not in state.engine.products:
            raise HTTPException(status_code=404, detail="unknown product")
        return state.engine.book_snapshot(product)

    @app.websocket("/book/{product}/stream")
    async def stream_book(websocket: WebSocket, product: str):
        if product not in state.engine.products:
            await websocket.close(code=4404)
            return
        api_key = websocket.query_params.get("api_key")
        record = state.auth.resolve(api_key) if api_key else None
        if record is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(state.engine.book_snapshot(product))
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass

    @app.post("/orders")
    def submit_order(order: OrderIn, auth: ApiKeyRecord = Depends(auth_dep)):
        if not state.is_tradeable(order.product):
            raise HTTPException(status_code=400, detail=f"{order.product} is currently disabled")
        try:
            result = state.engine.submit_order(
                auth.account_id, order.product, order.side, order.type, order.qty, order.price
            )
        except OrderRejected as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return _order_out(result)

    @app.delete("/orders/{order_id}")
    def cancel_order(order_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            result = state.engine.cancel_order(order_id, auth.account_id)
        except OrderRejected as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return _order_out(result)

    @app.get("/orders")
    def list_orders(auth: ApiKeyRecord = Depends(auth_dep)):
        return [_order_out(o) for o in state.engine.orders_by_account.get(auth.account_id, {}).values()]

    @app.get("/orders/{order_id}")
    def get_order(order_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        order = state.engine.orders.get(order_id)
        if order is None or order.account_id != auth.account_id:
            raise HTTPException(status_code=404, detail="no such order")
        return _order_out(order)

    @app.get("/fills")
    def list_fills(auth: ApiKeyRecord = Depends(auth_dep)):
        return [state.fill_view(f, auth.account_id) for f in state.engine.fills_by_account.get(auth.account_id, [])]

    @app.get("/account")
    def get_account(auth: ApiKeyRecord = Depends(auth_dep)):
        account = state.engine.accounts[auth.account_id]
        prices = state.index_prices()
        starting_cash = state.starting_cash_by_account.get(auth.account_id, state.config.accounts.starting_cash)
        return {
            "account_id": account.id,
            "cash": account.cash,
            "balance": account.cash,
            "realized_pnl": account.cash - starting_cash,
            "positions": {p: {"qty": pos.qty, "avg_cost": pos.avg_cost} for p, pos in account.positions.items() if pos.qty != 0},
            "unrealized_pnl": unrealized_pnl(account, prices),
            "equity": equity(account, prices),
            "frozen": account.frozen,
        }

    @app.get("/leaderboard")
    def get_leaderboard():
        return state.leaderboard()

    @app.get("/portfolio/{account_id}")
    def get_portfolio_of(account_id: str):
        """No auth: read-only view of *any* account's portfolio — same
        shape your own GET /account plus fills/orders returns. Not
        materially more sensitive than GET /leaderboard, which already
        publishes every account's cash/equity/positions with no auth; this
        just adds the order/fill detail the website's own
        /portfolio/{account_id} page shows."""
        if account_id not in state.engine.accounts:
            raise HTTPException(status_code=404, detail="no such account")
        return state.portfolio(account_id)

    @app.post("/chat")
    def post_chat(body: ChatIn, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            return state.post_chat_message(auth.account_id, body.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- RFQs: any account can ask for a quote, any account (other than the
    # requester) can supply liquidity by quoting back a firm price. See
    # rfq.py's module docstring for why this is deliberately not book-based.
    @app.post("/rfqs")
    def create_rfq(body: RFQIn, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            r = state.rfq_manager.create_rfq(auth.account_id, body.product, body.side, body.qty, body.ttl_seconds)
        except RFQError as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return state.rfq_view(r, auth.account_id)

    @app.get("/rfqs")
    def list_rfqs(product: Optional[str] = None, auth: ApiKeyRecord = Depends(auth_dep)):
        return [state.rfq_view(r, auth.account_id) for r in state.rfq_manager.list_open_rfqs(product)]

    @app.get("/rfqs/{rfq_id}")
    def get_rfq(rfq_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        r = state.rfq_manager.get_rfq(rfq_id)
        if r is None:
            raise HTTPException(status_code=404, detail="no such RFQ")
        return state.rfq_view(r, auth.account_id)

    @app.delete("/rfqs/{rfq_id}")
    def cancel_rfq(rfq_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            r = state.rfq_manager.cancel_rfq(rfq_id, auth.account_id)
        except RFQError as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return state.rfq_view(r, auth.account_id)

    @app.post("/rfqs/{rfq_id}/quotes")
    def submit_quote(rfq_id: int, body: QuoteIn, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            q = state.rfq_manager.submit_quote(rfq_id, auth.account_id, body.price, body.qty)
        except RFQError as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return state.quote_view(q)

    @app.delete("/rfqs/{rfq_id}/quotes/{quote_id}")
    def withdraw_quote(rfq_id: int, quote_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            q = state.rfq_manager.withdraw_quote(rfq_id, quote_id, auth.account_id)
        except RFQError as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return state.quote_view(q)

    @app.post("/rfqs/{rfq_id}/quotes/{quote_id}/accept")
    def accept_quote(rfq_id: int, quote_id: int, auth: ApiKeyRecord = Depends(auth_dep)):
        try:
            fill, r = state.rfq_manager.accept_quote(rfq_id, quote_id, auth.account_id)
        except RFQError as e:
            raise HTTPException(status_code=400, detail=e.reason)
        return {"fill": state.fill_view(fill, auth.account_id), "rfq": state.rfq_view(r, auth.account_id)}

    return app


def _order_out(order) -> dict:
    return {
        "id": order.id,
        "product": order.product,
        "side": order.side.value,
        "type": order.type.value,
        "qty": order.qty,
        "price": order.price,
        "remaining_qty": order.remaining_qty,
        "status": order.status.value,
    }
