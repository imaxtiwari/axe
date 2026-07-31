"""Standalone entrypoint for running the retry worker in docker compose."""

import asyncio
import logging

from axe.db.session import AsyncSessionLocal
from axe.ingestion.worker import RetryWorker, default_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("axe.ingestion.cli")


async def main() -> None:
    """Run the retry worker loop in the background."""
    registry = default_registry()
    worker = RetryWorker(AsyncSessionLocal, registry=registry, poll_interval=30.0)
    worker.start()
    logger.info("Retry worker running; press Ctrl+C to stop")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
