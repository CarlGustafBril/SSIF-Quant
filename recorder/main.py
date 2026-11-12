"""Polymarket BTC up/down recorder. Run with:  python -m recorder

Read-only: it subscribes to public market data and writes Parquet. It never places orders.
"""
import asyncio
import logging
import logging.handlers
import os
import socket
import sys
import time

from . import config
from .books import Books
from .discovery import Discovery
from .health import Health
from .prices import Binance, Chainlink
from .store import Store

log = logging.getLogger("recorder")


def setup_logging():
    folder = os.path.join(config.DATA_DIR, "logs")
    os.makedirs(folder, exist_ok=True)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    fmt.converter = time.gmtime
    handlers = [logging.handlers.RotatingFileHandler(os.path.join(folder, "recorder.log"),
                                                     maxBytes=10_000_000, backupCount=5),
                logging.StreamHandler()]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


async def forever(name, run, health):
    """Restart a task if it ever dies from an unexpected error."""
    while True:
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task %s crashed", name)
            health.event(name, "task_crash", detail=repr(e))
            await asyncio.sleep(5)


async def flusher(store, health):
    """Write buffered rows to Parquet on every minute boundary, off the event loop."""
    while True:
        await asyncio.sleep(config.FLUSH_EVERY_S - time.time() % config.FLUSH_EVERY_S + 0.05)
        batches = store.swap()
        started = time.time()
        try:
            await asyncio.to_thread(store.write, batches)
            rows = sum(len(r) for r in batches.values())
            health.event("store", "flush", round(time.time() - started, 3), detail=f"{rows} rows")
        except Exception as e:
            log.exception("write failed")
            health.event("store", "write_failed", detail=repr(e))


async def main():
    store = Store()
    health = Health(store)
    discovery = Discovery(store, health)
    health.event("recorder", "start", detail=f"pid {os.getpid()} data {config.DATA_DIR}")
    try:
        await discovery.refresh()          # know the live markets before opening the book socket
    except Exception as e:
        health.event("gamma", "error", detail=repr(e))
    books, chainlink, binance = Books(store, health, discovery), Chainlink(store, health), Binance(store, health)
    try:
        await asyncio.gather(
            forever("health", health.run, health),
            forever("discovery", discovery.run, health),
            forever("books", books.run, health),
            forever("chainlink", chainlink.run, health),
            forever("binance", binance.run, health),
            forever("flusher", lambda: flusher(store, health), health),
        )
    finally:
        health.event("recorder", "stop")
        store.write(store.swap())          # save whatever is still buffered


def run():
    setup_logging()
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", config.LOCK_PORT))
    except OSError:
        log.error("another recorder is already running (port %s is taken); exiting", config.LOCK_PORT)
        sys.exit(3)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    run()
