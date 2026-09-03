#!/usr/bin/env python3
"""Local entrypoint: `python run.py` starts the public API, admin API,
website, Kraken feed client, and bot loop all in one process.

Config (ports, passwords, etc.) comes from config/config.yaml — see that
file to change bind addresses before deploying off localhost.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from exchange.app import run  # noqa: E402

if __name__ == "__main__":
    run()
