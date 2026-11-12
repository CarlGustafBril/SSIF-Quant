"""Unit tests for the recorder's pure logic. Run with:  python -m unittest discover tests"""
import glob
import json
import os
import tempfile
import unittest

import pyarrow.parquet as pq

from recorder import config
from recorder.books import Books, apply_change, top_levels
from recorder.discovery import iso_ms, parse_event, parse_resolution
from recorder.health import Health
from recorder.store import SCHEMAS, Store

N = config.TOP_LEVELS


class FakeDiscovery:
    def __init__(self, tokens):
        self.tokens = set(tokens)

    def live_markets(self):
        return []

    def condition_ids(self):
        return {"0xabc"}


class TopLevelsTest(unittest.TestCase):
    def test_best_first_and_padded(self):
        bids = {0.40: 10.0, 0.42: 5.0, 0.41: 7.0}
        asks = {0.45: 3.0, 0.44: 2.0}
        flat = top_levels(bids, asks)
        self.assertEqual(len(flat), 4 * N)
        self.assertEqual(flat[:6], [0.42, 5.0, 0.41, 7.0, 0.40, 10.0])
        self.assertEqual(flat[6:2 * N], [None] * (2 * N - 6))
        self.assertEqual(flat[2 * N:2 * N + 4], [0.44, 2.0, 0.45, 3.0])

    def test_only_top_n_kept(self):
        bids = {round(0.01 * i, 2): 1.0 for i in range(1, 20)}
        flat = top_levels(bids, {})
        self.assertEqual(flat[0], 0.19)
        self.assertEqual(flat[2 * (N - 1)], round(0.19 - 0.01 * (N - 1), 2))

    def test_apply_change(self):
        book = ({0.40: 10.0}, {0.45: 3.0})
        apply_change(book, "BUY", 0.41, 4.0)
        apply_change(book, "SELL", 0.45, 0.0)
        apply_change(book, "SELL", 0.46, 0.0)       # removing a level that is not there is fine
        self.assertEqual(book, ({0.40: 10.0, 0.41: 4.0}, {}))


class BooksTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(tempfile.mkdtemp())
        self.books = Books(self.store, Health(self.store), FakeDiscovery(["UP", "DOWN"]))
        self.books.live = {"UP", "DOWN"}

    def snapshot(self):
        self.books.on_message(json.dumps([{
            "event_type": "book", "asset_id": "UP", "market": "0xabc", "timestamp": "1000",
            "bids": [{"price": "0.40", "size": "10"}, {"price": "0.41", "size": "7"}],
            "asks": [{"price": "0.44", "size": "2"}, {"price": "0.43", "size": "3"}]}]), 1005)

    def test_snapshot_then_change(self):
        self.snapshot()
        self.books.on_message(json.dumps({
            "event_type": "price_change", "market": "0xabc", "timestamp": "2000", "price_changes": [
                {"asset_id": "UP", "price": "0.42", "size": "5", "side": "BUY", "best_bid": "0.42", "best_ask": "0.43"},
                {"asset_id": "OTHER", "price": "0.58", "size": "5", "side": "SELL"}]}), 2005)
        rows = self.store.buffers["books"]
        self.assertEqual(len(rows), 2)                      # the unknown token is ignored
        names = SCHEMAS["books"].names
        last = dict(zip(names, rows[-1]))
        self.assertEqual((last["recv_ts"], last["src_ts"], last["event_type"]), (2005, 2000, "price_change"))
        self.assertEqual((last["bid_px_1"], last["bid_sz_1"], last["bid_px_2"]), (0.42, 5.0, 0.41))
        self.assertEqual((last["ask_px_1"], last["ask_sz_1"]), (0.43, 3.0))
        self.assertIsNone(last["bid_px_4"])
        self.assertNotIn("UP", self.books.bad_since)

    def test_mismatch_is_flagged_and_cleared_by_snapshot(self):
        self.snapshot()
        self.books.on_message(json.dumps({
            "event_type": "price_change", "market": "0xabc", "timestamp": "2000", "price_changes": [
                {"asset_id": "UP", "price": "0.30", "size": "5", "side": "BUY", "best_bid": "0.39", "best_ask": "0.43"}]}), 2005)
        self.assertEqual(self.books.bad_since, {"UP": 2005})
        self.snapshot()
        self.assertEqual(self.books.bad_since, {})

    def test_other_events(self):
        self.assertFalse(self.books.on_message("PONG", 1))
        self.books.on_message(json.dumps({"event_type": "tick_size_change", "asset_id": "UP", "market": "0xabc",
                                          "old_tick_size": "0.01", "new_tick_size": "0.001", "timestamp": "3000"}), 3005)
        self.books.on_message(json.dumps({"event_type": "market_resolved", "market": "0xother",
                                          "winning_asset_id": "X", "timestamp": "3000"}), 3005)
        rows = self.store.buffers["clob_events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4:6], ("tick_size_change", 0.001))


class StoreTest(unittest.TestCase):
    def test_write_splits_by_hour_and_round_trips(self):
        folder = tempfile.mkdtemp()
        store = Store(folder)
        hour = 3_600_000
        t0 = 500_000 * hour                              # an exact hour boundary
        for recv in (t0 - 1000, t0 + 1000):
            store.add("chainlink", (recv, recv - 1100, recv - 50, "twap60", 74000.5, "74000500000000000000000", False))
        store.write(store.swap())
        self.assertEqual(store.swap(), {})
        files = sorted(glob.glob(os.path.join(folder, "chainlink", "date=*", "hour=*", "*.parquet")))
        self.assertEqual(len(files), 2)
        self.assertEqual(glob.glob(os.path.join(folder, "**", "*.tmp"), recursive=True), [])
        table = pq.read_table(files[0])
        self.assertEqual(table.schema.names, SCHEMAS["chainlink"].names)
        self.assertEqual(table.column("value")[0].as_py(), 74000.5)

    def test_every_schema_starts_with_recv_and_src(self):
        for name, schema in SCHEMAS.items():
            self.assertEqual(schema.names[:2], ["recv_ts", "src_ts"], name)


class GammaParsingTest(unittest.TestCase):
    EVENT = {
        "id": "1", "slug": "btc-updown-5m-1794417900", "seriesSlug": "btc-up-or-down-5m",
        "closedTime": "2026-11-11T17:31:21Z", "eventMetadata": {"priceToBeat": 74527.5, "finalPrice": 74397.0},
        "markets": [{
            "id": "2", "conditionId": "0xabc", "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
            "eventStartTime": "2026-11-11T17:25:00Z", "endDate": "2026-11-11T17:30:00Z",
            "orderPriceMinTickSize": 0.01, "orderMinSize": 5, "description": "rules", "closed": True,
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "cryptoMarketConfig": {"twapLookbackSeconds": 60}, "outcomePrices": '["0", "1"]',
            "updatedAt": "2026-11-11T17:25:14.455562Z"}]}

    def test_iso_ms(self):
        self.assertEqual(iso_ms("2026-11-11T17:25:00Z"), 1794417900000)

    def test_parse_event(self):
        m = parse_event(self.EVENT, "5m")
        self.assertEqual((m["open_ts"], m["close_ts"]), (1794417900000, 1794418200000))
        self.assertEqual((m["token_up"], m["token_down"], m["twap_lookback_s"]), ("111", "222", 60))

    def test_wrong_length_rejected(self):
        self.assertIsNone(parse_event(self.EVENT, "15m"))

    def test_parse_resolution(self):
        res = parse_resolution(self.EVENT)
        self.assertEqual((res["outcome"], res["price_to_beat"], res["final_price"]), ("Down", 74527.5, 74397.0))

    def test_unresolved_has_no_outcome(self):
        ev = json.loads(json.dumps(self.EVENT))
        ev["markets"][0]["closed"] = False
        ev["eventMetadata"] = None
        res = parse_resolution(ev)
        self.assertEqual((res["outcome"], res["price_to_beat"]), (None, None))


if __name__ == "__main__":
    unittest.main()
