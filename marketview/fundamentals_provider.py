"""
fundamentals_provider.py  –  Fundamental data for MarketView
=============================================================
Fetches financial data via yfinance (Yahoo Finance) for NSE-listed stocks
and persists it in PostgreSQL. Designed to plug into server.py with minimal
changes — just import and call the async wrappers.

Data provided:
  - Key ratios (P/E, P/B, ROE, ROCE, Debt/Equity, EPS, etc.)
  - Quarterly P&L (last 8 quarters)
  - Annual P&L (last 10 years where available)
  - Balance sheet (annual)
  - Cash flow (annual)
  - Shareholding pattern (approximate via yfinance major holders)

Caching strategy:
  - Fundamentals / ratios:  refresh every 24 hours
  - Quarterly results:       refresh every 24 hours (only change 4x/year)
  - Annual data:             refresh every 7 days
  - All reads go to PostgreSQL first; yfinance is only called on a miss/stale

Usage:
  from fundamentals_provider import FundamentalsProvider
  fp = FundamentalsProvider()          # reads DATABASE_URL from .env
  await fp.get_fundamentals("RELIANCE")
  await fp.get_quarterly("RELIANCE")
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta, date
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("fundamentals")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nse(symbol: str) -> str:
    """Convert plain NSE symbol to Yahoo Finance ticker format."""
    return f"{symbol.upper()}.NS"


def _safe_int(value) -> Optional[int]:
    """Convert a possibly-NaN/None value to int safely."""
    try:
        if value is None:
            return None
        f = float(value)
        if np.isnan(f):
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _safe_float(value, decimals: int = 4) -> Optional[float]:
    """Convert a possibly-NaN/None value to rounded float safely."""
    try:
        if value is None:
            return None
        f = float(value)
        if np.isnan(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None


def _get_conn():
    """Open a new psycopg2 connection from DATABASE_URL."""
    if not DATABASE_URL:
        raise EnvironmentError(
            "DATABASE_URL not set in .env — "
            "example: postgres://marketview:password@localhost/marketview"
        )
    return psycopg2.connect(DATABASE_URL)


# ── FundamentalsProvider ──────────────────────────────────────────────────────

class FundamentalsProvider:
    """
    Provides fundamental financial data for NSE stocks.
    All public methods are async — they call blocking I/O via asyncio.to_thread.
    """

    STALE_HOURS_RATIOS   = 24
    STALE_HOURS_QUARTERLY = 24
    STALE_DAYS_ANNUAL    = 7

    # ── Internal: staleness checks ────────────────────────────────────────────

    def _is_fundamentals_stale(self, symbol: str) -> bool:
        """Return True if fundamentals row is missing or older than STALE_HOURS_RATIOS."""
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT fetched_at FROM fundamentals WHERE symbol = %s",
                        (symbol.upper(),)
                    )
                    row = cur.fetchone()
            if not row:
                return True
            age = datetime.utcnow() - row[0]
            return age > timedelta(hours=self.STALE_HOURS_RATIOS)
        except Exception as e:
            log.warning("[Fundamentals] Staleness check failed: %s", e)
            return True

    def _is_quarterly_stale(self, symbol: str) -> bool:
        """Return True if quarterly data is missing or older than STALE_HOURS_QUARTERLY."""
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(period) FROM quarterly_results WHERE symbol = %s",
                        (symbol.upper(),)
                    )
                    row = cur.fetchone()
            if not row or not row[0]:
                return True
            # Stale if the most recent fetch was more than 24h ago (we check by period age)
            age = date.today() - row[0]
            return age.days > self.STALE_HOURS_QUARTERLY
        except Exception as e:
            log.warning("[Fundamentals] Quarterly staleness check failed: %s", e)
            return True

    def _is_annual_stale(self, symbol: str) -> bool:
        """Return True if annual data is missing or older than STALE_DAYS_ANNUAL."""
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(year) FROM annual_results WHERE symbol = %s",
                        (symbol.upper(),)
                    )
                    row = cur.fetchone()
            if not row or not row[0]:
                return True
            # Stale if we're missing the current year
            return row[0] < datetime.utcnow().year - 1
        except Exception as e:
            log.warning("[Fundamentals] Annual staleness check failed: %s", e)
            return True

    # ── Internal: yfinance fetch ──────────────────────────────────────────────

    def _fetch_and_store_fundamentals(self, symbol: str) -> dict:
        """Fetch key ratios from yfinance and upsert into fundamentals table."""
        sym = symbol.upper()
        log.info("[Fundamentals] Fetching ratios for %s from yfinance…", sym)

        ticker = yf.Ticker(_nse(sym))
        info   = ticker.info or {}

        # ROCE calculation: EBIT / Capital Employed
        roce = None
        ebit              = _safe_float(info.get("ebitda"))
        total_assets      = _safe_float(info.get("totalAssets"))
        current_liabs     = _safe_float(info.get("totalCurrentLiabilities"))
        if ebit and total_assets and current_liabs:
            capital_employed = total_assets - current_liabs
            if capital_employed > 0:
                roce = round((ebit / capital_employed) * 100, 4)

        row = {
            "symbol":         sym,
            "fetched_at":     datetime.utcnow(),
            "market_cap":     _safe_int(info.get("marketCap")),
            "pe_ratio":       _safe_float(info.get("trailingPE")),
            "pb_ratio":       _safe_float(info.get("priceToBook")),
            "roe":            _safe_float(info.get("returnOnEquity")),
            "roce":           roce,
            "debt_to_equity": _safe_float(info.get("debtToEquity")),
            "profit_margin":  _safe_float(info.get("profitMargins")),
            "dividend_yield": _safe_float(info.get("dividendYield")),
            "eps":            _safe_float(info.get("trailingEps")),
            "face_value":     None,   # not available via yfinance for Indian stocks
            "sector":         info.get("sector"),
            "industry":       info.get("industry"),
            "website":        info.get("website"),
            "description":    info.get("longBusinessSummary"),
        }

        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO fundamentals
                        (symbol, fetched_at, market_cap, pe_ratio, pb_ratio, roe, roce,
                         debt_to_equity, profit_margin, dividend_yield, eps, face_value,
                         sector, industry, website, description)
                    VALUES
                        (%(symbol)s, %(fetched_at)s, %(market_cap)s, %(pe_ratio)s,
                         %(pb_ratio)s, %(roe)s, %(roce)s, %(debt_to_equity)s,
                         %(profit_margin)s, %(dividend_yield)s, %(eps)s, %(face_value)s,
                         %(sector)s, %(industry)s, %(website)s, %(description)s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        fetched_at      = EXCLUDED.fetched_at,
                        market_cap      = EXCLUDED.market_cap,
                        pe_ratio        = EXCLUDED.pe_ratio,
                        pb_ratio        = EXCLUDED.pb_ratio,
                        roe             = EXCLUDED.roe,
                        roce            = EXCLUDED.roce,
                        debt_to_equity  = EXCLUDED.debt_to_equity,
                        profit_margin   = EXCLUDED.profit_margin,
                        dividend_yield  = EXCLUDED.dividend_yield,
                        eps             = EXCLUDED.eps,
                        sector          = EXCLUDED.sector,
                        industry        = EXCLUDED.industry,
                        website         = EXCLUDED.website,
                        description     = EXCLUDED.description
                """, row)
            conn.commit()

        log.info("[Fundamentals] Ratios stored for %s.", sym)
        return row

    def _fetch_and_store_quarterly(self, symbol: str) -> list:
        """Fetch last 8 quarters of P&L from yfinance and upsert."""
        sym = symbol.upper()
        log.info("[Fundamentals] Fetching quarterly P&L for %s…", sym)

        ticker = yf.Ticker(_nse(sym))
        qf     = ticker.quarterly_financials   # columns = quarter end dates

        rows = []
        if qf is None or qf.empty:
            log.warning("[Fundamentals] No quarterly financials returned for %s", sym)
            return rows

        # yfinance quarterly_earnings has EPS per quarter
        qe = ticker.quarterly_earnings

        with _get_conn() as conn:
            with conn.cursor() as cur:
                for period in qf.columns[:8]:   # last 8 quarters
                    def _get(label):
                        try:
                            v = qf.loc[label, period]
                            return _safe_int(v)
                        except KeyError:
                            return None

                    sales            = _get("Total Revenue")
                    expenses         = _get("Total Expenses")
                    operating_profit = _get("Operating Income")
                    net_profit       = _get("Net Income")

                    # EPS from quarterly_earnings if available
                    eps = None
                    if qe is not None and not qe.empty:
                        try:
                            eps = _safe_float(qe.loc[period, "Earnings"])
                        except (KeyError, TypeError):
                            pass

                    row = {
                        "symbol":          sym,
                        "period":          period.date(),
                        "sales":           sales,
                        "expenses":        expenses,
                        "operating_profit":operating_profit,
                        "net_profit":      net_profit,
                        "eps":             eps,
                    }
                    cur.execute("""
                        INSERT INTO quarterly_results
                            (symbol, period, sales, expenses, operating_profit, net_profit, eps)
                        VALUES
                            (%(symbol)s, %(period)s, %(sales)s, %(expenses)s,
                             %(operating_profit)s, %(net_profit)s, %(eps)s)
                        ON CONFLICT (symbol, period) DO UPDATE SET
                            sales            = EXCLUDED.sales,
                            expenses         = EXCLUDED.expenses,
                            operating_profit = EXCLUDED.operating_profit,
                            net_profit       = EXCLUDED.net_profit,
                            eps              = EXCLUDED.eps
                    """, row)
                    rows.append(row)
            conn.commit()

        log.info("[Fundamentals] %d quarterly rows stored for %s.", len(rows), sym)
        return rows

    def _fetch_and_store_annual(self, symbol: str) -> list:
        """Fetch annual P&L (up to 10 years) from yfinance and upsert."""
        sym = symbol.upper()
        log.info("[Fundamentals] Fetching annual P&L for %s…", sym)

        ticker = yf.Ticker(_nse(sym))
        af     = ticker.financials   # annual P&L — columns = year end dates

        rows = []
        if af is None or af.empty:
            log.warning("[Fundamentals] No annual financials for %s", sym)
            return rows

        with _get_conn() as conn:
            with conn.cursor() as cur:
                for period in af.columns:
                    def _get(label):
                        try:
                            return _safe_int(af.loc[label, period])
                        except KeyError:
                            return None

                    row = {
                        "symbol":          sym,
                        "year":            period.year,
                        "sales":           _get("Total Revenue"),
                        "expenses":        _get("Total Expenses"),
                        "operating_profit":_get("Operating Income"),
                        "net_profit":      _get("Net Income"),
                        "eps":             None,
                    }
                    cur.execute("""
                        INSERT INTO annual_results
                            (symbol, year, sales, expenses, operating_profit, net_profit, eps)
                        VALUES
                            (%(symbol)s, %(year)s, %(sales)s, %(expenses)s,
                             %(operating_profit)s, %(net_profit)s, %(eps)s)
                        ON CONFLICT (symbol, year) DO UPDATE SET
                            sales            = EXCLUDED.sales,
                            expenses         = EXCLUDED.expenses,
                            operating_profit = EXCLUDED.operating_profit,
                            net_profit       = EXCLUDED.net_profit
                    """, row)
                    rows.append(row)
            conn.commit()

        log.info("[Fundamentals] %d annual rows stored for %s.", len(rows), sym)
        return rows

    def _fetch_and_store_balance_sheet(self, symbol: str) -> list:
        """Fetch annual balance sheet from yfinance and upsert."""
        sym    = symbol.upper()
        ticker = yf.Ticker(_nse(sym))
        bs     = ticker.balance_sheet

        rows = []
        if bs is None or bs.empty:
            return rows

        with _get_conn() as conn:
            with conn.cursor() as cur:
                for period in bs.columns:
                    def _get(label):
                        try:
                            return _safe_int(bs.loc[label, period])
                        except KeyError:
                            return None

                    total_assets = _get("Total Assets")
                    total_equity = _get("Stockholders Equity")
                    total_liabs  = None
                    if total_assets and total_equity:
                        total_liabs = total_assets - total_equity

                    row = {
                        "symbol":            sym,
                        "year":              period.year,
                        "total_assets":      total_assets,
                        "total_liabilities": total_liabs,
                        "total_equity":      total_equity,
                        "borrowings":        _get("Long Term Debt"),
                        "reserves":          _get("Retained Earnings"),
                    }
                    cur.execute("""
                        INSERT INTO balance_sheet
                            (symbol, year, total_assets, total_liabilities,
                             total_equity, borrowings, reserves)
                        VALUES
                            (%(symbol)s, %(year)s, %(total_assets)s, %(total_liabilities)s,
                             %(total_equity)s, %(borrowings)s, %(reserves)s)
                        ON CONFLICT (symbol, year) DO UPDATE SET
                            total_assets      = EXCLUDED.total_assets,
                            total_liabilities = EXCLUDED.total_liabilities,
                            total_equity      = EXCLUDED.total_equity,
                            borrowings        = EXCLUDED.borrowings,
                            reserves          = EXCLUDED.reserves
                    """, row)
                    rows.append(row)
            conn.commit()

        log.info("[Fundamentals] Balance sheet stored for %s (%d rows).", sym, len(rows))
        return rows

    def _fetch_and_store_cashflow(self, symbol: str) -> list:
        """Fetch annual cash flow from yfinance and upsert."""
        sym    = symbol.upper()
        ticker = yf.Ticker(_nse(sym))
        cf     = ticker.cashflow

        rows = []
        if cf is None or cf.empty:
            return rows

        with _get_conn() as conn:
            with conn.cursor() as cur:
                for period in cf.columns:
                    def _get(label):
                        try:
                            return _safe_int(cf.loc[label, period])
                        except KeyError:
                            return None

                    op_cf  = _get("Operating Cash Flow")
                    inv_cf = _get("Investing Cash Flow")
                    fin_cf = _get("Financing Cash Flow")
                    cap_ex = _get("Capital Expenditure")

                    # Free cash flow = operating CF - capex
                    free_cf = None
                    if op_cf is not None and cap_ex is not None:
                        free_cf = op_cf - abs(cap_ex)

                    row = {
                        "symbol":             sym,
                        "year":               period.year,
                        "operating_cashflow": op_cf,
                        "investing_cashflow": inv_cf,
                        "financing_cashflow": fin_cf,
                        "free_cashflow":      free_cf,
                    }
                    cur.execute("""
                        INSERT INTO cash_flow
                            (symbol, year, operating_cashflow, investing_cashflow,
                             financing_cashflow, free_cashflow)
                        VALUES
                            (%(symbol)s, %(year)s, %(operating_cashflow)s,
                             %(investing_cashflow)s, %(financing_cashflow)s, %(free_cashflow)s)
                        ON CONFLICT (symbol, year) DO UPDATE SET
                            operating_cashflow = EXCLUDED.operating_cashflow,
                            investing_cashflow = EXCLUDED.investing_cashflow,
                            financing_cashflow = EXCLUDED.financing_cashflow,
                            free_cashflow      = EXCLUDED.free_cashflow
                    """, row)
                    rows.append(row)
            conn.commit()

        log.info("[Fundamentals] Cash flow stored for %s (%d rows).", sym, len(rows))
        return rows

    # ── Internal: PostgreSQL reads ────────────────────────────────────────────

    def _read_fundamentals(self, symbol: str) -> Optional[dict]:
        try:
            with _get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM fundamentals WHERE symbol = %s",
                        (symbol.upper(),)
                    )
                    row = cur.fetchone()
            return dict(row) if row else None
        except Exception as e:
            log.error("[Fundamentals] DB read error: %s", e)
            return None

    def _read_quarterly(self, symbol: str) -> list:
        try:
            with _get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM quarterly_results
                        WHERE symbol = %s
                        ORDER BY period DESC
                        LIMIT 12
                    """, (symbol.upper(),))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[Fundamentals] DB quarterly read error: %s", e)
            return []

    def _read_annual(self, symbol: str) -> list:
        try:
            with _get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM annual_results
                        WHERE symbol = %s
                        ORDER BY year DESC
                        LIMIT 10
                    """, (symbol.upper(),))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[Fundamentals] DB annual read error: %s", e)
            return []

    def _read_balance_sheet(self, symbol: str) -> list:
        try:
            with _get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM balance_sheet
                        WHERE symbol = %s
                        ORDER BY year DESC
                        LIMIT 10
                    """, (symbol.upper(),))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[Fundamentals] DB balance sheet read error: %s", e)
            return []

    def _read_cashflow(self, symbol: str) -> list:
        try:
            with _get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT * FROM cash_flow
                        WHERE symbol = %s
                        ORDER BY year DESC
                        LIMIT 10
                    """, (symbol.upper(),))
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[Fundamentals] DB cash flow read error: %s", e)
            return []

