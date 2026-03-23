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

# ── CANDLESTICK PATTERN DETECTION ────────────────────────────────────────────
def detect_patterns(df: pd.DataFrame) -> dict:
    """
    Scan the last 20 candles for classic candlestick patterns.
    Returns counts and the most recent pattern found.
    """
    if len(df) < 3:
        return {
            "bull_count": 0, "bear_count": 0, "neut_count": 0,
            "latest_pattern": "None", "latest_signal": "Neutral",
        }

    window = df.tail(20).reset_index()
    bull = bear = neut = 0
    latest_name  = "None"
    latest_signal = "Neutral"

    for i in range(2, len(window)):
        c  = window.iloc[i]
        p  = window.iloc[i - 1]
        pp = window.iloc[i - 2]

        body  = abs(c.close - c.open)
        rng   = c.high - c.low
        upper = c.high - max(c.open, c.close)
        lower = min(c.open, c.close) - c.low

        if rng < 1e-6:
            continue

        name = sig = None

        # Doji – indecision
        if body <= rng * 0.1:
            name, sig = "Doji", "Neutral"
            neut += 1

        # Hammer – small body, long lower wick, bullish
        elif lower >= body * 2 and upper <= body * 0.5 and c.close > c.open:
            name, sig = "Hammer", "Bullish"
            bull += 1

        # Inverted Hammer – bullish reversal
        elif upper >= body * 2 and lower <= body * 0.5 and c.close > c.open:
            name, sig = "Inverted Hammer", "Bullish"
            bull += 1

        # Shooting Star – bearish reversal
        elif upper >= body * 2 and lower <= body * 0.5 and c.close < c.open:
            name, sig = "Shooting Star", "Bearish"
            bear += 1

        # Bullish Engulfing
        elif (p.close < p.open and c.close > c.open
              and c.open <= p.close and c.close >= p.open):
            name, sig = "Bullish Engulfing", "Bullish"
            bull += 1

        # Bearish Engulfing
        elif (p.close > p.open and c.close < c.open
              and c.open >= p.close and c.close <= p.open):
            name, sig = "Bearish Engulfing", "Bearish"
            bear += 1

        # Morning Star – three-candle bullish reversal
        elif (pp.close < pp.open
              and abs(p.close - p.open) <= (p.high - p.low) * 0.3
              and c.close > c.open
              and c.close > (pp.open + pp.close) / 2):
            name, sig = "Morning Star", "Bullish"
            bull += 1

        # Evening Star – three-candle bearish reversal
        elif (pp.close > pp.open
              and abs(p.close - p.open) <= (p.high - p.low) * 0.3
              and c.close < c.open
              and c.close < (pp.open + pp.close) / 2):
            name, sig = "Evening Star", "Bearish"
            bear += 1

        if name:
            latest_name, latest_signal = name, sig

    return {
        "bull_count":     bull,
        "bear_count":     bear,
        "neut_count":     neut,
        "latest_pattern": latest_name,
        "latest_signal":  latest_signal,
    }

# ── SUPPORT / RESISTANCE ──────────────────────────────────────────────────────
def find_support_resistance(df: pd.DataFrame, n: int = 60):
    """
    Identify key support and resistance from pivot highs/lows.
    Returns (support, resistance) or (None, None) if not enough data.
    """
    if len(df) < 5:
        return None, None

    window = df.tail(n).reset_index()
    pivot_highs, pivot_lows = [], []

    for i in range(2, len(window) - 2):
        h = window.iloc[i]["high"]
        l = window.iloc[i]["low"]
        if (h > window.iloc[i-1]["high"] and h > window.iloc[i+1]["high"]
                and h > window.iloc[i-2]["high"] and h > window.iloc[i+2]["high"]):
            pivot_highs.append(float(h))
        if (l < window.iloc[i-1]["low"] and l < window.iloc[i+1]["low"]
                and l < window.iloc[i-2]["low"] and l < window.iloc[i+2]["low"]):
            pivot_lows.append(float(l))

    resistance = round(max(pivot_highs[-3:]), 2) if pivot_highs else round(float(df["high"].max()), 2)
    support    = round(min(pivot_lows[-3:]),  2) if pivot_lows  else round(float(df["low"].min()),  2)
    return support, resistance

# ── SAFE FLOAT HELPER ─────────────────────────────────────────────────────────
def _safe(v, decimals: int = 2):
    """Round a potentially NaN/None float safely."""
    try:
        f = float(v)
        return round(f, decimals) if not np.isnan(f) else None
    except (TypeError, ValueError):
        return None

def _json_response(data, status=200):
    """
    aiohttp's json_response dropped the 'default' kwarg in newer versions.
    This helper pre-serialises data using a custom encoder that converts
    non-serialisable types (Timestamp, NaN, numpy scalars, etc.) to safe values.
    """
    def _default(obj):
        import math
        if isinstance(obj, float) and math.isnan(obj):
            return None
        try:
            # numpy scalar types
            return obj.item()
        except AttributeError:
            pass
        return str(obj)
    return web.Response(
        text=json.dumps(data, default=_default),
        content_type="application/json",
        status=status,
    )


