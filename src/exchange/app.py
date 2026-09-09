"""Entrypoint: builds shared AppState, then runs the public API, admin API,
and website concurrently (as three uvicorn servers in one process, sharing
one in-memory engine/feed/bots — see state.py) plus the synthetic price
feed, the random event scheduler, and the bot tick loop as background
tasks.

All host/ports come from config/config.yaml (or its env var overrides) —
see that file's `network` section to point this at a real IP later.
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from .api_admin import create_admin_app
from .api_public import create_public_app
from .config import load_config
from .options import OptionsScheduler
from .persistence import PersistenceLog, ensure_schema, make_engine, replay_into
from .state import AppState
from .synthetic_feed import RandomEventScheduler, SyntheticFeedClient
from .website import create_website_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("exchange.app")


async def main() -> None:
    config = load_config()

    # Durability is a hard requirement now, not a nice-to-have — fail
    # fast and loud if Postgres isn't reachable rather than quietly
    # falling back to in-memory-only and losing everything on the next
    # restart without anyone noticing until it's too late.
    db_engine = make_engine(config.database_url)
    try:
        await ensure_schema(db_engine)
    except Exception as e:
        logger.error("persistence: could not reach/initialize the database at startup: %s", e)
        raise
    persistence_log = PersistenceLog(db_engine)

    state = AppState(config, persistence_log)
    await replay_into(state, db_engine)

    public_app = create_public_app(state)
    admin_app = create_admin_app(state)
    website_app = create_website_app(state)

    servers = [
        uvicorn.Server(
            uvicorn.Config(public_app, host=config.network.api.host, port=config.network.api.port, log_level="info")
        ),
        uvicorn.Server(
            uvicorn.Config(admin_app, host=config.network.admin_api.host, port=config.network.admin_api.port, log_level="info")
        ),
        uvicorn.Server(
            uvicorn.Config(website_app, host=config.network.website.host, port=config.network.website.port, log_level="info")
        ),
    ]

    feed_client = SyntheticFeedClient(config, state.index_service, state.synthetic_params)
    random_events = RandomEventScheduler(state.index_service, state.bot_manager, config)
    options_scheduler = OptionsScheduler(state.options_manager, state)

    async def sample_price_history(interval_seconds: float = 1.0) -> None:
        while True:
            state.record_price_tick()
            state.options_manager.price_tick()
            await asyncio.sleep(interval_seconds)

    logger.info("starting: api=%s admin_api=%s website=%s",
                config.network.api_base_url, config.network.admin_api_base_url, config.network.website_base_url)

    await asyncio.gather(
        *(s.serve() for s in servers),
        feed_client.run(),
        random_events.run(),
        options_scheduler.run(),
        state.bot_manager.run_forever(),
        sample_price_history(),
        persistence_log.drain_forever(),
    )


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
