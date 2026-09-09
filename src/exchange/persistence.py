"""Durable storage: accounts, credentials, orders, fills, chat survive a
process restart. Everything else (the order book itself, bot accounts,
synthetic feed state) stays exactly as ephemeral as it already was — see
build-spec.md §11: "order book itself can be in-memory with a
Postgres-backed event log for durability/restart-recovery."

Two halves:
  - `PersistenceLog`: a cheap, sync-callable write queue any code path
    (including MatchingEngine's internals, which are plain sync functions)
    can push events onto without awaiting anything. A background task
    (`drain_forever`) actually writes them to Postgres.
  - `replay_into`: boot-time read of everything back into a fresh
    `AppState`, run once before the app starts serving traffic.

Fills and chat messages are immutable historical events (append-only,
`ON CONFLICT DO NOTHING` so a retried write is harmless). Accounts,
credentials, and orders only need their *current* state restored, so
they're upserted (`ON CONFLICT DO UPDATE`) — replaying the full history of
every status transition an order went through isn't needed, only where it
ended up.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .ledger import apply_fill
from .models import Fill, Order, OrderStatus, OrderType, Side, advance_fill_id_counter, advance_order_id_counter

logger = logging.getLogger("exchange.persistence")

metadata = sa.MetaData()

accounts_table = sa.Table(
    "accounts", metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("starting_cash", sa.Float, nullable=False),
    sa.Column("frozen", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.Float, nullable=False),
)

credentials_table = sa.Table(
    "credentials", metadata,
    sa.Column("account_id", sa.Text, primary_key=True),
    sa.Column("api_key", sa.Text, nullable=False),
    sa.Column("password_salt", sa.LargeBinary, nullable=True),
    sa.Column("password_hash", sa.LargeBinary, nullable=True),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

orders_table = sa.Table(
    "orders", metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("qty", sa.Integer, nullable=False),
    sa.Column("price", sa.Float, nullable=True),
    sa.Column("remaining_qty", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("timestamp", sa.Float, nullable=False),
)

fills_table = sa.Table(
    "fills", metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("product", sa.Text, nullable=False),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("qty", sa.Integer, nullable=False),
    sa.Column("timestamp", sa.Float, nullable=False),
    sa.Column("maker_order_id", sa.BigInteger, nullable=False),
    sa.Column("taker_order_id", sa.BigInteger, nullable=False),
    sa.Column("maker_account_id", sa.Text, nullable=False),
    sa.Column("taker_account_id", sa.Text, nullable=False),
    sa.Column("taker_side", sa.Text, nullable=False),
    # The fee *rate* active at the moment of this fill (not necessarily
    # today's config — fees could change between restarts), so replay can
    # reproduce the exact cash deduction _match() applied. NULL for a
    # settlement fill (MatchingEngine.settle_fill), which never charges a
    # fee — that's a distinct case from "0 bps", not the same as it.
    sa.Column("maker_fee_bps", sa.Float, nullable=True),
    sa.Column("taker_fee_bps", sa.Float, nullable=True),
)

chat_table = sa.Table(
    "chat_messages", metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("account_id", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("timestamp", sa.Float, nullable=False),
)


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


class PersistenceLog:
    """Owns the write-behind queue. `log_*` methods are plain sync calls —
    safe from anywhere, including inside MatchingEngine's sync internals —
    that just append a dict to an in-memory queue. `drain_forever` is the
    only thing that ever touches the database for writes."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._stop = False

    # -- producers (sync, cheap, called from anywhere) ---------------------
    def log_account(self, account_id: str, starting_cash: float, frozen: bool = False, created_at: float | None = None) -> None:
        self._queue.put_nowait({
            "kind": "account", "account_id": account_id, "starting_cash": starting_cash,
            "frozen": frozen, "created_at": created_at if created_at is not None else time.time(),
        })

    def log_credentials(
        self, account_id: str, api_key: str, active: bool,
        password_salt: bytes | None, password_hash: bytes | None,
    ) -> None:
        self._queue.put_nowait({
            "kind": "credentials", "account_id": account_id, "api_key": api_key, "active": active,
            "password_salt": password_salt, "password_hash": password_hash, "updated_at": time.time(),
        })

    def log_order(self, order: Order) -> None:
        self._queue.put_nowait({"kind": "order", "order": order})

    def log_fill(self, fill: Fill, maker_fee_bps: float | None, taker_fee_bps: float | None) -> None:
        self._queue.put_nowait({
            "kind": "fill", "fill": fill, "maker_fee_bps": maker_fee_bps, "taker_fee_bps": taker_fee_bps,
        })

    def log_chat(self, message: dict) -> None:
        self._queue.put_nowait({"kind": "chat", "message": message})

    # -- consumer ------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True

    async def drain_forever(self, batch_size: int = 50, idle_sleep: float = 0.2) -> None:
        while not self._stop:
            batch = [await self._queue.get()]
            while len(batch) < batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._write_batch(batch)
            except Exception:
                # A DB hiccup must never take the live exchange down with
                # it — log loudly and keep going. The events in this batch
                # are lost (this is the "small window" the design accepts),
                # but every subsequent event still gets through.
                logger.exception("persistence: failed to write batch of %d event(s)", len(batch))
            await asyncio.sleep(idle_sleep)

    async def _write_batch(self, batch: list[dict]) -> None:
        async with self.engine.begin() as conn:
            for event in batch:
                kind = event["kind"]
                if kind == "account":
                    stmt = pg_insert(accounts_table).values(
                        account_id=event["account_id"], starting_cash=event["starting_cash"],
                        frozen=event["frozen"], created_at=event["created_at"],
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["account_id"],
                        set_={"frozen": stmt.excluded.frozen},
                    )
                    await conn.execute(stmt)
                elif kind == "credentials":
                    stmt = pg_insert(credentials_table).values(
                        account_id=event["account_id"], api_key=event["api_key"], active=event["active"],
                        password_salt=event["password_salt"], password_hash=event["password_hash"],
                        updated_at=event["updated_at"],
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["account_id"],
                        set_={
                            "api_key": stmt.excluded.api_key, "active": stmt.excluded.active,
                            "password_salt": stmt.excluded.password_salt,
                            "password_hash": stmt.excluded.password_hash,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                    await conn.execute(stmt)
                elif kind == "order":
                    o = event["order"]
                    stmt = pg_insert(orders_table).values(
                        id=o.id, account_id=o.account_id, product=o.product, side=o.side.value,
                        type=o.type.value, qty=o.qty, price=o.price, remaining_qty=o.remaining_qty,
                        status=o.status.value, timestamp=o.timestamp,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["id"],
                        set_={"remaining_qty": stmt.excluded.remaining_qty, "status": stmt.excluded.status},
                    )
                    await conn.execute(stmt)
                elif kind == "fill":
                    f = event["fill"]
                    stmt = pg_insert(fills_table).values(
                        id=f.id, product=f.product, price=f.price, qty=f.qty, timestamp=f.timestamp,
                        maker_order_id=f.maker_order_id, taker_order_id=f.taker_order_id,
                        maker_account_id=f.maker_account_id, taker_account_id=f.taker_account_id,
                        taker_side=f.taker_side.value,
                        maker_fee_bps=event["maker_fee_bps"], taker_fee_bps=event["taker_fee_bps"],
                    ).on_conflict_do_nothing(index_elements=["id"])
                    await conn.execute(stmt)
                elif kind == "chat":
                    m = event["message"]
                    stmt = pg_insert(chat_table).values(
                        id=m["id"], account_id=m["account_id"], text=m["text"], timestamp=m["timestamp"],
                    ).on_conflict_do_nothing(index_elements=["id"])
                    await conn.execute(stmt)


async def replay_into(state, engine: AsyncEngine) -> None:
    """Boot-time restore, called once from app.py before any server starts
    accepting requests. Order matters: credentials before accounts (both
    are independent reads, order doesn't actually matter between them, but
    accounts before fills does — fills need the account to already exist
    via get_or_create_account)."""
    async with engine.begin() as conn:
        cred_rows = (await conn.execute(sa.select(credentials_table))).mappings().all()
        account_rows = (await conn.execute(sa.select(accounts_table))).mappings().all()
        order_rows = (await conn.execute(sa.select(orders_table).order_by(orders_table.c.id))).mappings().all()
        fill_rows = (await conn.execute(sa.select(fills_table).order_by(fills_table.c.id))).mappings().all()
        chat_rows = (await conn.execute(sa.select(chat_table).order_by(chat_table.c.id))).mappings().all()

    if not (cred_rows or account_rows or fill_rows):
        logger.info("persistence: nothing to restore (empty database)")
        return

    for row in cred_rows:
        state.auth.restore_record(
            row["account_id"], row["api_key"], row["active"], row["password_salt"], row["password_hash"],
        )

    for row in account_rows:
        account = state.engine.get_or_create_account(row["account_id"], row["starting_cash"])
        account.frozen = row["frozen"]
        state.starting_cash_by_account[row["account_id"]] = row["starting_cash"]

    for row in fill_rows:
        account_id = row["taker_account_id"]
        account = state.engine.accounts.get(account_id)
        # A fill can also touch the maker side, which needs its own
        # apply_fill call too — replaying is symmetric with how _match
        # itself calls apply_fill once per side, same fill.
        if account is not None:
            apply_fill(account, row["product"], Side(row["taker_side"]), row["qty"], row["price"])
        maker_id = row["maker_account_id"]
        maker_account = None
        if maker_id != account_id:
            maker_account = state.engine.accounts.get(maker_id)
            if maker_account is not None:
                apply_fill(maker_account, row["product"], Side(row["taker_side"]).opposite, row["qty"], row["price"])
        # _match() deducts fees directly from cash, outside apply_fill —
        # replay must reproduce that exact deduction too, using the fee
        # *rate* that was actually active at the time (not whatever the
        # current config says), or restored cash silently comes back short
        # every fee ever charged. NULL means a fee-free settlement fill.
        if row["maker_fee_bps"] is not None:
            notional = row["price"] * row["qty"]
            if maker_account is not None:
                maker_account.cash -= notional * row["maker_fee_bps"] / 10_000
            if account is not None:
                account.cash -= notional * row["taker_fee_bps"] / 10_000
        fill = Fill(
            id=row["id"], product=row["product"], price=row["price"], qty=row["qty"],
            timestamp=row["timestamp"], maker_order_id=row["maker_order_id"], taker_order_id=row["taker_order_id"],
            maker_account_id=maker_id, taker_account_id=account_id, taker_side=Side(row["taker_side"]),
        )
        state.engine.trade_tape.append(fill)
        state.engine.fills_by_account.setdefault(maker_id, []).append(fill)
        if account_id != maker_id:
            state.engine.fills_by_account.setdefault(account_id, []).append(fill)
        if row["product"] in state.engine.volume_qty:
            state.engine.volume_qty[row["product"]] += row["qty"]
            state.engine.volume_notional[row["product"]] += row["price"] * row["qty"]

    for row in order_rows:
        status = OrderStatus(row["status"])
        remaining_qty = row["remaining_qty"]
        # The book itself never survives a restart — anything that was
        # still resting when we went down genuinely isn't resting anymore,
        # so restore it as cancelled rather than lying that it's still open
        # with nothing backing it in a book that no longer exists.
        if status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            status = OrderStatus.CANCELLED
        order = Order(
            id=row["id"], account_id=row["account_id"], product=row["product"], side=Side(row["side"]),
            type=OrderType(row["type"]), qty=row["qty"], price=row["price"], remaining_qty=remaining_qty,
            status=status, timestamp=row["timestamp"],
        )
        state.engine.orders[order.id] = order
        state.engine.orders_by_account.setdefault(order.account_id, {})[order.id] = order

    for row in chat_rows:
        state.chat_messages.append({
            "id": row["id"], "account_id": row["account_id"], "text": row["text"], "timestamp": row["timestamp"],
        })

    max_order_id = max((r["id"] for r in order_rows), default=0)
    max_fill_id = max((r["id"] for r in fill_rows), default=0)
    advance_order_id_counter(max_order_id)
    advance_fill_id_counter(max_fill_id)
    max_chat_id = max((r["id"] for r in chat_rows), default=0)
    state._next_chat_id = itertools.count(max_chat_id + 1)

    logger.info(
        "persistence: restored %d account(s), %d order(s), %d fill(s), %d chat message(s)",
        len(account_rows), len(order_rows), len(fill_rows), len(chat_rows),
    )
