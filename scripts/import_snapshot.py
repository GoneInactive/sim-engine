#!/usr/bin/env python3
"""One-time bootstrap: load the JSON produced by dump_state.py into the
new persistence database, so today's live accounts survive the restart
that deploys persistence itself.

Run this ONCE, after the database/tables exist (the app creates the
schema on its own boot, so either start the new code once against an
otherwise-empty database first and stop it again, or just let this
script's own --ensure-schema flag do it) and BEFORE restarting the app
onto the new persistence-enabled code for real.

Import strategy — read this before running:
  - Each account's `starting_cash` is set to its *current* snapshot cash
    (not the system's normal 1000.0 starting cash), and one synthetic
    "opening" fill is inserted per open position at its exact avg_cost.
    That reproduces the exact cash/position numbers the live system shows
    right now, and — because starting_cash becomes the new anchor point —
    stays exactly right on every future restart too.
  - Trade-off: pre-migration fills are NOT imported into the fills table.
    Baking their net effect into starting_cash while *also* replaying them
    on a future restart would double-count every realized gain/loss —
    only one of those two things can be the source of truth, and
    starting_cash is the simpler, more robust one. Practically: students'
    /fills history starts fresh from this migration forward; their
    cash/position numbers do not reset even by a cent.
  - Orders ARE imported as-is (id, status, etc.) — purely historical
    display, no cash/position math depends on them, so there's no
    double-counting risk.
  - Passwords cannot be migrated (correctly — they're salted/hashed and no
    endpoint ever returns the hash, let alone the original password). Each
    imported account is instead left with NO password set — the exact
    same state as an admin-issued-but-not-yet-claimed account. AuthStore's
    existing "claim" logic (see auth.py's register() docstring) means the
    *next* time that account_id calls POST /register with any password —
    which every existing student script and the website's own login form
    already does automatically, using whatever password that student
    already has memorized/hardcoded — the account silently accepts it as
    its new password. No student needs to be told anything or change
    anything; every already-running bot script just keeps working.
    Trade-off, accepted deliberately: until each account is claimed this
    way, anyone who knows/guesses that account_id could claim it first
    with a password of their choosing. Restart during this migration
    promptly and avoid publicizing the full account list in that window.
  - Synthetic opening-fill ids are assigned starting at 900_000_000 to
    avoid any realistic collision with real fill ids (which start at 1).

Usage:
    python3 scripts/import_snapshot.py --snapshot snapshot-123.json --database-url postgresql+asyncpg://sim_engine:sim_engine@127.0.0.1:5432/sim_engine
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exchange.persistence import (  # noqa: E402
    accounts_table, chat_table, credentials_table, ensure_schema, fills_table, make_engine, orders_table,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

SYNTHETIC_FILL_ID_START = 900_000_000

# The house trading account isn't a real student — it's recreated fresh on
# every boot with whatever config.yaml's admin.password currently is
# (state.py's AppState.__init__). Importing it here would stamp its
# credentials with a random password that AppState never wrote and replay
# would then load *after* AppState already set it correctly, breaking
# "log into the website as admin with the admin panel password."
SKIP_ACCOUNT_IDS = {"admin"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="path to a dump_state.py JSON output")
    ap.add_argument("--database-url", required=True)
    args = ap.parse_args()

    snapshot = json.loads(Path(args.snapshot).read_text())
    engine = make_engine(args.database_url)
    await ensure_schema(engine)

    claimed_accounts: list[str] = []
    synthetic_fill_id = SYNTHETIC_FILL_ID_START

    async with engine.begin() as conn:
        for entry in snapshot["accounts"]:
            account_id = entry["account_id"]
            if account_id in SKIP_ACCOUNT_IDS:
                continue
            account = entry["account"]
            cash = account["cash"]
            claimed_accounts.append(account_id)

            # dump_state.py's snapshot never included api_key (it only
            # used it locally, to authenticate its own read calls) — not
            # a problem, since no student script hardcodes one (they all
            # call register-or-login every run, per spread-trader.py's
            # register_or_login()). No password_salt/password_hash either
            # — deliberately: see the "claim" strategy in the module
            # docstring above. That first post-migration /register call
            # (from the student's own unchanged script or the website's
            # login form, using whatever password they already know) sets
            # it, exactly like claiming a fresh admin-issued account.
            api_key = secrets.token_urlsafe(24)

            await conn.execute(
                pg_insert(accounts_table).values(
                    account_id=account_id, starting_cash=cash, frozen=account.get("frozen", False),
                    created_at=snapshot["captured_at"],
                ).on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={"starting_cash": cash, "frozen": account.get("frozen", False)},
                )
            )

            await conn.execute(
                pg_insert(credentials_table).values(
                    account_id=account_id, api_key=api_key, password_salt=None, password_hash=None,
                    active=entry.get("active", True), updated_at=snapshot["captured_at"],
                ).on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={
                        "api_key": api_key, "password_salt": None, "password_hash": None,
                        "active": entry.get("active", True), "updated_at": snapshot["captured_at"],
                    },
                )
            )

            for product, pos in account.get("positions", {}).items():
                if pos["qty"] == 0:
                    continue
                side = "buy" if pos["qty"] > 0 else "sell"
                await conn.execute(
                    pg_insert(fills_table).values(
                        id=synthetic_fill_id, product=product, price=pos["avg_cost"], qty=abs(pos["qty"]),
                        timestamp=snapshot["captured_at"], maker_order_id=0, taker_order_id=0,
                        maker_account_id=account_id, taker_account_id=account_id, taker_side=side,
                        maker_fee_bps=None, taker_fee_bps=None,
                    ).on_conflict_do_nothing(index_elements=["id"])
                )
                synthetic_fill_id += 1

            for order in entry.get("orders", []):
                await conn.execute(
                    pg_insert(orders_table).values(
                        id=order["id"], account_id=account_id, product=order["product"], side=order["side"],
                        type=order["type"], qty=order["qty"], price=order["price"],
                        remaining_qty=order["remaining_qty"], status=order["status"],
                        timestamp=snapshot["captured_at"],
                    ).on_conflict_do_nothing(index_elements=["id"])
                )

        for message in snapshot.get("chat", []):
            await conn.execute(
                pg_insert(chat_table).values(
                    id=message["id"], account_id=message["account_id"], text=message["text"],
                    timestamp=message["timestamp"],
                ).on_conflict_do_nothing(index_elements=["id"])
            )

    print(f"imported {len(claimed_accounts)} account(s), {len(snapshot.get('chat', []))} chat message(s)", file=sys.stderr)
    print(
        "\nNo passwords were set — each account will silently claim whatever password its next "
        "/register call uses (the student's own script or login form, unchanged). Nothing to give "
        "anyone. Restart promptly to keep the unclaimed window short.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
