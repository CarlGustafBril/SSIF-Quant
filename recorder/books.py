"""Order books from the CLOB market websocket.

Each market gets its own connection, opened a few minutes before its window and closed a few
minutes after. (One shared connection carried so much traffic that the exchange kept dropping
it as a "slow consumer".) We keep each token's full book in memory, from `book` snapshots plus
`price_change` deltas, and write one row per update with the top levels. Trades, tick size
changes and resolution events go to the clob_events table.
"""
import asyncio
import heapq
import json
import logging

from . import config
from .store import now_ms
from .wsloop import run_ws

log = logging.getLogger("recorder")
N = config.TOP_LEVELS
EMPTY = (None, None)


def top_levels(bids, asks):
    """Flat list: bid px/size for levels 1..N (best first), then the same for asks."""
    best_bids = heapq.nlargest(N, bids.items())
    best_asks = heapq.nsmallest(N, asks.items())
    flat = []
    for levels in (best_bids, best_asks):
        for i in range(N):
            flat.extend(levels[i] if i < len(levels) else EMPTY)
    return flat


def apply_change(book, side, price, size):
    """Apply one price_change to a (bids, asks) pair. Size 0 removes the level."""
    levels = book[0] if side == "BUY" else book[1]
    if size == 0:
        levels.pop(price, None)
    else:
        levels[price] = size


class Books:
    def __init__(self, store, health, discovery):
        self.store = store
        self.health = health
        self.discovery = discovery
        self.books = {}            # asset_id -> (bids {price: size}, asks {price: size})
        self.live = set()          # asset_ids we are recording right now
        self.bad_since = {}        # asset_id -> recv_ts since which our best bid/ask disagrees with the exchange

    async def run(self):
        """Keep one connection task per live market."""
        tasks = {}
        try:
            while True:
                for m in self.discovery.live_markets():
                    if m["slug"] not in tasks:
                        tasks[m["slug"]] = asyncio.create_task(self.run_market(m))
                for slug, task in list(tasks.items()):
                    if task.done():
                        if not task.cancelled() and task.exception():
                            log.error("book task %s failed: %r", slug, task.exception())
                            self.health.event("books", "task_crash", detail=f"{slug} {task.exception()!r}")
                        del tasks[slug]          # recreated on the next pass if the market is still live
                await asyncio.sleep(1)
        finally:
            for task in tasks.values():
                task.cancel()

    async def run_market(self, m):
        tokens = [m["token_up"]] + ([m["token_down"]] if config.RECORD_DOWN_TOKEN else [])
        end_ms = m["close_ts"] + config.SUBSCRIBE_TAIL_S * 1000

        async def on_open(ws):
            self._forget(tokens)                 # a new connection starts from a fresh snapshot
            self.live.update(tokens)
            await ws.send(json.dumps({"assets_ids": tokens, "type": "market", "custom_feature_enabled": True}))

        async def tick(ws):
            # Brief disagreements with the exchange's best bid/ask are normal while a trade is being
            # applied. One that lasts 3 seconds is a missed update: reconnect for a fresh snapshot.
            now = now_ms()
            if any(now - self.bad_since[a] > 3000 for a in tokens if a in self.bad_since):
                self.health.event("books_" + m["length"], "resync", detail=m["slug"])
                self._forget(tokens)
                await ws.close()

        try:
            await run_ws("books_" + m["length"], config.CLOB_WS, self.health, on_open, self.on_message, tick=tick,
                         text_ping="PING", ping_every=5, stale_s=15, any_frame_is_alive=True,
                         stop=lambda: now_ms() > end_ms, max_queue=None, detail=m["slug"])
        finally:
            self._forget(tokens)
            self.live.difference_update(tokens)

    def _forget(self, tokens):
        for asset in tokens:
            self.books.pop(asset, None)
            self.bad_since.pop(asset, None)

    def on_message(self, msg, recv_ts):
        """Handle one frame. Returns True if it carried data for a token we record."""
        if not msg or msg[0] not in "[{":       # "PONG" and empty keep-alives
            return False
        data = json.loads(msg)
        got_data = False
        for ev in data if isinstance(data, list) else [data]:
            kind = ev.get("event_type")
            if kind == "price_change":
                got_data |= self.on_price_change(ev, recv_ts)
            elif kind == "book":
                got_data |= self.on_book(ev, recv_ts)
            elif kind == "last_trade_price":
                if ev["asset_id"] in self.live:
                    self.store.add("clob_events", (recv_ts, int(ev["timestamp"]), ev["asset_id"], ev["market"], kind,
                                                   float(ev["price"]), float(ev["size"]), ev.get("side"), ""))
                    got_data = True
            elif kind == "tick_size_change":
                if ev["asset_id"] in self.live:
                    self.store.add("clob_events", (recv_ts, int(ev["timestamp"]), ev["asset_id"], ev["market"], kind,
                                                   float(ev["new_tick_size"]), None, None, ev.get("old_tick_size", "")))
                    got_data = True
            elif kind == "market_resolved":      # sent for every Polymarket market; keep only ours
                if ev.get("market") in self.discovery.condition_ids():
                    self.store.add("clob_events", (recv_ts, int(ev["timestamp"]), ev.get("winning_asset_id"),
                                                   ev["market"], kind, None, None, None, ev.get("winning_outcome", "")))
                    got_data = True
            # anything else (best_bid_ask, which we compute ourselves, and new_market) is ignored
        return got_data

    def on_book(self, ev, recv_ts):
        asset = ev["asset_id"]
        if asset not in self.live:
            return False
        bids = {float(level["price"]): float(level["size"]) for level in ev["bids"]}
        asks = {float(level["price"]): float(level["size"]) for level in ev["asks"]}
        self.books[asset] = (bids, asks)
        self.bad_since.pop(asset, None)
        self.store.add("books", (recv_ts, int(ev["timestamp"]), asset, "book", None, None, None, *top_levels(bids, asks)))
        self.health.count("books_rows", recv_ts)
        return True

    def on_price_change(self, ev, recv_ts):
        src_ts = int(ev["timestamp"])
        got_data = False
        for ch in ev["price_changes"]:
            asset = ch["asset_id"]
            book = self.books.get(asset)
            if book is None:                     # a token we do not record, or no snapshot yet
                continue
            price, size, side = float(ch["price"]), float(ch["size"]), ch["side"]
            apply_change(book, side, price, size)
            levels = top_levels(*book)
            self.store.add("books", (recv_ts, src_ts, asset, "price_change", side, price, size, *levels))
            self.health.count("books_rows", recv_ts)
            got_data = True
            # The exchange states its best bid/ask with every change; compare with ours.
            if self._matches(levels[0], ch.get("best_bid")) and self._matches(levels[2 * N], ch.get("best_ask")):
                self.bad_since.pop(asset, None)
            else:
                self.health.count("books_mismatch", recv_ts)
                self.bad_since.setdefault(asset, recv_ts)
        return got_data

    @staticmethod
    def _matches(ours, theirs):
        try:
            theirs = float(theirs)
        except (TypeError, ValueError):
            return True                          # field absent: nothing to compare
        if ours is None:
            return theirs in (0.0, 1.0)          # an empty side is reported as 0 (bid) or 1 (ask)
        return abs(ours - theirs) < 1e-9
