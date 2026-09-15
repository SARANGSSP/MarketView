"""
Test for bug #15 fix — tick_buffer/vol_buffer are now guarded by a real
threading.Lock() (tick_buffer_lock), not just the asyncio.Lock() that
never actually protected against Upstox's background OS streaming thread.

This is a genuine concurrency stress test: a real background thread
hammers on_tick() (simulating Upstox's streaming thread) while the main
thread repeatedly drains tick_buffer/vol_buffer using the exact same
locked pattern now in aggregator_loop(). If any tick is lost — the
original bug — the drained total won't match the number of ticks sent.

No live Upstox connection, no network, no DB. Needs UPSTOX_ACCESS_TOKEN
in .env (any non-empty string).

Run from inside the marketview/ folder:
    python test_tick_buffer_race.py
"""
import sys
import time
import threading
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


SYMBOL = "STRESSTEST"
N_TICKS = 50_000

server.tick_buffer.pop(SYMBOL, None)
server.vol_buffer.pop(SYMBOL, None)

drained_count = 0
drained_volume = 0
errors = []


def drain_once():
    """Exact same locked drain pattern now used in aggregator_loop()."""
    global drained_count, drained_volume
    with server.tick_buffer_lock:
        prices = server.tick_buffer.pop(SYMBOL, [])
        vol    = server.vol_buffer.pop(SYMBOL, 0)
    drained_count  += len(prices)
    drained_volume += vol


def hammer_on_tick():
    """Simulates Upstox's background streaming thread firing ticks rapidly."""
    try:
        for i in range(N_TICKS):
            server.on_tick(SYMBOL, float(100 + i * 0.01), 1, 100.0, 100.0)
    except Exception as e:
        errors.append(e)


print(f"=== Hammering on_tick() with {N_TICKS} ticks from a real background "
      f"thread, draining concurrently from the main thread ===\n")

t = threading.Thread(target=hammer_on_tick)
t.start()

# Drain aggressively and frequently while the hammer thread is still running
# — this is what maximizes the chance of hitting the race window that
# existed before the fix.
drain_rounds = 0
while t.is_alive():
    drain_once()
    drain_rounds += 1

t.join()

# Final drain to catch anything appended right at the very end
drain_once()
drain_rounds += 1

print(f"Ticks sent:      {N_TICKS}")
print(f"Ticks drained:   {drained_count}")
print(f"Volume drained:  {drained_volume}  (expected == ticks sent, 1 per tick)")
print(f"Drain rounds:    {drain_rounds}")
print(f"Exceptions during hammering: {errors}\n")

all_ok = True
all_ok &= check("No exceptions during concurrent on_tick() calls (no dict-size-changed crash)",
                 len(errors) == 0)
all_ok &= check(f"Every tick was drained — zero lost (got {drained_count}/{N_TICKS})",
                 drained_count == N_TICKS)
all_ok &= check(f"Every tick's volume was counted — zero lost (got {drained_volume}/{N_TICKS})",
                 drained_volume == N_TICKS)
all_ok &= check("tick_buffer for this symbol is fully drained afterward (no orphaned entries)",
                 SYMBOL not in server.tick_buffer or len(server.tick_buffer.get(SYMBOL, [])) == 0)

print("\n" + "=" * 60)
print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED — see [FAIL] lines above")
print("=" * 60)
