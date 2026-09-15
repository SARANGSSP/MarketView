"""
Test for bug #14 fix — compute_snapshot_ta() now falls back to
dp.get_intraday() (today's 1-minute candles, real Upstox data) when the
live intraday buffer doesn't have enough bars yet (freshly loaded or
just-reconnected symbol), instead of returning {} (blank indicators) for
the first ~15 seconds.

Uses synthetic 1-minute candle data shaped like Upstox's real response —
no live Upstox connection, no network. Needs UPSTOX_ACCESS_TOKEN present
in .env (any non-empty string; never actually used to call the API here).

Run from inside the marketview/ folder:
    python test_snapshot_warmup.py
"""
import sys
import asyncio
import numpy as np
import pandas as pd
sys.path.insert(0, ".")

try:
    import server
except EnvironmentError as e:
    print(f"[SETUP ERROR] {e}")
    print("Add UPSTOX_ACCESS_TOKEN=dummy_value_for_testing to your .env and retry.")
    sys.exit(1)


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


all_ok = True
SYMBOL = "RELIANCE"

# ── Test 1: live buffer completely empty (symbol just loaded/reconnected) ──
print("=== Live buffer empty, no get_intraday fallback data either ===\n")
server.intraday_data.pop(SYMBOL, None)

async def fake_get_intraday_empty(sym):
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    empty.index.name = "time"
    return empty, "Reliance Industries"

original_fetch = server.fetch_with_retry
async def fake_fetch_empty(fn, *args, **kwargs):
    return await fake_get_intraday_empty(*args)

server.fetch_with_retry = fake_fetch_empty
result_empty = asyncio.run(server.compute_snapshot_ta(SYMBOL))
server.fetch_with_retry = original_fetch

all_ok &= check("No data anywhere -> returns {} (safe, no crash)", result_empty == {})

# ── Test 2: live buffer empty, but get_intraday has real same-day data ──
print("\n=== Live buffer empty, but Upstox has 45 minutes of today's real candles ===\n")
server.intraday_data.pop(SYMBOL, None)

rng = np.random.default_rng(7)
n = 45
idx = pd.date_range("2026-07-24 09:15:00", periods=n, freq="1min")
closes = 2900 + np.cumsum(rng.normal(0, 2, n))
minute_df = pd.DataFrame({
    "open":   closes + rng.normal(0, 1, n),
    "high":   closes + np.abs(rng.normal(2, 1, n)),
    "low":    closes - np.abs(rng.normal(2, 1, n)),
    "close":  closes,
    "volume": rng.integers(1000, 50000, n),
}, index=idx)
minute_df.index.name = "time"

async def fake_fetch_with_data(fn, *args, **kwargs):
    return minute_df, "Reliance Industries"

server.fetch_with_retry = fake_fetch_with_data
result_with_data = asyncio.run(server.compute_snapshot_ta(SYMBOL))
server.fetch_with_retry = original_fetch

all_ok &= check("Returns real computed indicators (not blank {})", result_with_data != {})
if result_with_data:
    all_ok &= check("Contains an rsi key with a real value",
                     "rsi" in result_with_data and result_with_data["rsi"] is not None)
    all_ok &= check("RSI is within valid 0-100 range",
                     0 <= result_with_data.get("rsi", -1) <= 100)

all_ok &= check("Live intraday_data buffer was NOT modified by this fallback "
                 "(run_ta's future live computation stays pure single-granularity, per bug #1)",
                 SYMBOL not in server.intraday_data or len(server.intraday_data.get(SYMBOL, [])) == 0)

# ── Test 3: live buffer already has enough real data -> should use it directly,
#    NOT call get_intraday at all (avoid an unnecessary API call) ──
print("\n=== Live buffer already warm (20 rows) -> should skip the fallback entirely ===\n")

live_idx = pd.date_range("2026-07-24 10:00:00", periods=20, freq="1s")
live_closes = 2950 + np.cumsum(rng.normal(0, 0.5, 20))
live_df = pd.DataFrame({
    "open": live_closes, "high": live_closes + 0.5, "low": live_closes - 0.5,
    "close": live_closes, "volume": rng.integers(100, 1000, 20),
}, index=live_idx)
server.intraday_data[SYMBOL] = live_df

fallback_was_called = {"yes": False}
async def fake_fetch_should_not_be_called(fn, *args, **kwargs):
    fallback_was_called["yes"] = True
    return minute_df, "Reliance Industries"

server.fetch_with_retry = fake_fetch_should_not_be_called
result_warm = asyncio.run(server.compute_snapshot_ta(SYMBOL))
server.fetch_with_retry = original_fetch

all_ok &= check("Fallback (get_intraday) was NOT called when live buffer is already warm",
                 fallback_was_called["yes"] is False)
all_ok &= check("Real indicators still returned from the live buffer", result_warm != {})

server.intraday_data.pop(SYMBOL, None)  # cleanup

print("\n" + "=" * 60)
print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED — see [FAIL] lines above")
print("=" * 60)
