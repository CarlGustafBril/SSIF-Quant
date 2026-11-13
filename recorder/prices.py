"""Reference prices: Chainlink BTC/USD (via Polymarket's public feed) and Binance BTC/USDT."""
import asyncio
import json
import time

import requests

from . import config
from .store import now_ms
from .wsloop import run_ws

SYMBOL = config.CHAINLINK_SYMBOL
LIVE_TOPICS = {"crypto_prices_twap_sixty": "twap60", "crypto_prices_chainlink": "spot"}
# a snapshot of the spot topic comes back labelled "crypto_prices"
SNAPSHOT_TOPICS = {"crypto_prices_twap_sixty": "twap60", "crypto_prices": "spot", "crypto_prices_chainlink": "spot"}


class Chainlink:
    """Records the 60-second TWAP stream (the 5m/15m settlement source) and the spot stream.

    Live updates only flow on an unfiltered subscription, so we take every symbol and keep BTC.
    The feed skips about 1-2% of seconds, and a reconnect loses a few more. A second connection
    therefore asks for a snapshot of the last minute every 20 seconds and fills in any second
    the live stream did not deliver (rows flagged backfill=True).
    """

    def __init__(self, store, health):
        self.store = store
        self.health = health
        self.seen = {"twap60": {}, "spot": {}}     # stream -> {src_ts: True}, recent only
        self.last_snapshot_request = 0

    async def run(self):
        await asyncio.gather(self.live("crypto_prices_twap_sixty"), self.live("crypto_prices_chainlink"),
                             self.snapshots())

    async def live(self, topic):
        stream = LIVE_TOPICS[topic]
        name = "chainlink_" + stream

        async def on_open(ws):
            await ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": topic, "type": "update"}]}))

        def on_message(msg, recv_ts):
            if not msg or msg[0] != "{":
                return False
            d = json.loads(msg)
            p = d.get("payload") or {}
            if d.get("type") != "update" or p.get("symbol") != SYMBOL:
                return False
            self.store.add("chainlink", (recv_ts, p["timestamp"], d.get("timestamp"), stream, float(p["value"]),
                                         p.get("full_accuracy_value"), False))
            self.seen[stream][p["timestamp"]] = True
            return True

        await run_ws(name, config.RTDS_WS, self.health, on_open, on_message, text_ping="PING", ping_every=5, stale_s=15)

    async def snapshots(self):
        name = "chainlink_snapshot"
        request = json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": topic, "type": "update", "filters": json.dumps({"symbol": SYMBOL})} for topic in LIVE_TOPICS]})

        async def on_open(ws):
            await ws.send(request)
            self.last_snapshot_request = time.time()

        async def tick(ws):
            if time.time() - self.last_snapshot_request >= 20:
                await ws.send(request)
                self.last_snapshot_request = time.time()

        def on_message(msg, recv_ts):
            if not msg or msg[0] != "{":
                return False
            d = json.loads(msg)
            stream = SNAPSHOT_TOPICS.get(d.get("topic"))
            p = d.get("payload") or {}
            if d.get("type") != "subscribe" or stream is None or p.get("symbol") != SYMBOL:
                return False
            seen = self.seen[stream]
            for point in p.get("data", []):
                ts = point["timestamp"]
                # leave the newest few seconds to the live stream, which may simply be a little late
                if ts in seen or ts > recv_ts - 5000:
                    continue
                self.store.add("chainlink", (recv_ts, ts, d.get("timestamp"), stream, float(point["value"]),
                                             point.get("full_accuracy_value"), True))
                seen[ts] = True
                self.health.count(f"chainlink_{stream}_backfilled", recv_ts)
            for old in [ts for ts in seen if ts < recv_ts - 600_000]:
                del seen[old]
            return True

        await run_ws(name, config.RTDS_WS, self.health, on_open, on_message, tick=tick,
                     text_ping="PING", ping_every=5, stale_s=60)


class Binance:
    """BTC/USDT 1-second candles (kline_1s). The 1h markets settle on the 1h candle's open and close,
    which are the first and last trade prices of the hour, so 1-second candles reproduce them."""

    def __init__(self, store, health):
        self.store = store
        self.health = health
        self.last_open_ts = None

    async def run(self):
        await run_ws("binance", config.BINANCE_WS, self.health, self.on_open, self.on_message, stale_s=10)

    def on_message(self, msg, recv_ts):
        d = json.loads(msg)
        k = d.get("k")
        if not k:
            return False
        self.store.add("binance", (recv_ts, d["E"], k["t"], float(k["o"]), float(k["h"]), float(k["l"]),
                                   float(k["c"]), float(k["v"]), k["n"], k["x"], False))
        if k["x"]:
            self.last_open_ts = k["t"]
        return True

    async def on_open(self, ws):
        """After a reconnect, fill the seconds we missed from Binance's REST candles."""
        if self.last_open_ts is None:
            return
        try:
            start = self.last_open_ts + 1000
            for _ in range(10):                         # up to 10,000 seconds (about 2.8 hours)
                rows = await asyncio.to_thread(self._fetch, start)
                recv_ts = now_ms()
                rows = [r for r in rows if r[6] < recv_ts]          # closed candles only
                for r in rows:
                    self.store.add("binance", (recv_ts, r[6] + 1, r[0], float(r[1]), float(r[2]), float(r[3]),
                                               float(r[4]), float(r[5]), r[8], True, True))
                if rows:
                    self.health.count("binance_backfilled", recv_ts, len(rows))
                    self.last_open_ts = rows[-1][0]
                    start = rows[-1][0] + 1000
                if len(rows) < 1000:
                    break
        except Exception as e:
            self.health.event("binance", "backfill_failed", detail=repr(e))

    @staticmethod
    def _fetch(start_ms):
        r = requests.get(config.BINANCE_REST, timeout=10,
                         params={"symbol": config.BINANCE_PAIR.upper(), "interval": "1s", "startTime": start_ms, "limit": 1000})
        r.raise_for_status()
        return r.json()
