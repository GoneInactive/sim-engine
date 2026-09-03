"""Public student API — build-spec.md §8."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .auth import AccountExistsError, ApiKeyRecord
from .engine import OrderRejected
from .ledger import equity, unrealized_pnl
from .models import OrderType, Side
from .rate_limit import TokenBucketLimiter
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


def create_public_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Mini-Exchange Public API")
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
    )

    def auth_dep(x_api_key: str = Header(...)) -> ApiKeyRecord:
        record = state.auth.resolve(x_api_key)
        if record is None:
            raise HTTPException(status_code=401, detail="invalid or inactive API key")
        if not limiter.allow(x_api_key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return record

    @app.post("/register")
    def register(body: RegisterIn):
        """Self-serve: username + password. Active immediately, no admin
        approval step — deposits the starting cash and generates a key
        right away."""
        try:
            key = state.register_student(body.account_id, body.password)
        except AccountExistsError:
            raise HTTPException(status_code=409, detail="account already registered, use /login")
        return {"account_id": body.account_id, "api_key": key, "active": True}

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
                "max_position": p.max_position,
                "index_price": state.index_service.get_index_price(symbol, now),
            }
            for symbol, p in state.config.products.items()
        ]

    @app.get("/book/{product}")
    def get_book(product: str, auth: ApiKeyRecord = Depends(auth_dep)):
        if product not in state.config.products:
            raise HTTPException(status_code=404, detail="unknown product")
        return state.engine.book_snapshot(product)

    @app.websocket("/book/{product}/stream")
    async def stream_book(websocket: WebSocket, product: str):
        if product not in state.config.products:
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
