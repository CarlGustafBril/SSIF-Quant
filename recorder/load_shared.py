"""Load the team's shared dataset (pulled by scripts/pull_shared.py) into pandas.

The share is mirrored read-only in <POLYDATA_DIR>/shared; its data is under shared/data.
Analysis reads from there and writes derived files elsewhere (see README, "Shared dataset").

    from recorder import load_shared
    load_shared.datasets()                 # {name: [files]}
    df = load_shared.load("events")        # a file without its extension, or a folder of files
    df = load_shared.load("orderbook_rest", start="2026-07-02", end="2026-07-03", columns=[...])
    load_shared.events_index()             # one row per extracted expiry in data/events
    df = load_shared.events("2026-07-11 18:30")   # the tick extract for that expiry

Every timestamp column comes back as datetime64[UTC]: columns that are already datetimes, and
number or text columns whose name says they hold a time (ts, time, timestamp, date, *_at, *_ms).
Numbers are read as seconds, milliseconds, microseconds or nanoseconds since 1970 by their size.
"""
import os
import re

import pandas as pd
import pyarrow.parquet as pq

from . import config

SHARED_DIR = os.path.join(config.DATA_DIR, "shared")
DATA_ROOT = os.path.join(SHARED_DIR, "data")
EXTENSIONS = (".parquet", ".csv", ".csv.gz")
EVENT_FILE = re.compile(r"^(\d{8}_\d{6})\.parquet$")
MAX_LOAD_BYTES = 500_000_000       # on disk; the 723 MB trade tape and all of events need columns= or start=/end=
_TIME_NAME = re.compile(r"(?i:(^|_)(ts|time|timestamp|datetime|date|dt|expiry)($|_)|_(at|ms|ns|us)$)|[a-z]At$|^T(_iso)?$")


def _strip_ext(path):
    for ext in sorted(EXTENSIONS, key=len, reverse=True):
        if path.lower().endswith(ext):
            return path[: -len(ext)]
    return path


def datasets(root=None):
    """{name: sorted files}. A name is a file under shared/data without its extension, or any
    folder there that holds data files (all files below it, read as one table, except a file
    called index). Names use '/'."""
    root = root or DATA_ROOT
    out, folders = {}, {}
    for folder, _, names in os.walk(root):
        for n in sorted(names):
            if not n.lower().endswith(EXTENSIONS):
                continue
            path = os.path.join(folder, n)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            out[_strip_ext(rel)] = [path]
            if _strip_ext(n) == "index":        # a folder's index describes its files; not part of the table
                continue
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                folders.setdefault("/".join(parts[:i]), []).append(path)
    for name, paths in folders.items():
        # a folder is one table only if its files share their columns (data/historical holds
        # unrelated tables), and only if it adds something over its single file
        if name not in out and len(paths) > 1 and len({_columns(p) for p in paths}) == 1:
            out[name] = paths
    return {k: sorted(v) for k, v in sorted(out.items())}


def _columns(path):
    if path.lower().endswith(".parquet"):
        return tuple(pq.ParquetFile(path).schema_arrow.names)
    return tuple(pd.read_csv(path, nrows=0).columns)


def time_columns(names):
    return [n for n in names if _TIME_NAME.search(str(n))]


def to_utc(series):
    """A column as datetime64[UTC], or None if it does not hold times."""
    s = series
    if isinstance(s.dtype, pd.DatetimeTZDtype):
        return s.dt.tz_convert("UTC")
    if pd.api.types.is_datetime64_dtype(s):
        return s.dt.tz_localize("UTC")
    if pd.api.types.is_bool_dtype(s):
        return None
    if pd.api.types.is_numeric_dtype(s):
        top = s.abs().max()
        if pd.isna(top) or top < 1e8:          # empty, or too small to be a time since 1970
            return None
        unit = "s" if top < 1e11 else "ms" if top < 1e14 else "us" if top < 1e17 else "ns"
        return pd.to_datetime(s, unit=unit, utc=True)
    for fmt in (None, "mixed"):                # one format for the column, else row by row
        try:
            return pd.to_datetime(s, utc=True, format=fmt)
        except (ValueError, TypeError):
            pass
    return None


def parse_times(df):
    """Convert every timestamp column of df to UTC, in place. Returns the names converted."""
    done = []
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_datetime64_any_dtype(s) or _TIME_NAME.search(str(col)):
            parsed = to_utc(s)
            if parsed is not None:
                df[col] = parsed
                done.append(col)
    return done


def read_file(path, columns=None):
    """One shared file as a DataFrame with timestamps in UTC."""
    if path.lower().endswith(".parquet"):
        df = pq.read_table(path, columns=columns).to_pandas()
    else:
        df = pd.read_csv(path, usecols=columns)
    parse_times(df)
    return df


def _utc(t):
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def event_time(path):
    """The expiry an events/YYYYMMDD_HHMMSS.parquet file is named after (UTC), or None."""
    m = EVENT_FILE.match(os.path.basename(path))
    return pd.to_datetime(m.group(1), format="%Y%m%d_%H%M%S", utc=True) if m else None


def load(name, columns=None, start=None, end=None, time_col=None, root=None, max_bytes=MAX_LOAD_BYTES):
    """A shared dataset by name (see datasets()).

    start, end: keep rows with start <= time < end; anything pandas can read as a time, taken as
    UTC. time_col is the column they apply to; by default the first timestamp column.
    For "events" the files are first narrowed to expiries in [start, end - 15 min + 5 s] by name,
    so a day of events reads only that day's files. Reading every column of more than max_bytes
    of files on disk raises MemoryError; pass columns=, narrow with start/end, or max_bytes=None.
    """
    found = datasets(root)
    if name not in found:
        listed = [n for n in found if not EVENT_FILE.match(n.split("/")[-1] + ".parquet")]
        raise KeyError(f"no shared dataset {name!r}; have: {', '.join(listed) or 'nothing (run scripts/pull_shared.py)'}"
                       + (", events/<YYYYMMDD_HHMMSS>" if len(listed) < len(found) else ""))
    paths = found[name]
    if name == "events" and (start is not None or end is not None):
        # a file named T holds rows from T-15min to T+5s
        lo = _utc(start) if start is not None else None
        hi = _utc(end) if end is not None else None
        paths = [p for p in paths if (lo is None or event_time(p) + pd.Timedelta(seconds=5) >= lo)
                 and (hi is None or event_time(p) - pd.Timedelta(minutes=15) < hi)]
    size = sum(os.path.getsize(p) for p in paths)
    if max_bytes is not None and columns is None and size > max_bytes:
        raise MemoryError(f"{name}: {len(paths)} files, {size / 1e9:.2f} GB on disk (several times that in "
                          f"memory); narrow it with start=/end=/columns=, or pass max_bytes=None")
    if not paths:
        return pd.DataFrame(columns=columns)
    df = pd.concat([read_file(p, columns) for p in paths], ignore_index=True)
    if start is not None or end is not None:
        col = time_col or next((c for c in df.columns if isinstance(df[c].dtype, pd.DatetimeTZDtype)), None)
        if col is None:
            raise ValueError(f"{name}: no timestamp column to filter on")
        keep = pd.Series(True, index=df.index)
        if start is not None:
            keep &= df[col] >= _utc(start)
        if end is not None:
            keep &= df[col] < _utc(end)
        df = df[keep].reset_index(drop=True)
    return df


def events_index(root=None):
    """data/events/index.parquet: one row per extracted expiry (T), with strikes, outcomes and row counts."""
    return load("events/index", root=root)


def events(expiry, columns=None, root=None):
    """The tick extract for one expiry: data/events/YYYYMMDD_HHMMSS.parquet, named by expiry time in UTC.

    expiry: anything pandas reads as a time (taken as UTC), for example "2026-07-11 18:30".
    """
    t = pd.Timestamp(expiry)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return load("events/" + t.strftime("%Y%m%d_%H%M%S"), columns=columns, root=root)


def describe_parquet(path):
    """Columns, row count and first/last timestamp of one Parquet file (reads only the time columns)."""
    meta = pq.ParquetFile(path)
    names = meta.schema_arrow.names
    types = [str(t) for t in meta.schema_arrow.types]
    candidates = [n for n, t in zip(names, types) if t.startswith(("timestamp", "date")) or _TIME_NAME.search(n)]
    first = last = col = None
    if candidates:
        df = pd.read_parquet(path, columns=candidates)
        for c in candidates:
            parsed = to_utc(df[c])
            if parsed is not None and parsed.notna().any():
                first, last, col = parsed.min(), parsed.max(), c
                break
    return {"columns": list(zip(names, types)), "rows": meta.metadata.num_rows,
            "time_col": col, "first": first, "last": last}
