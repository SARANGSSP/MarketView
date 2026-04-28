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

