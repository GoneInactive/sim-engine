#!/usr/bin/env python3
"""One-off, read-only snapshot of a *running* sim-engine server's data.

Hits only existing GET endpoints (admin + public API) — never writes
anything to the server, never requires a restart or code deploy. Safe to
run against a live server with active students trading.

Usage (run ON the server, or against its public IP if the ports are
reachable):

    python3 dump_state.py \
        --admin-url http://127.0.0.1:8001 \
        --api-url http://127.0.0.1:8000 \
        --admin-password dev \
        --out snapshot.json

Captures, per registered account: cash/positions/pnl/frozen (GET /account),
every order ever placed (GET /orders), every fill ever received (GET
/fills) — all uncapped, unlike the website's portfolio view which only
shows the last 50 fills. Also captures the leaderboard and the full chat
log. Does NOT capture: account passwords (hashed, and not needed — a
restore would need each student to pick a new password anyway), bot
accounts (mm_*/noise_*/arb_* — they're not registered through the admin
key-issuance path this script reads from, and are trivially respawned),
or anything beyond what these endpoints expose (e.g. cancelled-order
history is included since GET /orders returns every order regardless of
status).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _get(url: str, headers: dict | None = None) -> object:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-url", required=True, help="e.g. http://127.0.0.1:8001")
    ap.add_argument("--api-url", required=True, help="e.g. http://127.0.0.1:8000")
    ap.add_argument("--website-url", default=None, help="e.g. http://127.0.0.1:8090 (for chat log; optional)")
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--out", default=f"snapshot-{int(time.time())}.json")
    args = ap.parse_args()

    admin_headers = {"X-Admin-Password": args.admin_password}

    print("fetching account list...", file=sys.stderr)
    accounts = _get(f"{args.admin_url}/accounts", headers=admin_headers)
    print(f"  {len(accounts)} accounts", file=sys.stderr)

    snapshot = {"captured_at": time.time(), "accounts": []}

    for rec in accounts:
        account_id, api_key = rec["account_id"], rec["api_key"]
        print(f"  dumping {account_id}...", file=sys.stderr)
        headers = {"X-API-Key": api_key}
        try:
            account = _get(f"{args.api_url}/account", headers=headers)
            orders = _get(f"{args.api_url}/orders", headers=headers)
            fills = _get(f"{args.api_url}/fills", headers=headers)
        except urllib.error.HTTPError as e:
            print(f"    WARNING: failed to fetch {account_id}: {e}", file=sys.stderr)
            continue
        snapshot["accounts"].append({
            "account_id": account_id,
            "active": rec["active"],
            "account": account,
            "orders": orders,
            "fills": fills,
        })

    print("fetching leaderboard...", file=sys.stderr)
    snapshot["leaderboard"] = _get(f"{args.api_url}/leaderboard")

    if args.website_url:
        print("fetching chat log...", file=sys.stderr)
        try:
            snapshot["chat"] = _get(f"{args.website_url}/data/chat")
        except urllib.error.HTTPError as e:
            print(f"  WARNING: failed to fetch chat: {e}", file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"wrote {args.out} ({len(snapshot['accounts'])} accounts)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
