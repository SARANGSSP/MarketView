"""
cache.py  –  Two-layer cache for MarketView
============================================

Layer 1: In-memory dict  — zero-latency, process-lifetime
Layer 2: SQLite on disk  — survives restarts, shared across processes

TTL policy by data type:
  instruments   24 h   (NSE instrument list changes at most once a day)
  baseline      15 min (200-day history; fetched on cold symbol load)
  history_1w    15 min
  history_1m    15 min
  history_1y    15 min
  history_5y    60 min (weekly candles; very stable)
  intraday      30 s   (1-min candles; live ticks supersede quickly)

Usage:
  from cache import Cache
  cache = Cache()                    # call once at startup
  cache.open()                       # connect to SQLite

  # Instruments list (raw JSON bytes)
  raw = cache.get_instruments()
  if raw is None:
      raw = fetch_from_upstox()
      cache.set_instruments(raw)

  # OHLCV DataFrame
  df, name = cache.get_ohlcv("RELIANCE", "1y")
  if df is None:
      df, name = upstox.get_history("RELIANCE", "1y")
      cache.set_ohlcv("RELIANCE", "1y", df, name)
"""

import json
import logging
import os
import pickle
import sqlite3
import threading
import time
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("cache")

# ── TTL constants (seconds) ───────────────────────────────────────────────────
TTL_INSTRUMENTS  = 60 * 60 * 24   # 24 hours
TTL_BASELINE     = 60 * 15        # 15 minutes
TTL_HISTORY_1W   = 60 * 15        # 15 minutes
TTL_HISTORY_1M   = 60 * 15        # 15 minutes
TTL_HISTORY_1Y   = 60 * 15        # 15 minutes
TTL_HISTORY_5Y   = 60 * 60        # 1 hour
TTL_INTRADAY     = 30             # 30 seconds

_RANGE_TTL: dict[str, int] = {
    "baseline": TTL_BASELINE,
    "1w":       TTL_HISTORY_1W,
    "1m":       TTL_HISTORY_1M,
    "1y":       TTL_HISTORY_1Y,
    "5y":       TTL_HISTORY_5Y,
    "1d":       TTL_INTRADAY,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ttl_for_range(range_: str) -> int:
    return _RANGE_TTL.get(range_, TTL_HISTORY_1Y)


def _is_market_hours() -> bool:
    """
    Returns True if current IST time is within NSE trading hours (09:15–15:30, Mon–Fri).
    During market hours, intraday TTL stays short; outside hours we can cache longer.
    """
    import datetime
    # IST = UTC+5:30
    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    if ist_now.weekday() >= 5:   # Saturday / Sunday
        return False
    t = ist_now.time()
    return datetime.time(9, 15) <= t <= datetime.time(15, 30)


# ── Cache class ───────────────────────────────────────────────────────────────

class Cache:
    """
    Thread-safe two-layer cache.
    All public methods are synchronous (call with asyncio.to_thread in async code).
    """

    DB_PATH = os.environ.get("CACHE_DB_PATH", "marketview_cache.db")

    def __init__(self):
        self._mem: dict[str, tuple[float, object]] = {}   # key → (expires_at, value)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self):
        """Open / create the SQLite database and build schema."""
        self._conn = sqlite3.connect(
            self.DB_PATH,
            check_same_thread=False,    # we guard with self._lock
            isolation_level=None,       # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")   # better read concurrency
        self._conn.execute("PRAGMA synchronous=NORMAL") # faster writes, safe
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key        TEXT PRIMARY KEY,
                value      BLOB NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
        log.info("[Cache] SQLite opened: %s", self.DB_PATH)
        # Purge expired rows from previous runs
        self._evict_expired()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _evict_expired(self):
        """Remove rows whose TTL has passed from SQLite."""
        if not self._conn:
            return
        now = time.time()
        with self._lock:
            cur = self._conn.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
            if cur.rowcount:
                log.debug("[Cache] Evicted %d expired rows from SQLite", cur.rowcount)

    def _mem_get(self, key: str) -> Optional[object]:
        with self._lock:
            entry = self._mem.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._mem[key]
                return None
            return value

    def _mem_set(self, key: str, value: object, ttl: int):
        with self._lock:
            self._mem[key] = (time.time() + ttl, value)

    def _db_get(self, key: str) -> Optional[object]:
        if not self._conn:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value_blob, expires_at = row
        if time.time() > expires_at:
            with self._lock:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            return None
        try:
            return pickle.loads(value_blob)
        except Exception as e:
            log.warning("[Cache] Failed to unpickle key %s: %s", key, e)
            return None

    def _db_set(self, key: str, value: object, ttl: int):
        if not self._conn:
            return
        try:
            blob       = pickle.dumps(value, protocol=5)
            expires_at = time.time() + ttl
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, blob, expires_at),
                )
        except Exception as e:
            log.warning("[Cache] Failed to write key %s: %s", key, e)

    def _get(self, key: str) -> Optional[object]:
        """Try memory first, then SQLite. Warm memory on SQLite hit."""
        v = self._mem_get(key)
        if v is not None:
            log.debug("[Cache] MEM HIT  %s", key)
            return v

        v = self._db_get(key)
        if v is not None:
            log.debug("[Cache] DB  HIT  %s", key)
            # Warm memory — re-calculate remaining TTL from SQLite expires_at
            with self._lock:
                row = self._conn.execute(
                    "SELECT expires_at FROM cache WHERE key = ?", (key,)
                ).fetchone() if self._conn else None
            if row:
                remaining = max(0, row[0] - time.time())
                self._mem_set(key, v, int(remaining))
            return v

        log.debug("[Cache] MISS     %s", key)
        return None

    def _set(self, key: str, value: object, ttl: int):
        self._mem_set(key, value, ttl)
        self._db_set(key, value, ttl)
        log.debug("[Cache] SET      %s  TTL=%ds", key, ttl)

    # ── Public API ────────────────────────────────────────────────────────────

    # ── Instruments ──

    def get_instruments(self) -> Optional[bytes]:
        """Return raw gzip bytes of the instruments JSON, or None if not cached."""
        return self._get("instruments:nse")

    def set_instruments(self, raw_bytes: bytes):
        self._set("instruments:nse", raw_bytes, TTL_INSTRUMENTS)

    # ── OHLCV DataFrames ──

    def get_ohlcv(self, symbol: str, range_: str) -> Optional[tuple[pd.DataFrame, str]]:
        """
        Return (DataFrame, company_name) for the given symbol + range, or None on miss.
        range_ is one of: 'baseline', '1d', '1w', '1m', '1y', '5y'
        """
        # Outside market hours, intraday cache can live longer (60 min)
        if range_ == "1d" and not _is_market_hours():
            ttl_hint = TTL_HISTORY_1Y   # after close, intraday is final for the day
        else:
            ttl_hint = _ttl_for_range(range_)   # not used for get, just for context

        key = f"ohlcv:{symbol.upper()}:{range_}"
        return self._get(key)

    def set_ohlcv(self, symbol: str, range_: str, df: pd.DataFrame, name: str):
        """Store (DataFrame, company_name) tuple."""
        key = f"ohlcv:{symbol.upper()}:{range_}"

        # Intraday TTL extends to end-of-day outside market hours
        if range_ == "1d" and not _is_market_hours():
            ttl = TTL_HISTORY_1Y          # lock in today's completed intraday
        else:
            ttl = _ttl_for_range(range_)

        self._set(key, (df, name), ttl)

    # ── Cache management ──

    def invalidate(self, symbol: str, range_: str | None = None):
        """
        Evict cache entries for a symbol.
        If range_ is given, only that range is evicted.
        If range_ is None, all ranges for the symbol are evicted.
        """
        ranges = [range_] if range_ else list(_RANGE_TTL.keys()) + ["baseline"]
        for r in ranges:
            key = f"ohlcv:{symbol.upper()}:{r}"
            with self._lock:
                self._mem.pop(key, None)
                if self._conn:
                    self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        log.info("[Cache] Invalidated %s / %s", symbol, range_ or "all")

    def invalidate_instruments(self):
        key = "instruments:nse"
        with self._lock:
            self._mem.pop(key, None)
            if self._conn:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        log.info("[Cache] Invalidated instruments list")

    def stats(self) -> dict:
        """Return cache stats for the /cache/stats health endpoint."""
        with self._lock:
            mem_count = sum(
                1 for (exp, _) in self._mem.values() if time.time() < exp
            )
            db_count = 0
            if self._conn:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM cache WHERE expires_at > ?", (time.time(),)
                ).fetchone()
                db_count = row[0] if row else 0

        return {
            "memory_entries": mem_count,
            "db_entries":     db_count,
            "db_path":        self.DB_PATH,
        }
