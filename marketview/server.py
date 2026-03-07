"""
server.py  –  MarketView WebSocket Server (Upstox Edition)
==========================================================
Pipeline:
  Upstox tick  →  1-second aggregator  →  TA engine  →  JSON  →  Frontend WebSocket

Run:
    python server.py

Frontend connects to: ws://{WS_HOST}:{WS_PORT} (see .env)
and sends a plain-text NSE symbol e.g. "RELIANCE"
"""
import os
import logging
from aiohttp import web
import aiohttp_cors

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
import asyncio
import json
import signal
import sys
import time
import numpy as np
import pandas as pd
import pandas_ta as ta
import websockets
from collections import defaultdict

from data_provider import DataProvider
from token_refresh import attach_auth_routes
from portfolio import attach_portfolio_routes, check_alerts, broadcast_summary, send_market_open_summary, send_daily_pnl_summary
from cache import Cache
from fundamentals_provider import FundamentalsProvider

# ── CONFIG (env-driven — never hardcode hosts/ports) ─────────────────────────
WS_HOST           = os.environ.get("WS_HOST",   "0.0.0.0")
WS_PORT           = int(os.environ.get("WS_PORT",   "8765"))
REST_PORT         = int(os.environ.get("REST_PORT",  "8000"))
API_KEY           = os.environ.get("API_KEY",   "")          # required in prod
CANDLE_INTERVAL   = 1          # seconds per aggregated candle sent to frontend
BUFFER_MIN        = 200        # minimum historical candles for TA
MAX_RETRY         = 3          # retries on API failure
RETRY_BASE_DELAY  = 2          # seconds; doubles each retry
CLEANUP_IDLE_SEC  = 600        # evict symbol data after 10 min with no watchers

# ── RATE LIMITING ─────────────────────────────────────────────────────────────
RATE_LIMIT_PER_MIN  = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))
_rate_buckets: dict[str, list[float]] = defaultdict(list)   # ip → [timestamps]

def _check_rate_limit(ip: str) -> bool:
    """Return True if allowed, False if over limit. Sliding 60-second window."""
    now     = time.time()
    window  = _rate_buckets[ip]
    # Evict timestamps older than 60 s
    _rate_buckets[ip] = [t for t in window if now - t < 60]
    if len(_rate_buckets[ip]) >= RATE_LIMIT_PER_MIN:
        return False
    _rate_buckets[ip].append(now)
    return True

# ── SHARED STATE ─────────────────────────────────────────────────────────────
tick_buffer:    dict[str, list[float]]  = defaultdict(list)
vol_buffer:     dict[str, int]          = defaultdict(int)
rolling_avg_vol: dict = {}  # EMA of per-symbol volume for alert checks
hist_data:      dict[str, pd.DataFrame] = {}
company_names:  dict[str, str]          = {}
watchers:       dict[str, set]          = defaultdict(set)
sym_to_key:     dict[str, str]          = {}
last_tick_time: dict[str, float]        = {}   # symbol → epoch of most recent tick
last_watcher:   dict[str, float]        = {}   # symbol → epoch when it last had watchers

hist_data_lock = asyncio.Lock()          # guards hist_data mutations

cache = Cache()
dp    = DataProvider(cache=cache)
fp    = FundamentalsProvider()

# ── TICK CALLBACK ─────────────────────────────────────────────────────────────
def on_tick(symbol: str, ltp: float, volume: int, bid: float, ask: float):
    tick_buffer[symbol].append(ltp)
    vol_buffer[symbol] += volume
    last_tick_time[symbol] = time.time()

# ── EXPONENTIAL BACKOFF ───────────────────────────────────────────────────────
async def fetch_with_retry(fn, *args, **kwargs):
    """Retry with exponential backoff."""
    for attempt in range(MAX_RETRY):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"[Retry] Attempt {attempt+1} failed: {e}. Retrying in {delay}s…")
            await asyncio.sleep(delay)
