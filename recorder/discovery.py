"""Market discovery and resolutions, both from the Gamma API.

5m and 15m markets have predictable slugs (btc-updown-5m-<unix_start>), so we fetch the current
window and the next two. Hourly slugs are labelled in ET, so instead of building them we list the
hourly series and read each market's open and close times, which Gamma gives in UTC.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime

import requests

from . import config
from .store import now_ms

log = logging.getLogger("recorder")


def iso_ms(text):
    """'2026-11-11T17:25:00Z' -> UTC epoch milliseconds."""
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def get_json(path, params):
    r = requests.get(config.GAMMA + path, params=params, timeout=10, headers={"User-Agent": "polyrecorder"})
    r.raise_for_status()
    return r.json()


def parse_event(ev, length):
    """Flatten one Gamma event into the fields we store. Returns None if it is not a usable market."""
    try:
        m = ev["markets"][0]
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
        market = {
            "event_id": str(ev["id"]), "market_id": str(m["id"]), "condition_id": m["conditionId"],
            "slug": ev["slug"], "length": length,
            "open_ts": iso_ms(m["eventStartTime"]), "close_ts": iso_ms(m["endDate"]),
            "token_up": tokens[outcomes.index("Up")], "token_down": tokens[outcomes.index("Down")],
            "tick_size": float(m["orderPriceMinTickSize"]), "min_order_size": float(m.get("orderMinSize") or 0),
            "rules": m.get("description", ""), "settlement_source": m.get("resolutionSource", ""),
            "twap_lookback_s": (m.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds"),
            "series_slug": ev.get("seriesSlug", ""),
            "updated_ts": iso_ms(m["updatedAt"]) if m.get("updatedAt") else None,
        }
    except (KeyError, ValueError, IndexError) as e:
        log.warning("could not parse event %s: %r", ev.get("slug"), e)
        return None
    if market["close_ts"] - market["open_ts"] != config.LENGTHS[length] * 1000:
        log.warning("unexpected window length for %s", market["slug"])
        return None
    return market


def parse_resolution(ev):
    """Outcome and official line / settlement values from a (possibly resolved) Gamma event."""
    m = ev["markets"][0]
    meta = ev.get("eventMetadata") or {}
    outcome = None
    if m.get("closed"):
        prices = json.loads(m.get("outcomePrices") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        winners = [o for o, p in zip(outcomes, prices) if float(p) == 1.0]
        outcome = winners[0] if len(winners) == 1 else None
    closed_ts = iso_ms(ev["closedTime"]) if ev.get("closedTime") else None
    return {"outcome": outcome, "price_to_beat": meta.get("priceToBeat"), "final_price": meta.get("finalPrice"),
            "closed_ts": closed_ts, "uma_status": m.get("umaResolutionStatus") or ""}


class Discovery:
    def __init__(self, store, health):
        self.store = store
        self.health = health
        self.markets = {}                 # slug -> market dict, for markets not long closed
        self.state_path = os.path.join(store.data_dir, "state", "pending.json")
        self.pending = self._load()       # slug -> market still waiting for its resolution row
        self.next_hourly = 0
        self.next_resolve = 0

    # ---- what the book recorder should be subscribed to right now
    def live_markets(self):
        now = now_ms()
        return [m for m in list(self.markets.values())
                if m["open_ts"] - config.SUBSCRIBE_LEAD_S * 1000 <= now <= m["close_ts"] + config.SUBSCRIBE_TAIL_S * 1000]

    def condition_ids(self):
        return {m["condition_id"] for m in list(self.markets.values())}

    # ---- main loop
    async def run(self):
        while True:
            try:
                await self.refresh()
                await self.resolve()
            except Exception as e:
                self.health.event("gamma", "error", detail=repr(e))
            await asyncio.sleep(20)

    async def refresh(self):
        now = time.time()
        for length in ("5m", "15m"):
            step = config.LENGTHS[length]
            current = int(now // step) * step
            for start in (current, current + step, current + 2 * step):
                slug = f"btc-updown-{length}-{start}"
                if slug not in self.markets:
                    events = await asyncio.to_thread(get_json, "/events", {"slug": slug})
                    self.health.count("gamma", now_ms())
                    if events:
                        self._add(parse_event(events[0], length))
                    else:
                        self.health.event("gamma", "market_missing", detail=slug)
        if now >= self.next_hourly:
            events = await asyncio.to_thread(
                get_json, "/events", {"series_id": config.HOURLY_SERIES_ID, "closed": "false", "limit": 100})
            self.health.count("gamma", now_ms())
            for ev in events:
                m = parse_event(ev, "1h")
                # keep the hour in progress and anything opening within two hours
                if m and m["slug"] not in self.markets and m["close_ts"] > now * 1000 and m["open_ts"] < (now + 7200) * 1000:
                    self._add(m)
            covered = any(m["length"] == "1h" and m["open_ts"] <= (now + 600) * 1000 < m["close_ts"]
                          for m in self.markets.values())
            self.next_hourly = now + (300 if covered else 30)
            if not covered:
                self.health.event("gamma", "hourly_missing")
        for slug in [s for s, m in self.markets.items() if m["close_ts"] < (now - 3600) * 1000]:
            del self.markets[slug]

    def _add(self, m):
        if m is None:
            return
        self.markets[m["slug"]] = m
        now = now_ms()
        self.store.add("markets", (
            now, m["updated_ts"], m["event_id"], m["market_id"], m["condition_id"], m["slug"], m["length"],
            m["open_ts"], m["close_ts"], m["token_up"], m["token_down"], m["tick_size"], m["min_order_size"],
            m["rules"], m["settlement_source"], m["twap_lookback_s"], m["series_slug"]))
        if m["slug"] not in self.pending:
            self.pending[m["slug"]] = {k: m[k] for k in ("length", "condition_id", "open_ts", "close_ts")}
            self._save()
        log.info("market %s opens %s", m["slug"], time.strftime("%H:%M:%S", time.gmtime(m["open_ts"] / 1000)))

    # ---- resolutions
    async def resolve(self):
        """Fetch outcome and official values for closed markets. Retries until Gamma has both numbers."""
        now = time.time()
        if now < self.next_resolve:
            return
        self.next_resolve = now + 120
        fetched = 0
        for slug, p in sorted(self.pending.items(), key=lambda kv: kv[1]["close_ts"]):
            wait_s = 600 if p["length"] == "1h" else 150
            if now * 1000 < p["close_ts"] + wait_s * 1000 or now < p.get("next_try", 0) or fetched >= 30:
                continue
            fetched += 1
            events = await asyncio.to_thread(get_json, "/events", {"slug": slug})
            self.health.count("gamma", now_ms())
            res = parse_resolution(events[0]) if events else {}
            complete = bool(res) and None not in (res["outcome"], res["price_to_beat"], res["final_price"])
            gave_up = now * 1000 > p["close_ts"] + 6 * 3600 * 1000
            if complete or gave_up:
                self.store.add("resolutions", (
                    now_ms(), res.get("closed_ts"), slug, p["length"], p["condition_id"], p["open_ts"], p["close_ts"],
                    res.get("outcome"), res.get("price_to_beat"), res.get("final_price"), res.get("uma_status", ""),
                    complete))
                del self.pending[slug]
                if not complete:
                    self.health.event("gamma", "resolution_incomplete", detail=slug)
            else:
                p["tries"] = p.get("tries", 0) + 1
                p["next_try"] = now + min(120 * 2 ** p["tries"], 1800)
        if fetched:
            self._save()

    # ---- the pending list survives restarts
    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path + ".tmp", "w") as f:
            json.dump(self.pending, f)
        os.replace(self.state_path + ".tmp", self.state_path)
