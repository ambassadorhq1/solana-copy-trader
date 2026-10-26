import asyncio, logging, os, time, statistics
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger("copytrader")
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}  # USDT
# Never scouted as "winners": SOL / wSOL, SOL liquid-staking tokens and large-cap stables (they top every mcap ranking).
EXCLUDED_MINTS = {WSOL, "11111111111111111111111111111111"} | STABLES | {
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",   # USDS
}
EXCLUDED_SYMBOLS = {"SOL", "WSOL", "MSOL", "JITOSOL", "BSOL", "JUPSOL", "INF", "USDC", "USDT", "PYUSD", "USDS", "USDE", "DAI",
                    "FDUSD", "TUSD", "USDH", "UXD", "EURC", "USD1"}


def is_excluded(mint, symbol=None):
    """SOL-like / stablecoin tokens: excluded from every discovery path (scout, winners, deep)."""
    sym = (symbol or "").strip().upper().lstrip("$")
    return mint in EXCLUDED_MINTS or sym in EXCLUDED_SYMBOLS


def load_config(path=None):
    path = path or os.environ.get("CONFIG_PATH") or Path(__file__).resolve().parent.parent / "config.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def now():
    return int(time.time())


def tz(cfg):
    return ZoneInfo(cfg.get("timezone", "UTC"))


def local_day(cfg, ts=None):
    return datetime.fromtimestamp(ts or now(), tz(cfg)).strftime("%Y-%m-%d")


def day_start_ts(cfg, ts=None):
    d = datetime.fromtimestamp(ts or now(), tz(cfg)).date()
    return int(datetime.combine(d, dtime(0), tz(cfg)).timestamp())


def local_hour(cfg):
    return datetime.now(tz(cfg)).hour


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def short(a, n=4):
    return f"{a[:n]}…{a[-n:]}" if a and len(a) > 2 * n + 1 else (a or "")


class RateLimiter:
    """Evenly spaced requests: at most `rps` per second."""

    def __init__(self, rps):
        self.interval = 1.0 / rps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            t = time.monotonic()
            if self._next > t:
                await asyncio.sleep(self._next - t)
                t = self._next
            self._next = t + self.interval

    def push_back(self, seconds):
        self._next = max(self._next, time.monotonic() + seconds)
