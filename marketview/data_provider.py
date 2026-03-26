import os
import json
import gzip
import threading
import requests
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

# ── Upstox SDK ──────────────────────────────────────────────────────────────
try:
    import upstox_client
except ImportError:
    raise ImportError("Run:  pip install upstox-python-sdk")


class DataProvider:
    """
    Upstox adapter.  Only this class changes when you switch data vendors.
    Cache is injected at construction time so it can be None in tests.
    """

    INSTRUMENTS_URL = (
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    )

    def __init__(self, cache=None):
        token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not token:
            raise EnvironmentError(
                "UPSTOX_ACCESS_TOKEN not found in .env — "
                "please add it and restart."
            )

        self._config = upstox_client.Configuration()
        self._config.access_token = token
        self._api_client = upstox_client.ApiClient(self._config)

        self._symbol_to_key: dict[str, str] = {}
        self._key_to_name:   dict[str, str] = {}
        self._key_to_symbol: dict[str, str] = {}
        self._name_to_symbol: dict[str, str] = {}   # "RELIANCE INDUSTRIES" → "RELIANCE"
        # Sorted list of (symbol, name) tuples for fast prefix search
        self._search_index: list[tuple[str, str]] = []

        self._streamer  = None
        self._stop_flag = None

        # Injected cache instance (cache.Cache or None)
        self._cache = cache

    # ──────────────────────────────────────────────────────────────────────────
    # 1. INSTRUMENT LOOKUP
    # ──────────────────────────────────────────────────────────────────────────
    def load_instruments(self):
        # ── Cache check ──
        if self._cache:
            cached = self._cache.get_instruments()
            if cached is not None:
                print("[DataProvider] Instruments loaded from cache.")
                instruments = json.loads(gzip.decompress(cached))
                self._build_instrument_maps(instruments)
                return

        print("[DataProvider] Downloading NSE instruments list …")
        resp = requests.get(self.INSTRUMENTS_URL, timeout=30)
        resp.raise_for_status()

        if self._cache:
            self._cache.set_instruments(resp.content)

        raw         = gzip.decompress(resp.content)
        instruments = json.loads(raw)
        self._build_instrument_maps(instruments)

    def _build_instrument_maps(self, instruments: list):
        count = 0
        for inst in instruments:
            if (inst.get("segment") == "NSE_EQ"
                    and inst.get("instrument_type") == "EQ"):
                sym  = inst["trading_symbol"]
                key  = inst["instrument_key"]
                name = inst.get("name", sym)
                self._symbol_to_key[sym.upper()]       = key
                self._key_to_name[key]                 = name
                self._key_to_symbol[key]               = sym.upper()
                self._name_to_symbol[name.upper()]     = sym.upper()
                count += 1

        # Build sorted search index: list of (symbol, company_name) for fast lookup
        self._search_index = sorted(
            [(sym.upper(), self._key_to_name[key])
             for sym, key in self._symbol_to_key.items()],
            key=lambda x: x[0]
        )
        print(f"[DataProvider] Loaded {count} NSE equity instruments.")

    def resolve(self, symbol: str) -> tuple[str, str]:
        key = self._symbol_to_key.get(symbol.upper())
        if not key:
            raise ValueError(
                f"Symbol '{symbol}' not found in NSE instruments. "
                "Check spelling or refresh instruments."
            )
        return key, self._key_to_name.get(key, symbol)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """
        Search NSE instruments by symbol prefix or company name substring.
        Returns up to `limit` results as [{"symbol": ..., "name": ...}].
        Results are ranked: symbol-prefix matches first, then name matches.
        """
        q = query.strip().upper()
        if not q or len(q) < 1:
            return []

        sym_prefix  = []   # symbol starts with query
        name_prefix = []   # company name starts with query
        name_sub    = []   # company name contains query (not prefix)

        for sym, name in self._search_index:
            name_up = name.upper()
            if sym.startswith(q):
                sym_prefix.append({"symbol": sym, "name": name})
            elif name_up.startswith(q):
                name_prefix.append({"symbol": sym, "name": name})
            elif q in name_up:
                name_sub.append({"symbol": sym, "name": name})

        # Sort each bucket alphabetically by symbol
        combined = sym_prefix + name_prefix + name_sub
        return combined[:limit]

    # ──────────────────────────────────────────────────────────────────────────
    # 2. HISTORICAL BASELINE
    # ──────────────────────────────────────────────────────────────────────────
    def get_baseline(self, symbol: str, min_candles: int = 200) -> tuple[pd.DataFrame, str]:
        # ── Cache check ──
        if self._cache:
            cached = self._cache.get_ohlcv(symbol, "baseline")
            if cached is not None:
                df, name = cached
                print(f"[DataProvider] {symbol}: baseline loaded from cache ({len(df)} candles).")
                return df, name

        key, name = self.resolve(symbol)
        api  = upstox_client.HistoryV3Api(self._api_client)
        to_d = date.today().strftime("%Y-%m-%d")
        fr_d = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")

        print(f"[DataProvider] Fetching historical candles for {symbol} …")
        try:
            resp = api.get_historical_candle_data1(
                instrument_key=key,
                unit="days", interval="1",
                to_date=to_d, from_date=fr_d,
            )
        except Exception as e:
            raise RuntimeError(f"Historical API failed for {symbol}: {e}")

        raw_candles = resp.data.candles
        if not raw_candles:
            raise RuntimeError(f"No historical data returned for {symbol}.")

        df = pd.DataFrame(
            raw_candles,
            columns=["time", "open", "high", "low", "close", "volume", "oi"],
        )
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]]
        df = df.sort_index()

        print(f"[DataProvider] {symbol}: {len(df)} candles loaded. "
              f"Last close ₹{df['close'].iloc[-1]:.2f}")

        if self._cache:
            self._cache.set_ohlcv(symbol, "baseline", df, name)

        return df, name

    def get_intraday(self, symbol: str) -> tuple[pd.DataFrame, str]:
        """
        Fetch today's intraday 1-minute candles from market open.
        Uses Upstox V3 get_intra_day_candle_data.
        Returns empty DataFrame if market hasn't opened yet.
        """
        # ── Cache check (short TTL — 30s during market hours) ──
        if self._cache:
            cached = self._cache.get_ohlcv(symbol, "1d")
            if cached is not None:
                df, name = cached
                print(f"[DataProvider] {symbol}: intraday loaded from cache ({len(df)} candles).")
                return df, name

        key, name = self.resolve(symbol)
        api = upstox_client.HistoryV3Api(self._api_client)
        print(f"[DataProvider] Fetching intraday 1m candles for {symbol} …")
        try:
            resp = api.get_intra_day_candle_data(key, "minutes", "1")
        except Exception as e:
            raise RuntimeError(f"Intraday API failed for {symbol}: {e}")

        raw = getattr(getattr(resp, "data", None), "candles", None) or []
        if not raw:
            print(f"[DataProvider] {symbol}: no intraday candles (market closed?) - falling back to baseline.")
            try:
                baseline_df, _ = self.get_baseline(symbol, min_candles=0)
                if baseline_df is not None and len(baseline_df):
                    last = baseline_df.iloc[-1]
                    df = pd.DataFrame(
                        [[last["open"], last["high"], last["low"], last["close"], last["volume"]]],
                        index=pd.DatetimeIndex([baseline_df.index[-1]]),
                        columns=["open", "high", "low", "close", "volume"],
                    )
                    df.index.name = "time"
                    print(f"[DataProvider] {symbol}: baseline LTP fallback -> {last['close']:.2f}")
                    return df, name
            except Exception as be:
                print(f"[DataProvider] {symbol}: baseline fallback failed: {be}")
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df.index.name = "time"
            return df, name

        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume", "oi"])
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]]
        df = df.sort_index()
        print(f"[DataProvider] {symbol}: {len(df)} intraday candles from "
              f"{df.index[0].strftime('%H:%M')} to {df.index[-1].strftime('%H:%M')}")

        if self._cache:
            self._cache.set_ohlcv(symbol, "1d", df, name)

        return df, name

    def get_history(self, symbol: str, range_: str):
        """Fetch historical OHLCV for the given time range."""
        # ── Cache check ──
        if self._cache:
            cached = self._cache.get_ohlcv(symbol, range_)
            if cached is not None:
                df, name = cached
                print(f"[DataProvider] {symbol} {range_}: loaded from cache ({len(df)} candles).")
                return df, name

        key, name = self.resolve(symbol)
        api  = upstox_client.HistoryV3Api(self._api_client)
        to_d = date.today()

        if range_ == "1w":
            from_d, unit, interval = to_d - timedelta(days=120),  "days",  "1"  # enough for Ichimoku
        elif range_ == "1m":
            from_d, unit, interval = to_d - timedelta(days=120),  "days",  "1"  # enough for Ichimoku
        elif range_ == "5y":
            from_d, unit, interval = to_d - timedelta(days=1825), "weeks", "1"
        else:  # default 1y
            from_d, unit, interval = to_d - timedelta(days=365),  "days",  "1"

        print(f"[DataProvider] Fetching {range_} history for {symbol} …")
        resp = api.get_historical_candle_data1(
            instrument_key=key,
            unit=unit, interval=interval,
            to_date=to_d.strftime("%Y-%m-%d"),
            from_date=from_d.strftime("%Y-%m-%d"),
        )

        raw_candles = resp.data.candles
        if not raw_candles:
            raise RuntimeError(f"No historical data returned for {symbol}.")

        df = pd.DataFrame(
            raw_candles,
            columns=["time", "open", "high", "low", "close", "volume", "oi"],
        )
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]]
        df = df.sort_index()

        if self._cache:
            self._cache.set_ohlcv(symbol, range_, df, name)

        return df, name
