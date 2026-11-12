"""Buffered Parquet writer.

Rows are tuples in schema order, buffered in memory and written once a minute to
    <DATA_DIR>/<table>/date=YYYY-MM-DD/hour=HH/<table>-<first recv_ts>.parquet
Each file is written under a temporary name and then renamed, so a crash never leaves
a half-written file and loses at most the last minute.
All timestamps are UTC epoch milliseconds. The first column of every table is recv_ts.
"""
import os
import time

import pyarrow as pa
import pyarrow.parquet as pq

from . import config

i64, f64, s, b = pa.int64(), pa.float64(), pa.string(), pa.bool_()


def _levels():
    cols = []
    for side in ("bid", "ask"):
        for n in range(1, config.TOP_LEVELS + 1):
            cols += [(f"{side}_px_{n}", f64), (f"{side}_sz_{n}", f64)]
    return cols


SCHEMAS = {
    # one row per book update per token; level 1 is the best bid / ask
    "books": pa.schema([("recv_ts", i64), ("src_ts", i64), ("asset_id", s), ("event_type", s),
                        ("chg_side", s), ("chg_px", f64), ("chg_sz", f64)] + _levels()),
    # trades, tick size changes and resolution events from the CLOB socket
    "clob_events": pa.schema([("recv_ts", i64), ("src_ts", i64), ("asset_id", s), ("market", s),
                              ("event_type", s), ("price", f64), ("size", f64), ("side", s), ("detail", s)]),
    "markets": pa.schema([("recv_ts", i64), ("src_ts", i64), ("event_id", s), ("market_id", s),
                          ("condition_id", s), ("slug", s), ("length", s), ("open_ts", i64), ("close_ts", i64),
                          ("token_up", s), ("token_down", s), ("tick_size", f64), ("min_order_size", f64),
                          ("rules", s), ("settlement_source", s), ("twap_lookback_s", i64), ("series_slug", s)]),
    # stream is "twap60" or "spot"; backfill rows came from a snapshot, not the live stream
    "chainlink": pa.schema([("recv_ts", i64), ("src_ts", i64), ("msg_ts", i64), ("stream", s),
                            ("value", f64), ("full_accuracy_value", s), ("backfill", b)]),
    # Binance BTC/USDT 1-second candles; src_ts is the exchange event time
    "binance": pa.schema([("recv_ts", i64), ("src_ts", i64), ("open_ts", i64), ("open", f64), ("high", f64),
                          ("low", f64), ("close", f64), ("volume", f64), ("trades", i64),
                          ("closed", b), ("backfill", b)]),
    "resolutions": pa.schema([("recv_ts", i64), ("src_ts", i64), ("slug", s), ("length", s),
                              ("condition_id", s), ("open_ts", i64), ("close_ts", i64), ("outcome", s),
                              ("price_to_beat", f64), ("final_price", f64), ("uma_status", s), ("complete", b)]),
    "health": pa.schema([("recv_ts", i64), ("src_ts", i64), ("stream", s), ("kind", s), ("value", f64),
                         ("start_ts", i64), ("end_ts", i64), ("detail", s)]),
}


def now_ms():
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir or config.DATA_DIR
        self.buffers = {name: [] for name in SCHEMAS}

    def add(self, table, row):
        self.buffers[table].append(row)

    def swap(self):
        """Take everything buffered so far and start fresh buffers."""
        full = {name: rows for name, rows in self.buffers.items() if rows}
        for name in full:
            self.buffers[name] = []
        return full

    def write(self, batches):
        """Write swapped-out buffers to disk. Safe to call from a worker thread."""
        for table, rows in batches.items():
            first_hour, last_hour = rows[0][0] // 3_600_000, rows[-1][0] // 3_600_000
            if first_hour == last_hour:
                groups = {first_hour: rows}
            else:                                   # batch straddles an hour: split it
                groups = {}
                for row in rows:
                    groups.setdefault(row[0] // 3_600_000, []).append(row)
            for hour, part in groups.items():
                self._write_file(table, hour, part)

    def _write_file(self, table, hour, rows):
        schema = SCHEMAS[table]
        columns = list(zip(*rows))
        arrays = [pa.array(col, type=field.type) for col, field in zip(columns, schema)]
        t = time.gmtime(hour * 3600)
        folder = os.path.join(self.data_dir, table, time.strftime("date=%Y-%m-%d", t), time.strftime("hour=%H", t))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{table}-{rows[0][0]}.parquet")
        pq.write_table(pa.Table.from_arrays(arrays, schema=schema), path + ".tmp", compression="zstd")
        os.replace(path + ".tmp", path)
