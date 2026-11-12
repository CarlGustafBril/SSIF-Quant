# Recorder

Records public market data for Polymarket BTC "Up or Down" 5m, 15m and 1h contracts, plus the
reference prices they settle on. Read-only: no keys, no orders, nothing shared with `trader.py`.

## What it records

All data lands in `C:\polydata` (override with the `POLYDATA_DIR` environment variable), as
Parquet files written once a minute:

    <POLYDATA_DIR>/<table>/date=YYYY-MM-DD/hour=HH/<table>-<first recv_ts>.parquet

| Table | Source | Content |
|---|---|---|
| `books` | CLOB websocket | One row per book update per token: the change, plus top 5 bid and ask levels with sizes (level 1 = best) |
| `clob_events` | CLOB websocket | Trades (`last_trade_price`), `tick_size_change`, `market_resolved` |
| `markets` | Gamma API | One row per market: ids, Up/Down token ids, slug, length, open/close, tick size, rules text, settlement source |
| `chainlink` | Polymarket live-data socket | BTC/USD 60s TWAP (`stream = twap60`) and spot (`stream = spot`), one value per second |
| `binance` | Binance websocket | BTC/USDT 1-second candles |
| `resolutions` | Gamma API | Per resolved market: outcome, official line (`price_to_beat`) and settlement value (`final_price`) |
| `health` | internal | Per-minute message counts, connects, disconnects, gaps, clock offset, flush times |

Every row has `recv_ts` (our clock when it arrived) and `src_ts` (the source's own timestamp), both
UTC epoch milliseconds. Window open and close times come from Gamma in UTC, so the ET labels on
hourly markets never need converting.

Things worth knowing when reading the data:

- **The 5m and 15m line** is the `twap60` value whose `src_ts` equals the window open exactly; the
  settlement value is the one stamped at the close. See `FINDINGS.md`.
- **Backfill rows.** The Chainlink feed skips a second now and then. A second connection re-reads
  the last minute every 20 s and fills what the live stream missed; those rows have
  `backfill = True` and a `recv_ts` up to a minute after their `src_ts`. Binance seconds missed
  during a reconnect are filled from Binance's REST candles, also flagged `backfill`.
- **Duplicates.** A market appears in `markets` again after each recorder restart. De-duplicate on `slug`.
- **Crossed rows.** While a trade is applied the exchange sends several level changes in a burst,
  so a few `books` rows show bid >= ask for a few milliseconds. They are real intermediate states.
- **Book connections.** Each market has its own websocket connection, from 5 minutes before its
  window opens until 3 minutes after it closes. A single shared connection was dropped by the
  exchange every few minutes as a "slow consumer".
- **Up and Down.** Both tokens are recorded. The Down book mirrors the Up book, so
  `RECORD_DOWN_TOKEN = False` in `config.py` halves the `books` table without losing information.

## Run it

    python -m venv C:\polydata\venv
    C:\polydata\venv\Scripts\pip install -r requirements.txt
    C:\polydata\venv\Scripts\python -m recorder          # from the repository root; Ctrl+C to stop

Keep the virtualenv and the data outside OneDrive. Only one recorder can run at a time; a second
copy exits immediately.

### As a Windows scheduled task (current setup)

    .\recorder\windows\install_task.ps1

This registers the task `PolyRecorder`. It starts at logon and runs with no window at all, so
there is nothing on screen to close by accident. The recorder is restarted 10 seconds after any
exit, and the task itself is re-checked every 5 minutes and started again if it is not running.
Useful commands:

    Get-ScheduledTask PolyRecorder                    # State should be Running
    Stop-ScheduledTask PolyRecorder                   # stop; it comes back within 5 minutes unless disabled
    Disable-ScheduledTask PolyRecorder                # keep it stopped (Enable-ScheduledTask to undo)
    Start-ScheduledTask PolyRecorder
    Unregister-ScheduledTask PolyRecorder -Confirm:$false

Limits of running on a PC:

- Sleep and hibernate on AC power were switched off (`powercfg /change standby-timeout-ac 0`).
- After a reboot (Windows Update, power cut) recording resumes only once the user logs in.
- Clock sync is whatever Windows provides; the recorder measures the offset against NTP every
  5 minutes and stores it in `health` (`kind = ntp_offset_ms`, positive = our clock is behind),
  so `recv_ts` can be corrected afterwards.

### Moving to a Linux VM (systemd + chrony)

Use a region that Polymarket and Binance both serve (not the US), 2 vCPU / 4 GB / 80 GB disk.

    sudo apt update && sudo apt install -y python3-venv chrony
    sudo systemctl enable --now chrony && chronyc tracking       # offset should be well under 10 ms
    sudo useradd --system --home /opt/polyrecorder --shell /usr/sbin/nologin polyrecorder
    sudo mkdir -p /opt/polyrecorder /var/lib/polyrecorder
    sudo cp -r recorder requirements.txt /opt/polyrecorder/
    sudo python3 -m venv /opt/polyrecorder/venv
    sudo /opt/polyrecorder/venv/bin/pip install -r /opt/polyrecorder/requirements.txt
    sudo chown -R polyrecorder:polyrecorder /opt/polyrecorder /var/lib/polyrecorder
    sudo cp recorder/deploy/polyrecorder.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now polyrecorder
    journalctl -u polyrecorder -f

The recorder runs as its own user with its own virtualenv and data folder, so it cannot touch
anything else on the machine.

## Check it is healthy

1. **Is it running?** `Get-ScheduledTask PolyRecorder` shows `Running`, and
   `Get-Content C:\polydata\logs\recorder.log -Tail 20` shows recent lines. Normal traffic is a
   `market ... opens` line every five minutes and an `ntp_offset_ms` line every five minutes.
   `disconnect`, `stale` and `gap` lines mark trouble.
2. **Are files arriving?** Each table's current `hour=HH` folder gains a file every minute
   (`books`, `chainlink`, `binance`, `health`).
3. **Full report:**

       C:\polydata\venv\Scripts\python -m recorder.acceptance 2026-11-11T20:23 2026-11-11T23:23

   prints message rates, every gap, file sizes with a 14-day projection, our line against the
   official one for each 5m/15m window, and the three legs of the latest hourly expiries on a
   1-second grid. Without arguments it covers everything recorded (slow once there are days of data).
4. **Every morning:** `python daily_check.py` for the previous UTC day.

Expected rates: `books_rows` roughly 1,000 to 3,000 per second (`books_5m`, `books_15m` and
`books_1h` count websocket messages per contract length), `chainlink_twap60` and `chainlink_spot`
about 59 per minute each, `binance` 60 per minute. `books_mismatch` is a small count of brief
disagreements during trades; a `resync` event means one lasted over 3 seconds and the recorder
reconnected for a fresh snapshot. `loop_lag_ms` is the worst delay of the recorder's own event
loop in each minute; `recv_ts` values can be late by about that much.

The Chainlink streams go silent for 15 to 20 seconds a few times an hour. The recorder reconnects
(`stale`, then `gap`) and the backfill connection fills those seconds, so a logged Chainlink gap
does not by itself mean lost data. The daily check counts seconds that are truly missing.

## Known risks

- The public Chainlink feed (`ws-live-data.polymarket.com`) is deprecated and past its announced
  removal date. If it is switched off, `chainlink_*` counts drop to zero and `stale` events repeat.
  The replacement needs Polymarket API credentials (see `FINDINGS.md`).
- Disk: about 64 GB for 14 days at current activity, nearly all of it `books`.

## Files

| File | Purpose |
|---|---|
| `main.py` | Starts everything; restarts any task that dies; flushes Parquet every minute |
| `discovery.py` | Finds markets before they open (Gamma), pulls resolutions |
| `books.py` | CLOB websocket, one connection per market: maintains each book, writes top-5 rows |
| `prices.py` | Chainlink TWAP/spot and Binance candles, with backfill |
| `wsloop.py` | Reconnect-with-backoff loop, heartbeat, stale detection, gap logging |
| `health.py` | Counts, gaps, NTP offset, sleep detection |
| `store.py` | Table schemas and the Parquet writer |
| `reader.py`, `acceptance.py` | Reading data back; the acceptance report |
