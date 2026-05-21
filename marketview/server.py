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


# ── READ-ONLY SNAPSHOT TA ─────────────────────────────────────────────────────
def compute_ta_for_df(df: pd.DataFrame) -> dict:
    """
    Compute full TA arrays (per-row BB, RSI, MACD) for a DataFrame.
    Returns per-candle arrays suitable for the frontend charts plus
    scalar summary values for the indicator badges.
    """
    if df is None or len(df) < 14:
        return {}

    # RSI (per-row series)
    rsi_s = df.ta.rsi(length=14)
    rsi_arr = [round(float(v), 2) if v is not None and not np.isnan(float(v)) else None
               for v in (rsi_s if rsi_s is not None else [])]
    rsi_last = rsi_arr[-1] if rsi_arr else 50.0

    # MACD (per-row series)
    df.index = df.index.tz_localize(None) if hasattr(df.index, "tzinfo") and df.index.tzinfo is not None else df.index
    macd_df = df.ta.macd(fast=12, slow=26, signal=9)
    macd_arr = signal_arr = hist_arr = []
    macd_last = signal_last = hist_last = 0.0
    if macd_df is not None and len(macd_df) and "MACD_12_26_9" in macd_df.columns:
        macd_arr   = [_safe(v, 3) for v in macd_df["MACD_12_26_9"]]
        signal_arr = [_safe(v, 3) for v in macd_df["MACDs_12_26_9"]]
        hist_arr   = [_safe(v, 3) for v in macd_df["MACDh_12_26_9"]]
        macd_last   = macd_arr[-1]   or 0.0
        signal_last = signal_arr[-1] or 0.0
        hist_last   = hist_arr[-1]   or 0.0

    # Bollinger Bands (per-row series)
    bb = df.ta.bbands(length=20, std=2)
    bb_upper_arr = bb_lower_arr = bb_mid_arr = []
    bb_upper = bb_lower = bb_mid = None
    if bb is not None and len(bb):
        # Column names vary across pandas-ta versions — find them dynamically
        cols      = bb.columns.tolist()
        col_upper = next((c for c in cols if c.startswith("BBU_")), None)
        col_lower = next((c for c in cols if c.startswith("BBL_")), None)
        col_mid   = next((c for c in cols if c.startswith("BBM_")), None)
        if col_upper and col_lower and col_mid:
            bb_upper_arr = [_safe(v) for v in bb[col_upper]]
            bb_lower_arr = [_safe(v) for v in bb[col_lower]]
            bb_mid_arr   = [_safe(v) for v in bb[col_mid]]
            bb_upper = bb_upper_arr[-1]
            bb_lower = bb_lower_arr[-1]
            bb_mid   = bb_mid_arr[-1]

    # Patterns + S/R
    patterns = detect_patterns(df)
    support, resistance = find_support_resistance(df)

    rsi_signal  = "Overbought" if rsi_last > 70 else "Oversold" if rsi_last < 30 else "Neutral"
    macd_signal = "Bullish" if macd_last > signal_last else "Bearish"
    score       = (2 if rsi_last < 30 else -2 if rsi_last > 70 else 0) + \
                  (1 if macd_last > signal_last else -1) + \
                  (1 if hist_last > 0 else -1)
    composite_signal     = "Buy" if score >= 2 else "Sell" if score <= -2 else "Hold"
    composite_confidence = round(min(0.95, 0.55 + abs(score) * 0.1), 2)

    avg_vol    = float(df["volume"].rolling(20).mean().iloc[-1] or 0)
    vol_signal = "High" if int(df["volume"].iloc[-1]) > avg_vol * 1.5 else "Normal"

    return {
        # Per-row arrays for charts
        "rsi_arr":      rsi_arr,
        "macd_arr":     macd_arr,
        "signal_arr":   signal_arr,
        "hist_arr":     hist_arr,
        "bb_upper_arr": bb_upper_arr,
        "bb_lower_arr": bb_lower_arr,
        "bb_mid_arr":   bb_mid_arr,
        # Scalar summaries for badges
        "rsi": rsi_last, "rsi_signal": rsi_signal,
        "macd": macd_last, "macd_signal": macd_signal, "macd_histogram": hist_last,
        "volume_signal": vol_signal,
        "composite_signal": composite_signal, "composite_confidence": composite_confidence,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_mid": bb_mid,
        "support": support, "resistance": resistance,
        **patterns,
    }


def compute_snapshot_ta(symbol: str) -> dict:
    """
    Compute TA from existing hist_data without appending a candle.
    Used for the initial snapshot sent on WebSocket connect.
    """
    df = hist_data.get(symbol)
    if df is None or len(df) < 14:
        return {}
    return compute_ta_for_df(df)

# ── TECHNICAL ANALYSIS ────────────────────────────────────────────────────────
def run_ta(symbol: str, candle: dict) -> dict | None:
    """
    Appends the new candle to the historical buffer and recalculates all indicators.
    Returns full analysis dict or None if not enough data.
    """
    df = hist_data.get(symbol)
    if df is None or len(df) < 14:
        return None

    new_row = pd.DataFrame([{
        "open":   candle["open"],  "high":   candle["high"],
        "low":    candle["low"],   "close":  candle["close"],
        "volume": candle["volume"],
    }], index=[pd.Timestamp(candle["time"], unit="ms")])
    new_row.index.name = "time"
    combined = pd.concat([df, new_row])

    # RSI
    rsi_s = combined.ta.rsi(length=14)
    rsi   = float(rsi_s.iloc[-1]) if rsi_s is not None else 50.0
    rsi   = round(rsi, 2) if not np.isnan(rsi) else 50.0

    # MACD
    combined.index = pd.to_datetime(combined.index, utc=True)

    combined.index = combined.index.tz_convert('Asia/Kolkata').tz_localize(None)

    macd_df = combined.ta.macd(fast=12, slow=26, signal=9)
    macd_val = signal_val = histogram_val = 0.0
    if macd_df is not None and len(macd_df) and "MACD_12_26_9" in macd_df.columns:
        last = macd_df.iloc[-1]
        macd_val      = round(float(last.get("MACD_12_26_9",  0) or 0), 3)
        signal_val    = round(float(last.get("MACDs_12_26_9", 0) or 0), 3)
        histogram_val = round(float(last.get("MACDh_12_26_9", 0) or 0), 3)

    # Bollinger Bands (20, 2)
    bb = combined.ta.bbands(length=20, std=2)
    bb_upper = bb_lower = bb_mid = None
    if bb is not None and len(bb):
        lb = bb.iloc[-1]
        cols      = bb.columns.tolist()
        col_upper = next((c for c in cols if c.startswith("BBU_")), None)
        col_lower = next((c for c in cols if c.startswith("BBL_")), None)
        col_mid   = next((c for c in cols if c.startswith("BBM_")), None)
        bb_upper = _safe(lb.get(col_upper)) if col_upper else None
        bb_lower = _safe(lb.get(col_lower)) if col_lower else None
        bb_mid   = _safe(lb.get(col_mid))   if col_mid   else None

    # Patterns + S/R
    patterns   = detect_patterns(combined)
    support, resistance = find_support_resistance(combined)

    # Volume signal
    avg_vol    = float(combined["volume"].rolling(20).mean().iloc[-1] or 0)
    vol_signal = "High" if candle["volume"] > avg_vol * 1.5 else "Normal"

    # Directional signals
    rsi_signal  = "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"
    macd_signal = "Bullish" if macd_val > signal_val else "Bearish"

    # Composite signal
    score = (2 if rsi < 30 else -2 if rsi > 70 else 0) + \
            (1 if macd_val > signal_val else -1) + \
            (1 if histogram_val > 0 else -1)
    composite_signal     = "Buy" if score >= 2 else "Sell" if score <= -2 else "Hold"
    composite_confidence = round(min(0.95, 0.55 + abs(score) * 0.1), 2)

    # Stream staleness – no tick in last 30 s during market hours
    last_t   = last_tick_time.get(symbol, 0)
    is_stale = (time.time() - last_t) > 30 if last_t else False

    # Keep buffer trimmed to last 500 candles
    hist_data[symbol] = combined.tail(500)

    # ── Extra stats ──────────────────────────────────────────────────────────
    # Prev close = second-to-last row's close in combined
    prev_close = round(float(combined["close"].iloc[-2]), 2) if len(combined) >= 2 else None

    # VWAP = sum(typical_price * volume) / sum(volume) over the session
    typical = (combined["high"] + combined["low"] + combined["close"]) / 3
    total_vol = combined["volume"].sum()
    vwap = round(float((typical * combined["volume"]).sum() / total_vol), 2) if total_vol > 0 else None

    # 52-week high / low (last 252 trading days ≈ 1 year)
    year_df = combined.tail(252)
    w52_high = round(float(year_df["high"].max()), 2)
    w52_low  = round(float(year_df["low"].min()),  2)

    # Adjusted price — use close as proxy (no adjustment factor available from stream)
    adjusted_price = round(candle["close"], 2)

    return {
        "symbol":         symbol,
        "company_name":   company_names.get(symbol, symbol),
        "time":           candle["time"],
        "open":           round(candle["open"],  2),
        "high":           round(candle["high"],  2),
        "low":            round(candle["low"],   2),
        "close":          round(candle["close"], 2),
        "volume":         candle["volume"],
        "prev_close":     prev_close,
        "vwap":           vwap,
        "w52_high":       w52_high,
        "w52_low":        w52_low,
        "adjusted_price": adjusted_price,
        "rsi":            rsi,         "rsi_signal":   rsi_signal,
        "macd":           macd_val,    "macd_signal":  macd_signal,
        "macd_histogram": histogram_val,
        "volume_signal":  vol_signal,
        "composite_signal":      composite_signal,
        "composite_confidence":  composite_confidence,
        "bb_upper":       bb_upper,    "bb_lower":      bb_lower,   "bb_mid": bb_mid,
        "support":        support,     "resistance":    resistance,
        "is_stale":       is_stale,
        **patterns,
    }

# ── LIVE BAR BROADCAST (250ms) ───────────────────────────────────────────────
async def live_bar_loop():
    """
    Every 250 ms, peek at the current tick_buffer (without draining it) and
    broadcast a lightweight 'live_bar' message to all watchers.
    This lets the frontend show a real-time forming candle.
    """
    print("[LiveBar] Started — broadcasting every 250ms")
    while True:
        try:
            await asyncio.sleep(0.25)
            for symbol, ticks in list(tick_buffer.items()):
                if not ticks or not watchers.get(symbol):
                    continue
                # Peek — do NOT pop; the 1s aggregator will drain these
                prices = list(ticks)
                vol    = vol_buffer.get(symbol, 0)
                live_bar = {
                    "live_bar": True,
                    "symbol":   symbol,
                    "time":     int(time.time() * 1000),
                    "open":     prices[0],
                    "high":     max(prices),
                    "low":      min(prices),
                    "close":    prices[-1],
                    "volume":   vol,
                    "ltp":      prices[-1],
                }
                msg  = json.dumps(live_bar)
                dead = set()
                for ws in list(watchers.get(symbol, [])):
                    try:
                        await ws.send(msg)
                    except websockets.exceptions.ConnectionClosed:
                        dead.add(ws)
                watchers[symbol] -= dead
        except Exception as e:
            print(f"[LiveBar Error]: {e}")


# ── DAILY WHATSAPP SCHEDULER ─────────────────────────────────────────────────
async def scheduled_summaries():
    """Fire WhatsApp summaries at IST market open (09:15) and close (15:30)."""
    import zoneinfo
    from datetime import datetime, time as dtime, timedelta
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    EVENTS = [
        (dtime(9, 15),  send_market_open_summary, "market-open"),
        (dtime(15, 30), send_daily_pnl_summary,   "daily-pnl"),
    ]
    print("[Scheduler] Started — watching for 09:15 and 15:30 IST")
    while True:
        try:
            now   = datetime.now(IST)
            today = now.date()
            next_fire = next_fn = next_label = None
            for t, fn, label in EVENTS:
                candidate = datetime.combine(today, t, tzinfo=IST)
                if now < candidate:
                    if next_fire is None or candidate < next_fire:
                        next_fire, next_fn, next_label = candidate, fn, label
            if next_fire is None:
                tomorrow  = today + timedelta(days=1)
                next_fire = datetime.combine(tomorrow, EVENTS[0][0], tzinfo=IST)
                next_fn, next_label = EVENTS[0][1], EVENTS[0][2]
            sleep_secs = (next_fire - now).total_seconds()
            print(f"[Scheduler] Next: {next_label} at {next_fire.strftime('%Y-%m-%d %H:%M IST')} (in {sleep_secs/60:.1f} min)")
            await asyncio.sleep(max(sleep_secs, 1))
            print(f"[Scheduler] Firing {next_label}")
            await broadcast_summary(next_fn)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Scheduler Error]: {e}")
            await asyncio.sleep(60)

# ── 1-SECOND AGGREGATOR LOOP ─────────────────────────────────────────────────
async def aggregator_loop():
    print(f"[Aggregator] Started — firing every {CANDLE_INTERVAL}s")
    while True:
        try:
            await asyncio.sleep(CANDLE_INTERVAL)
            now = time.time()

            # ── Memory cleanup: evict symbols idle for CLEANUP_IDLE_SEC ──
            for sym in list(hist_data.keys()):
                if not watchers.get(sym):
                    if now - last_watcher.get(sym, now) > CLEANUP_IDLE_SEC:
                        print(f"[Aggregator] Evicting idle symbol: {sym}")
                        hist_data.pop(sym, None)
                        company_names.pop(sym, None)
                        sym_key = sym_to_key.pop(sym, None)
                        last_tick_time.pop(sym, None)
                        last_watcher.pop(sym, None)
                        # Restart stream without the evicted key
                        if sym_key and sym_to_key:
                            dp.start_stream(list(sym_to_key.values()), on_tick)

            # ── Drain tick buffers and broadcast ──
            for symbol, ticks in list(tick_buffer.items()):
                if not ticks:
                    continue

                prices = tick_buffer.pop(symbol, [])
                vol    = vol_buffer.pop(symbol, 0)
                if not prices:
                    continue

                candle = {
                    "time":   int(time.time() * 1000),
                    "open":   prices[0],
                    "high":   max(prices),
                    "low":    min(prices),
                    "close":  prices[-1],
                    "volume": vol,
                }

                try:
                    async with hist_data_lock:
                        result = run_ta(symbol, candle)
                except Exception as e:
                    print(f"[TA ERROR] {symbol}: {e}")
                    continue

                if result is None:
                    continue

                msg  = json.dumps(result)
                dead = set()
                for ws in list(watchers.get(symbol, [])):
                    try:
                        await ws.send(msg)
                    except websockets.exceptions.ConnectionClosed:
                        dead.add(ws)
                watchers[symbol] -= dead

                # ── Fire alert checker on every aggregated candle ──────────
                ltp        = candle["close"]
                open_price = candle["open"]
                pct_change = ((ltp - open_price) / open_price * 100) if open_price else 0.0
                avg_vol    = rolling_avg_vol.get(symbol, float(vol) or 1.0)
                rolling_avg_vol[symbol] = avg_vol * 0.9 + float(vol) * 0.1
                asyncio.create_task(check_alerts(symbol, ltp, pct_change, vol, avg_vol))

                if watchers.get(symbol):
                    last_watcher[symbol] = now

        except Exception as e:
            print(f"[Aggregator Error]: {e}")


async def handle_ltp(request):
    """GET /api/ltp?symbols=RELIANCE,INFY — return last known close per symbol."""
    syms = request.rel_url.query.get("symbols", "").split(",")
    result = {}
    async with hist_data_lock:
        for sym in syms:
            sym = sym.strip()
            if not sym:
                continue
            df = hist_data.get(sym)
            if df is not None and not df.empty:
                result[sym] = float(df["close"].iloc[-1])
    return web.json_response(result)

# ── LOAD SYMBOL ───────────────────────────────────────────────────────────────
async def ensure_symbol_loaded(symbol: str) -> bool:
    if symbol in hist_data:
        key, _ = dp.resolve(symbol)
        if key not in sym_to_key.values():
            sym_to_key[symbol] = key
            dp.start_stream(list(sym_to_key.values()), on_tick)
            print(f"[Server] Subscribed to stream (from cache): {list(sym_to_key.values())}")
        return True
    print(f"[Server] Loading baseline for {symbol}…")
    try:
        df, name = await fetch_with_retry(dp.get_baseline, symbol, BUFFER_MIN)
        async with hist_data_lock:
            hist_data[symbol]     = df
            company_names[symbol] = name
        key, _ = dp.resolve(symbol)
        if key not in sym_to_key.values():
            sym_to_key[symbol] = key
            dp.start_stream(list(sym_to_key.values()), on_tick)
            print(f"[Server] Subscribed to stream: {list(sym_to_key.values())}")
        return True
    except Exception as e:
        print(f"[Server] Failed to load {symbol}: {e}")
        return False

# ── REST: HISTORY ─────────────────────────────────────────────────────────────
async def handle_cache_invalidate(request):
    """
    POST /cache/invalidate?symbol=RELIANCE&range=1y
    Manually evict a cached entry — useful after a bad data fetch or token refresh.
    Omit range to evict all ranges for the symbol.
    Omit both to flush the entire instruments cache.
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    symbol = request.query.get("symbol", "").upper()
    range_ = request.query.get("range", "")
    instr  = request.query.get("instruments", "")

    if instr:
        await asyncio.to_thread(cache.invalidate_instruments)
        return web.json_response({"invalidated": "instruments"})
    elif symbol:
        await asyncio.to_thread(cache.invalidate, symbol, range_ or None)
        return web.json_response({"invalidated": symbol, "range": range_ or "all"})
    else:
        return web.json_response({"error": "Provide symbol= or instruments=1"}, status=400)


def _check_api_key_rest(request) -> bool:
    """Validate API key from query param or Authorization header."""
    if not API_KEY:
        return True   # no key configured → open (dev mode)
    # Accept from header: Authorization: Bearer <key>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == API_KEY:
        return True
    # Accept from query param: ?api_key=<key>
    if request.query.get("api_key") == API_KEY:
        return True
    return False


async def get_history(request):
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    # ── Rate limit by IP ──
    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    range_ = request.query.get("range", "1y")
    if not symbol:
        return web.json_response({"error": "Symbol required"}, status=400)
    try:
        if range_ == "1d":
            df, name = await asyncio.to_thread(dp.get_intraday, symbol)
            candle_type = "intraday"
        else:
            df, name = await asyncio.to_thread(dp.get_history, symbol, range_)
            candle_type = "daily"

        if df.empty:
            return web.json_response({
                "candles": [], "candle_type": candle_type, "patterns": {},
                "rsi_arr": [], "macd_arr": [], "signal_arr": [], "hist_arr": [],
                "bb_upper_arr": [], "bb_lower_arr": [], "bb_mid_arr": [],
                "rsi": 50.0, "rsi_signal": "Neutral", "macd": 0.0,
                "macd_signal": "Neutral", "macd_histogram": 0.0,
                "volume_signal": "Normal", "support": None, "resistance": None,
            })

        ta = compute_ta_for_df(df)

        candles_out = [
            {"t": int(idx.timestamp() * 1000),
             "o": float(r["open"]),  "h": float(r["high"]),
             "l": float(r["low"]),   "c": float(r["close"]),
             "v": int(r["volume"])}
            for idx, r in df.iterrows()
        ]

        # session_open = first candle's open (used for day-change % calc in frontend)
        session_open = float(df["open"].iloc[0]) if candle_type == "intraday" else None

        return web.json_response({
            "candles":      candles_out,
            "candle_type":  candle_type,
            "session_open": session_open,
            "patterns":     {k: ta[k] for k in ("bull_count","bear_count","neut_count",
                                                  "latest_pattern","latest_signal")
                             if k in ta},
            "rsi_arr":      ta.get("rsi_arr", []),
            "macd_arr":     ta.get("macd_arr", []),
            "signal_arr":   ta.get("signal_arr", []),
            "hist_arr":     ta.get("hist_arr", []),
            "bb_upper_arr": ta.get("bb_upper_arr", []),
            "bb_lower_arr": ta.get("bb_lower_arr", []),
            "bb_mid_arr":   ta.get("bb_mid_arr", []),
            "rsi":            ta.get("rsi", 50.0),
            "rsi_signal":     ta.get("rsi_signal", "Neutral"),
            "macd":           ta.get("macd", 0.0),
            "macd_signal":    ta.get("macd_signal", "Neutral"),
            "macd_histogram": ta.get("macd_histogram", 0.0),
            "volume_signal":  ta.get("volume_signal", "Normal"),
            "support":        ta.get("support"),
            "resistance":     ta.get("resistance"),
        })
    except Exception as e:
        print(f"[REST /history error] {e}")
        return web.json_response({"error": str(e)}, status=500)

# ── REST: FUNDAMENTALS ───────────────────────────────────────────────────────

async def handle_fundamentals(request):
    """
    GET /fundamentals?symbol=RELIANCE
    Returns key ratios: P/E, P/B, ROE, ROCE, debt-to-equity, EPS, market cap, etc.
    Data is fetched from yfinance and cached in PostgreSQL for 24 hours.
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        data = await fp.get_fundamentals(symbol)
        if not data:
            return web.json_response(
                {"error": f"No fundamental data found for {symbol}. "
                          "Check the symbol is a valid NSE equity."},
                status=404
            )
        return _json_response(data)
    except Exception as e:
        log.error("[REST /fundamentals] %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_financials_quarterly(request):
    """
    GET /financials/quarterly?symbol=RELIANCE
    Returns the last 8 quarters of P&L:
      period, sales, expenses, operating_profit, net_profit, eps
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        rows = await fp.get_quarterly(symbol)
        return _json_response({"symbol": symbol, "quarters": rows})
    except Exception as e:
        log.error("[REST /financials/quarterly] %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_financials_annual(request):
    """
    GET /financials/annual?symbol=RELIANCE
    Returns up to 10 years of annual P&L.
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        rows = await fp.get_annual(symbol)
        return _json_response({"symbol": symbol, "annual": rows})
    except Exception as e:
        log.error("[REST /financials/annual] %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_balance_sheet(request):
    """
    GET /financials/balance-sheet?symbol=RELIANCE
    Returns up to 10 years of annual balance sheet:
      year, total_assets, total_liabilities, total_equity, borrowings, reserves
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        rows = await fp.get_balance_sheet(symbol)
        return _json_response({"symbol": symbol, "balance_sheet": rows})
    except Exception as e:
        log.error("[REST /financials/balance-sheet] %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_cashflow(request):
    """
    GET /financials/cashflow?symbol=RELIANCE
    Returns up to 10 years of annual cash flow:
      year, operating_cashflow, investing_cashflow, financing_cashflow, free_cashflow
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        rows = await fp.get_cashflow(symbol)
        return _json_response({"symbol": symbol, "cashflow": rows})
    except Exception as e:
        log.error("[REST /financials/cashflow] %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_financials_all(request):
    """
    GET /financials/all?symbol=RELIANCE
    Returns everything in a single call:
      fundamentals, quarterly, annual, balance_sheet, cashflow
    Useful for populating a full company page in one network request.
    """
    if not _check_api_key_rest(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    ip = request.remote or "unknown"
    if not _check_rate_limit(ip):
        return web.json_response({"error": "Rate limit exceeded"}, status=429)

    symbol = request.query.get("symbol", "").upper()
    if not symbol:
        return web.json_response({"error": "symbol parameter required"}, status=400)

    try:
        data = await fp.get_all(symbol)
        return _json_response(data)
    except Exception as e:
        log.error("[REST /financials/all] %s", e)
        return web.json_response({"error": str(e)}, status=500)


# ── WEBSOCKET HANDLER ─────────────────────────────────────────────────────────
async def handler(websocket):
    remote = websocket.remote_address
    ip     = remote[0] if remote else "unknown"
    print(f"[WS] Client connected: {ip}")
    current_symbol = None

    # ── Rate limit ──
    if not _check_rate_limit(ip):
        print(f"[WS] Rate limit exceeded for {ip}")
        await websocket.send(json.dumps({"error": "Rate limit exceeded. Please slow down."}))
        await websocket.close(1008, "Rate limit exceeded")
        return

    # ── API key auth (if API_KEY set, first message must be the key) ──
    if API_KEY:
        try:
            first_msg = await asyncio.wait_for(websocket.recv(), timeout=10)
        except asyncio.TimeoutError:
            await websocket.close(1008, "Auth timeout")
            return
        if first_msg.strip() != API_KEY:
            print(f"[WS] Invalid API key from {ip}")
            await websocket.send(json.dumps({"error": "Unauthorized"}))
            await websocket.close(1008, "Unauthorized")
            return

    try:
        async for message in websocket:
            try:
                symbol = message.strip().upper()
                if not symbol:
                    continue

                print(f"[WS] {remote} requested: {symbol}")

                if current_symbol and current_symbol in watchers:
                    watchers[current_symbol].discard(websocket)

                try:
                    dp.resolve(symbol)
                except ValueError as e:
                    await websocket.send(json.dumps({"error": str(e)}))
                    continue

                ok = await ensure_symbol_loaded(symbol)
                if not ok:
                    await websocket.send(json.dumps({"error": f"Could not load data for {symbol}"}))
                    continue

                current_symbol = symbol
                watchers[symbol].add(websocket)
                last_watcher[symbol] = time.time()

                # Send real snapshot using existing hist_data (no dummy values)
                async with hist_data_lock:
                    df       = hist_data[symbol]
                    last_row = df.iloc[-1]
                    ta_snap  = compute_snapshot_ta(symbol)  # now returns full arrays too

                    history_candles = [
                        {"t": int(idx.timestamp() * 1000),
                         "o": round(float(r["open"]),  2),
                         "h": round(float(r["high"]),  2),
                         "l": round(float(r["low"]),   2),
                         "c": round(float(r["close"]), 2),
                         "v": int(r["volume"])}
                        for idx, r in df.tail(150).iterrows()
                    ]

                    # Extra stats from full history buffer
                    _typical    = (df["high"] + df["low"] + df["close"]) / 3
                    _tvol       = df["volume"].sum()
                    _vwap       = round(float((_typical * df["volume"]).sum() / _tvol), 2) if _tvol > 0 else None
                    _year_df    = df.tail(252)
                    _w52_high   = round(float(_year_df["high"].max()), 2)
                    _w52_low    = round(float(_year_df["low"].min()),  2)
                    _prev_close = round(float(df["close"].iloc[-2]), 2) if len(df) >= 2 else None

                    snapshot = {
                        "snapshot":       True,
                        "candle_type":    "daily",
                        "symbol":         symbol,
                        "company_name":   company_names.get(symbol, symbol),
                        "time":           int(time.time() * 1000),
                        "open":           round(float(last_row["open"]),  2),
                        "high":           round(float(last_row["high"]),  2),
                        "low":            round(float(last_row["low"]),   2),
                        "close":          round(float(last_row["close"]), 2),
                        "volume":         int(last_row["volume"]),
                        "session_open":   round(float(last_row["close"]), 2),
                        "prev_close":     _prev_close,
                        "vwap":           _vwap,
                        "w52_high":       _w52_high,
                        "w52_low":        _w52_low,
                        "adjusted_price": round(float(last_row["close"]), 2),
                        "history":        history_candles,
                        # Scalar badges
                        "rsi":            ta_snap.get("rsi", 50.0),
                        "rsi_signal":     ta_snap.get("rsi_signal", "Neutral"),
                        "macd":           ta_snap.get("macd", 0.0),
                        "macd_signal":    ta_snap.get("macd_signal", "Neutral"),
                        "macd_histogram": ta_snap.get("macd_histogram", 0.0),
                        "volume_signal":  ta_snap.get("volume_signal", "Normal"),
                        "composite_signal":      ta_snap.get("composite_signal", "Hold"),
                        "composite_confidence":  ta_snap.get("composite_confidence", 0.55),
                        "bb_upper":       ta_snap.get("bb_upper"),
                        "bb_lower":       ta_snap.get("bb_lower"),
                        "bb_mid":         ta_snap.get("bb_mid"),
                        "support":        ta_snap.get("support"),
                        "resistance":     ta_snap.get("resistance"),
                        "bull_count":     ta_snap.get("bull_count", 0),
                        "bear_count":     ta_snap.get("bear_count", 0),
                        "neut_count":     ta_snap.get("neut_count", 0),
                        "latest_pattern": ta_snap.get("latest_pattern", "None"),
                        "latest_signal":  ta_snap.get("latest_signal", "Neutral"),
                        "is_stale":       False,
                        # Full per-candle TA arrays (aligned with history_candles, last 150)
                        "rsi_arr":        ta_snap.get("rsi_arr",      [])[-150:],
                        "macd_arr":       ta_snap.get("macd_arr",     [])[-150:],
                        "signal_arr":     ta_snap.get("signal_arr",   [])[-150:],
                        "hist_arr":       ta_snap.get("hist_arr",     [])[-150:],
                        "bb_upper_arr":   ta_snap.get("bb_upper_arr", [])[-150:],
                        "bb_lower_arr":   ta_snap.get("bb_lower_arr", [])[-150:],
                        "bb_mid_arr":     ta_snap.get("bb_mid_arr",   [])[-150:],
                    }
                await websocket.send(json.dumps(snapshot))
                print(f"[WS] Snapshot sent for {symbol}.")

            except Exception as e:
                print(f"[WS Handler Error]: {e}")

    except websockets.exceptions.ConnectionClosed as e:
        print(f"[WS] Connection closed: {e}")
    finally:
        if current_symbol:
            watchers[current_symbol].discard(websocket)
        print(f"[WS] Client disconnected: {remote}")

# ── STARTUP ───────────────────────────────────────────────────────────────────
async def handle_search(request):
    q = request.query.get("q", "").strip()
    limit = int(request.query.get("limit", 10))
    results = dp.search(q, limit)
    return _json_response(results)

async def main():
    print("=" * 60)
    print("  MarketView Server – Upstox Edition")
    print("=" * 60)

    print("\n[Startup] Opening cache database…")
    await asyncio.to_thread(cache.open)

    print("\n[Startup] Loading instruments…")
    try:
        await asyncio.to_thread(dp.load_instruments)
    except Exception as e:
        print(f"[Startup] FATAL — Could not load instruments: {e}")
        return

    print("\n[Startup] Pre-loading RELIANCE baseline…")
    await ensure_symbol_loaded("RELIANCE")

    asyncio.create_task(aggregator_loop())
    asyncio.create_task(scheduled_summaries())
    asyncio.create_task(live_bar_loop())

    print(f"\n[Startup] WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    print(f"[Startup] Frontend available at http://localhost:{REST_PORT}")
    print(f"[Startup] DuckDNS access:   http://yourname.duckdns.org:{REST_PORT}\n")

    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True, expose_headers="*", allow_headers="*",
        )
    })
    app.router.add_get("/history", get_history)
    app.router.add_get("/health",  lambda r: web.json_response({"status": "ok"}))
    app.router.add_get("/cache/stats",
        lambda r: web.json_response(cache.stats()))
    app.router.add_post("/cache/invalidate",
        handle_cache_invalidate)
    # ── Fundamentals endpoints ──────────────────────────────────────────────
    app.router.add_get("/fundamentals",            handle_fundamentals)
    app.router.add_get("/financials/quarterly",    handle_financials_quarterly)
    app.router.add_get("/financials/annual",       handle_financials_annual)
    app.router.add_get("/financials/balance-sheet",handle_balance_sheet)
    app.router.add_get("/financials/cashflow",     handle_cashflow)
    app.router.add_get("/financials/all",          handle_financials_all)
    attach_auth_routes(app)  # /auth/login, /auth/callback, /auth/status
    app.router.add_get('/api/ltp', handle_ltp)
    attach_portfolio_routes(app)  # /auth/google/*, /portfolio, /alerts, /user

    # ── Serve frontend static files ──────────────────────────────────────────
    # marketview.html → /
    # static/marketview.js, static/style.css → /static/
    app.router.add_get("/search", handle_search)
    app.router.add_get("/", lambda r: web.FileResponse("./marketview.html"))
    app.router.add_static("/static", path="./static", name="static")
    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", REST_PORT, reuse_address=True, reuse_port=(sys.platform != "win32"))
    try:
        await site.start()
    except OSError as e:
        if e.errno in (10048, 98):   # 10048 = Windows WSAEADDRINUSE, 98 = Linux EADDRINUSE
            print(f"\n[FATAL] Port {REST_PORT} is already in use.")
            print(f"        A previous server.py may still be running.")
            print(f"        On Windows, run:  taskkill /IM python.exe /F")
            print(f"        On Linux/Mac, run: kill $(lsof -ti:{REST_PORT})")
            print(f"        Then restart server.py.\n")
        else:
            print(f"\n[FATAL] Could not bind REST API: {e}\n")
        await runner.cleanup()
        return
    print(f"[Startup] REST API on http://localhost:{REST_PORT}/history")

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _request_shutdown():
        print("\n[Shutdown] Signal received — shutting down cleanly…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT,  _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

    try:
        ws_server = await websockets.serve(handler, WS_HOST, WS_PORT)
    except OSError as e:
        if e.errno in (10048, 98):
            print(f"\n[FATAL] Port {WS_PORT} is already in use.")
            print(f"        A previous server.py may still be running.")
            print(f"        On Windows, run:  taskkill /IM python.exe /F\n")
        else:
            print(f"\n[FATAL] Could not start WebSocket server: {e}\n")
        await runner.cleanup()
        return

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        ws_server.close()
        await ws_server.wait_closed()

    # Cleanup
    print("[Shutdown] Stopping stream…")
    dp.stop_stream()
    print("[Shutdown] Closing cache…")
    cache.close()
    print("[Shutdown] Stopping REST API…")
    await runner.cleanup()
    print("[Shutdown] Done. Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Suppress traceback — shutdown was already handled cleanly
