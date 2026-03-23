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
