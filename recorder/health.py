"""Health table: message counts per minute, connects, disconnects, gaps, clock offset."""
import asyncio
import logging
import socket
import struct
import time
from collections import defaultdict

from . import config
from .store import now_ms

log = logging.getLogger("recorder")


def ntp_offset_ms(host, timeout=3):
    """Clock offset against an NTP server in ms. Positive means our clock is behind."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t0 = time.time()
        sock.sendto(packet, (host, 123))
        data, _ = sock.recvfrom(48)
        t3 = time.time()

    def to_unix(secs, frac):
        return secs - 2208988800 + frac / 2**32

    t1 = to_unix(*struct.unpack("!II", data[32:40]))    # server receive time
    t2 = to_unix(*struct.unpack("!II", data[40:48]))    # server transmit time
    return ((t1 - t0) + (t2 - t3)) / 2 * 1000


class Health:
    def __init__(self, store):
        self.store = store
        self.counts = defaultdict(int)
        self.last_msg = {}            # stream -> recv_ts of its latest data message

    def count(self, stream, recv_ts, n=1):
        self.counts[stream] += n
        self.last_msg[stream] = recv_ts

    def event(self, stream, kind, value=None, start_ts=None, end_ts=None, detail=""):
        now = now_ms()
        self.store.add("health", (now, now, stream, kind, value, start_ts, end_ts, str(detail)[:500]))
        if kind not in ("count", "flush", "loop_lag_ms"):
            log.info("health %s %s value=%s start=%s end=%s %s", stream, kind, value, start_ts, end_ts, detail)

    def gap(self, stream, start_ts, end_ts, detail=""):
        self.event(stream, "gap", (end_ts - start_ts) / 1000, start_ts, end_ts, detail)

    async def run(self):
        """Every second: detect a suspended machine. Every minute: write counts. Every 5 min: NTP."""
        last_tick = time.time()
        minute = int(last_tick // 60)
        next_ntp = 0
        worst_lag = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            worst_lag = max(worst_lag, now - last_tick - 1)      # how late this 1-second sleep woke up
            if now - last_tick > 20:          # the process was frozen: PC sleep, VM pause, clock step
                self.gap("all", int(last_tick * 1000), int(now * 1000), "process suspended or clock jumped")
            last_tick = now
            if int(now // 60) != minute:
                start = minute * 60_000
                for stream in list(self.counts):
                    self.event(stream, "count", self.counts[stream], start, start + 60_000)
                    self.counts[stream] = 0
                # a busy event loop delays every recv_ts by about this much
                self.event("recorder", "loop_lag_ms", round(worst_lag * 1000, 1), start, start + 60_000)
                worst_lag = 0.0
                minute = int(now // 60)
            if now >= next_ntp:
                next_ntp = now + config.NTP_EVERY_S
                asyncio.create_task(self._ntp())

    async def _ntp(self):
        last = None
        for host in config.NTP_HOSTS:
            try:
                offset = await asyncio.to_thread(ntp_offset_ms, host)
                self.event("clock", "ntp_offset_ms", round(offset, 2), detail=host)
                return
            except Exception as e:
                last = e
        self.event("clock", "ntp_failed", detail=repr(last))
