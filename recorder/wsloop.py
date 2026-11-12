"""One reconnecting websocket loop shared by every stream."""
import asyncio
import logging
import time

import websockets

from .store import now_ms

log = logging.getLogger("recorder")


async def run_ws(name, url, health, on_open, on_message, tick=None, text_ping=None, ping_every=10,
                 stale_s=15, any_frame_is_alive=False, stop=None, max_queue=16, detail=""):
    """Connect, call on_open(ws), then on_message(msg, recv_ts) for every frame, until stop() is true.

    on_message returns True for a real data message; those are counted in the health table under
    `name` and are what gaps are measured between. tick(ws) runs about once a second.
    A connection that stays silent for stale_s seconds is closed and reopened (silent means no
    data, or no frame of any kind if any_frame_is_alive). Every outage is logged as a gap from the
    last data message before it to the first data message after it.
    """
    backoff = 1
    gap_start = None
    last_data = None
    while not (stop and stop()):
        opened = time.time()
        conn = {"alive": now_ms()}
        try:
            async with websockets.connect(url, max_size=None, max_queue=max_queue, open_timeout=15, close_timeout=2,
                                          ping_interval=None) as ws:
                health.event(name, "connect", detail=detail)
                await on_open(ws)
                keeper = asyncio.create_task(_keeper(name, ws, health, conn, tick, text_ping, ping_every, stale_s, stop))
                try:
                    async for msg in ws:
                        recv_ts = now_ms()
                        if on_message(msg, recv_ts):
                            health.count(name, recv_ts)
                            conn["alive"] = last_data = recv_ts
                            if gap_start is not None:
                                health.gap(name, gap_start, recv_ts, detail)
                                gap_start = None
                        elif any_frame_is_alive:
                            conn["alive"] = recv_ts
                finally:
                    keeper.cancel()
            reason = "closed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = repr(e)
        if stop and stop():
            return
        if gap_start is None:
            gap_start = last_data or int(opened * 1000)
        health.event(name, "disconnect", detail=f"{detail} {reason}".strip())
        backoff = 1 if time.time() - opened > 60 else min(backoff * 2, 30)
        await asyncio.sleep(backoff)


async def _keeper(name, ws, health, conn, tick, text_ping, ping_every, stale_s, stop):
    """Heartbeat, per-second tick, the staleness watchdog and the stop check for one connection."""
    last_ping = time.time()
    try:
        while True:
            await asyncio.sleep(1)
            if stop and stop():
                await ws.close()
                return
            if text_ping and time.time() - last_ping >= ping_every:
                await ws.send(text_ping)
                last_ping = time.time()
            if tick:
                await tick(ws)
            if now_ms() - conn["alive"] > stale_s * 1000:
                health.event(name, "stale", stale_s)
                await ws.close()
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("%s keeper stopped: %r", name, e)
        await ws.close()
