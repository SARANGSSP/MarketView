"""
market_calendar.py

Authoritative source for "is the market open right now" — replaces the
previous approach (in data_provider.py's get_intraday()) of *inferring*
market-closed status from an empty Upstox intraday API response. That
inference conflated two independent facts: "the market is closed" and
"today's daily candle has been published" — see bug audit #6, #7, #8.

Primary source: Upstox's GET /v2/market/status/{exchange} and
GET /v2/market/holidays endpoints (via MarketHolidaysAndTimingsApi).
Falls back to a clock heuristic (weekday + 09:15–15:30 IST) if that call
fails — e.g. network hiccup, rate limit — so a single failed API call
never takes the whole app down.

Status is cached in-process and only re-fetched once every CACHE_TTL_SEC,
not once per request — this is what actually fixes #7 (nothing here
gets re-derived from scratch on every single call) and sidesteps the
TTL-mismatch class of bug entirely for this piece of state.
"""
import time
import datetime
import zoneinfo
from dataclasses import dataclass

import upstox_client
from upstox_client.rest import ApiException

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# Upstox market status enum values — see Upstox docs, Appendix > Market Status.
# PRE_OPEN_START / CLOSING_START are transitional phases (no continuous
# trading happening yet/still) and are treated as closed for our purposes.
STATUS_OPEN_VALUES = {"NORMAL_OPEN", "PRE_OPEN_END"}


@dataclass
class MarketStatus:
    is_open:         bool
    source:          str            # "upstox_api" | "clock_heuristic"
    exchange_status: str | None = None   # raw Upstox status string, if available
    checked_at:      float = 0.0         # epoch seconds this was determined


class MarketCalendar:
    """
    Call is_market_open() for a cheap, cached answer to "is the exchange
    open right now". Only hits the Upstox API at most once every
    CACHE_TTL_SEC — everything in between is served from the cached result.
    """

    CACHE_TTL_SEC = 60 * 30   # re-check at most every 30 minutes — frequent
                               # enough to catch open/close/pre-open transitions
                               # within a session, without hitting the API
                               # on every request (that repeated-recomputation
                               # pattern is exactly what caused bug #7).

    def __init__(self, api_client: "upstox_client.ApiClient", exchange: str = "NSE"):
        self._api_client = api_client
        self._exchange = exchange
        self._cached: MarketStatus | None = None
        self._holidays: set[str] | None = None            # {"YYYY-MM-DD", ...}
        self._holidays_fetched_year: int | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        return self.get_status().is_open

    def get_status(self) -> MarketStatus:
        now = time.time()
        if self._cached is not None and (now - self._cached.checked_at) < self.CACHE_TTL_SEC:
            return self._cached

        status = self._fetch_from_api()
        if status is None:
            status = self._fallback_clock_heuristic()

        self._cached = status
        return status

    def is_today_a_holiday(self) -> bool:
        self._ensure_holidays_loaded()
        today_str = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        return today_str in (self._holidays or set())

    # ── Internal ──────────────────────────────────────────────────────────

    def _fetch_from_api(self) -> "MarketStatus | None":
        try:
            api = upstox_client.MarketHolidaysAndTimingsApi(self._api_client)
            resp = api.get_market_status(self._exchange)
            data = getattr(resp, "data", None)
            raw_status = getattr(data, "status", None)
            if raw_status is None:
                print("[MarketCalendar] get_market_status returned no status field, "
                      "falling back to clock heuristic")
                return None
            is_open = raw_status in STATUS_OPEN_VALUES
            print(f"[MarketCalendar] Upstox reports {self._exchange} status: {raw_status} "
                  f"({'OPEN' if is_open else 'CLOSED'})")
            return MarketStatus(is_open=is_open, source="upstox_api",
                                 exchange_status=raw_status, checked_at=time.time())
        except ApiException as e:
            print(f"[MarketCalendar] get_market_status API call failed ({e}), "
                  f"falling back to clock heuristic")
            return None
        except Exception as e:
            print(f"[MarketCalendar] Unexpected error checking market status: {e}, "
                  f"falling back to clock heuristic")
            return None

    def _fallback_clock_heuristic(self) -> MarketStatus:
        """
        Same logic the app previously relied on unconditionally (cache.py's
        old _is_market_hours()) — now demoted to a fallback, only used when
        the authoritative Upstox status API is unreachable.
        """
        ist_now = datetime.datetime.now(IST)
        is_open = False
        if ist_now.weekday() < 5:  # Mon–Fri
            t = ist_now.time()
            is_open = datetime.time(9, 15) <= t <= datetime.time(15, 30)
        if is_open and self.is_today_a_holiday():
            is_open = False
        return MarketStatus(is_open=is_open, source="clock_heuristic", checked_at=time.time())

    def _ensure_holidays_loaded(self):
        current_year = datetime.datetime.now(IST).year
        if self._holidays is not None and self._holidays_fetched_year == current_year:
            return
        try:
            api = upstox_client.MarketHolidaysAndTimingsApi(self._api_client)
            resp = api.get_holidays()
            data = getattr(resp, "data", None) or []
            self._holidays = {h.date for h in data if getattr(h, "date", None)}
            self._holidays_fetched_year = current_year
            print(f"[MarketCalendar] Loaded {len(self._holidays)} holidays for {current_year}")
        except Exception as e:
            print(f"[MarketCalendar] Could not load holiday list: {e}")
            self._holidays = self._holidays or set()


def is_tick_stream_stale(is_open_per_source: bool, last_tick_time: float | None,
                          stale_after_sec: int = 120) -> bool:
    """
    A stream reporting "open" with no tick in the last `stale_after_sec`
    seconds is flagged stale regardless of what the status source says —
    covers cases like a WebSocket silently dying mid-session while the
    market itself is genuinely still open.
    """
    if not is_open_per_source:
        return False
    if last_tick_time is None:
        return False   # no ticks yet at all — nothing to judge staleness against
    return (time.time() - last_tick_time) >= stale_after_sec
