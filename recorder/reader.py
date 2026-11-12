"""Read recorded Parquet back into pandas. Used by the acceptance report and the daily check."""
import glob
import os
import warnings

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import config
from .store import SCHEMAS


def hour_folders(table, start_ms, end_ms, data_dir=None):
    """Partition folders of a table that can hold rows received in [start_ms, end_ms)."""
    root = os.path.join(data_dir or config.DATA_DIR, table)
    folders = []
    for hour in range(start_ms // 3_600_000, (end_ms - 1) // 3_600_000 + 1):
        t = pd.Timestamp(hour * 3_600_000, unit="ms")
        folder = os.path.join(root, t.strftime("date=%Y-%m-%d"), t.strftime("hour=%H"))
        if os.path.isdir(folder):
            folders.append(folder)
    return folders


def files(table, start_ms, end_ms, data_dir=None):
    out = []
    for folder in hour_folders(table, start_ms, end_ms, data_dir):
        out += sorted(glob.glob(os.path.join(folder, "*.parquet")))
    return out


_WARNED = set()        # unreadable files already reported in this run


def load(table, start_ms, end_ms, columns=None, filters=None, data_dir=None):
    """Rows of a table with recv_ts in [start_ms, end_ms). filters use pyarrow's list-of-tuples form."""
    paths = files(table, start_ms, end_ms, data_dir)
    row_filter = [("recv_ts", ">=", start_ms), ("recv_ts", "<", end_ms)] + (filters or [])
    tables = []
    for p in paths:
        try:
            tables.append(pq.read_table(p, columns=columns, filters=row_filter))
        except pa.ArrowInvalid as e:  # a file cut short by a crash or power loss
            if p in _WARNED:
                continue
            _WARNED.add(p)
            warnings.warn(f"skipping unreadable {p}: {str(e).splitlines()[0][:80]}")
    if not tables:
        names = columns or SCHEMAS[table].names
        return pd.DataFrame({n: pd.Series(dtype=SCHEMAS[table].field(n).type.to_pandas_dtype()) for n in names})
    return pa.concat_tables(tables).to_pandas()


def size_bytes(table, start_ms, end_ms, data_dir=None):
    return sum(os.path.getsize(p) for p in files(table, start_ms, end_ms, data_dir))


def fmt_ts(ms):
    return pd.Timestamp(int(ms), unit="ms").strftime("%Y-%m-%d %H:%M:%S")


def quotes_on_grid(books, grid_ms):
    """As-of join: for each grid time, the last best bid/ask with src_ts at or before it (no look-ahead).

    books: rows for ONE token with src_ts, bid_px_1, ask_px_1 (+ sizes if present).
    Returns one row per grid time with the quote, its mid, and its age in ms (staleness).
    """
    books = books.sort_values("src_ts", kind="stable")
    grid = pd.DataFrame({"ts": pd.Series(grid_ms, dtype="int64")})
    out = pd.merge_asof(grid, books, left_on="ts", right_on="src_ts", direction="backward")
    out["mid"] = (out["bid_px_1"] + out["ask_px_1"]) / 2
    out["age_ms"] = out["ts"] - out["src_ts"]
    return out


def chainlink_at(chainlink, stream, ts_ms):
    """The value of a Chainlink stream stamped exactly ts_ms, or None. Live rows win over backfill."""
    rows = chainlink[(chainlink["stream"] == stream) & (chainlink["src_ts"] == ts_ms)]
    if rows.empty:
        return None
    return float(rows.sort_values("backfill").iloc[0]["value"])
