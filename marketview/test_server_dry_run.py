"""
Test #2 — Dry-run against real server.py functions with a realistic
baseline DataFrame (250 fake daily candles, shaped exactly like what
dp.get_baseline() returns) plus simulated 1-second ticks.

Unlike test_session_logic.py, this imports the ACTUAL run_ta(),
compute_snapshot_ta(), detect_patterns(), etc. from server.py — so it
catches real pandas_ta / indexing / column-name bugs, not just logic
bugs in a reimplementation.

Still fully local — no live Upstox connection, no live DB writes. But
importing server.py does require UPSTOX_ACCESS_TOKEN to be set in your
.env (DataProvider() raises if it's missing — it just needs to be a
non-empty string, it's never actually used to call the API here).

Run from inside the marketview/ folder:
    python test_server_dry_run.py
"""
import sys
import numpy as np
import pandas as pd
from datetime import datetime as _dt

sys.path.insert(0, ".")  # ensure local imports resolve when run from marketview/

try:
    import server
except EnvironmentError as e:
    print(f"[SETUP ERROR] {e}")
    print("Add UPSTOX_ACCESS_TOKEN=dummy_value_for_testing to your .env and retry —")
    print("it just needs to be present, this test never calls the real Upstox API.")
    sys.exit(1)


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


SYMBOL = "RELIANCE"
all_ok = True

# ── Build a realistic ~250-trading-day daily baseline, same shape as
#    dp.get_baseline() returns (DatetimeIndex named "time", OHLCV columns) ──
n_days = 250
dates = pd.bdate_range(end="2026-07-23", periods=n_days)  # business days, ends "yesterday"
rng = np.random.default_rng(42)
base_price = 2900.0
closes = base_price + np.cumsum(rng.normal(0, 15, n_days))
daily_df = pd.DataFrame({
    "open":   closes + rng.normal(0, 5, n_days),
    "high":   closes + np.abs(rng.normal(10, 5, n_days)),
    "low":    closes - np.abs(rng.normal(10, 5, n_days)),
    "close":  closes,
    "volume": rng.integers(500_000, 2_000_000, n_days),
}, index=dates)
daily_df.index.name = "time"

server.daily_data[SYMBOL] = daily_df
yesterday_close = round(float(daily_df["close"].iloc[-1]), 2)
print(f"Baseline loaded: {n_days} daily candles, yesterday's close = {yesterday_close}\n")

# ── Simulate today's session: first tick at 09:15 IST, then many 1-second ticks ──
t0 = int(_dt(2026, 7, 24, 3, 45, 0).timestamp() * 1000)  # 09:15 IST = 03:45 UTC
session_open = float(daily_df["close"].iloc[-1]) + 2.0  # small gap up at open

results = []
price = session_open
for i in range(600):  # past the old 500-row eviction point
    t_i = t0 + i * 1000
    price += rng.normal(0, 1.5)
    candle_ist_date = pd.Timestamp(t_i, unit="ms", tz="UTC").tz_convert("Asia/Kolkata").date()

    if server.session_date.get(SYMBOL) != candle_ist_date:
        server.session_date[SYMBOL]       = candle_ist_date
        server.session_open_price[SYMBOL] = session_open
        server.intraday_data.pop(SYMBOL, None)

    candle = {
        "time":   t_i,
        "open":   price - 0.3,
        "high":   price + 1.0,
        "low":    price - 1.0,
        "close":  price,
        "volume": int(rng.integers(100, 5000)),
    }

    result = server.run_ta(SYMBOL, candle)
    if result is not None:
        results.append(result)

print(f"Simulated 600 ticks. run_ta() returned real results for {len(results)} of them")
print(f"(first ~13 return None — not enough intraday bars yet for RSI-14, expected)\n")

all_ok &= check("run_ta() eventually starts returning results (warm-up completes)", len(results) > 0)

if results:
    first, last = results[0], results[-1]

    # ── The actual bug #1 check: RSI/MACD/BB shouldn't blow up or go NaN
    #    once we're past the old 500-candle eviction point ──
    all_ok &= check("RSI stays within valid 0-100 range at end of run",
                     0 <= last["rsi"] <= 100)
    all_ok &= check("RSI is not NaN", not np.isnan(last["rsi"]))
    all_ok &= check("MACD and MACD histogram are finite numbers",
                     all(np.isfinite(last[k]) for k in ("macd", "macd_histogram")))
    all_ok &= check("macd_signal is a valid directional label",
                     last["macd_signal"] in ("Bullish", "Bearish"))
    all_ok &= check("Bollinger Bands: upper > mid > lower (sane ordering)",
                     last["bb_upper"] is not None and last["bb_lower"] is not None and
                     last["bb_upper"] > last["bb_mid"] > last["bb_lower"])

    # ── bug #4: prev_close should be IDENTICAL across every single result,
    #    from the very first candle to the 600th ──
    prev_closes = {r["prev_close"] for r in results}
    all_ok &= check(f"prev_close is the SAME value across all {len(results)} results (no drift)",
                     len(prev_closes) == 1)
    all_ok &= check(f"prev_close == yesterday's real daily close ({yesterday_close})",
                     prev_closes == {yesterday_close})

    # ── bug #1: 52-week high/low should reflect the 250-day daily series,
    #    not the ~600 rows of intraday ticks ──
    all_ok &= check("w52_high/w52_low computed from the 250-day daily range, not intraday noise",
                     abs(last["w52_high"] - float(daily_df["high"].max())) < 0.01 and
                     abs(last["w52_low"]  - float(daily_df["low"].min()))  < 0.01)

    # ── intraday buffer should have kept growing past the old 500 cap ──
    intraday_len = len(server.intraday_data[SYMBOL])
    all_ok &= check(f"Intraday buffer grew past the old tail(500) cap (has {intraday_len} rows)",
                     intraday_len > 500)

    # ── daily_data should be completely untouched — same row count as loaded ──
    all_ok &= check(f"daily_data still has exactly {n_days} rows (aggregator never touched it)",
                     len(server.daily_data[SYMBOL]) == n_days)

print("\n" + "=" * 60)
print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED — see [FAIL] lines above")
print("=" * 60)
