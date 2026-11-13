"""Settings for the recorder. Read-only market data: no keys, no orders."""
import os

# Which coin's Up or Down contracts to record. Default btc, with every setting as it always was.
# POLY_ASSET=eth runs a second, separate recorder: its own data folder, its own lock, nothing shared.
ASSET = os.environ.get("POLY_ASSET", "btc").lower()
_PER_ASSET = {          # Binance pair, Chainlink symbol, Gamma id of the hourly series, lock port
    "btc": ("btcusdt", "btc/usd", 10114, 47811),
    "eth": ("ethusdt", "eth/usd", 10117, 47812),
}
BINANCE_PAIR, CHAINLINK_SYMBOL, HOURLY_SERIES_ID, LOCK_PORT = _PER_ASSET[ASSET]
_SUFFIX = "" if ASSET == "btc" else "_" + ASSET

# Where data lands. Keep it outside any synced folder (OneDrive, Dropbox).
DATA_DIR = os.environ.get("POLYDATA_DIR", (r"C:\polydata" if os.name == "nt" else "/var/lib/polyrecorder") + _SUFFIX)

GAMMA = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"          # legacy public feed (deprecated, see FINDINGS.md)
BINANCE_WS = f"wss://stream.binance.com:9443/ws/{BINANCE_PAIR}@kline_1s"
BINANCE_REST = "https://api.binance.com/api/v3/klines"

LENGTHS = {"5m": 300, "15m": 900, "1h": 3600}           # contract length in seconds

SUBSCRIBE_LEAD_S = 300      # subscribe to a market's book this long before its window opens
SUBSCRIBE_TAIL_S = 180      # keep the book this long after close (catches the resolution event)
RECORD_DOWN_TOKEN = True    # the Down book mirrors the Up book; set False to halve the books table
TOP_LEVELS = 5

FLUSH_EVERY_S = 60          # buffered rows are written to Parquet this often
NTP_HOSTS = ["time.cloudflare.com", "pool.ntp.org"]
NTP_EVERY_S = 300
# LOCK_PORT (set per coin above): localhost port used only to stop two recorders of one coin running at once
