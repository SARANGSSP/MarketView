-- ── schema.sql  –  MarketView PostgreSQL schema ────────────────────────────
-- Run once to set up the database:
--   psql -U marketview -d marketview -f schema.sql

-- ── Key Ratios & Company Info ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol          TEXT        PRIMARY KEY,
    fetched_at      TIMESTAMP   NOT NULL,
    market_cap      BIGINT,
    pe_ratio        FLOAT,
    pb_ratio        FLOAT,
    roe             FLOAT,
    roce            FLOAT,
    debt_to_equity  FLOAT,
    profit_margin   FLOAT,
    dividend_yield  FLOAT,
    eps             FLOAT,
    face_value      FLOAT,
    sector          TEXT,
    industry        TEXT,
    website         TEXT,
    description     TEXT
);

-- ── Quarterly P&L ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quarterly_results (
    symbol              TEXT    NOT NULL,
    period              DATE    NOT NULL,
    sales               BIGINT,
    expenses            BIGINT,
    operating_profit    BIGINT,
    net_profit          BIGINT,
    eps                 FLOAT,
    PRIMARY KEY (symbol, period)
);

-- ── Annual P&L ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS annual_results (
    symbol              TEXT    NOT NULL,
    year                INT     NOT NULL,
    sales               BIGINT,
    expenses            BIGINT,
    operating_profit    BIGINT,
    net_profit          BIGINT,
    eps                 FLOAT,
    PRIMARY KEY (symbol, year)
);

-- ── Balance Sheet ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS balance_sheet (
    symbol              TEXT    NOT NULL,
    year                INT     NOT NULL,
    total_assets        BIGINT,
    total_liabilities   BIGINT,
    total_equity        BIGINT,
    borrowings          BIGINT,
    reserves            BIGINT,
    PRIMARY KEY (symbol, year)
);

-- ── Cash Flow ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cash_flow (
    symbol              TEXT    NOT NULL,
    year                INT     NOT NULL,
    operating_cashflow  BIGINT,
    investing_cashflow  BIGINT,
    financing_cashflow  BIGINT,
    free_cashflow       BIGINT,
    PRIMARY KEY (symbol, year)
);

-- ── Shareholding Pattern ──────────────────────────────────────────────────────
-- Note: yfinance gives approximate holder data for Indian stocks.
-- For precise promoter/FII/DII breakdown you would need a paid API.
CREATE TABLE IF NOT EXISTS shareholding (
    symbol      TEXT    NOT NULL,
    period      DATE    NOT NULL,
    promoter    FLOAT,
    fii         FLOAT,
    dii         FLOAT,
    public_     FLOAT,
    PRIMARY KEY (symbol, period)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_quarterly_symbol  ON quarterly_results(symbol);
CREATE INDEX IF NOT EXISTS idx_annual_symbol     ON annual_results(symbol);
CREATE INDEX IF NOT EXISTS idx_bs_symbol         ON balance_sheet(symbol);
CREATE INDEX IF NOT EXISTS idx_cf_symbol         ON cash_flow(symbol);
CREATE INDEX IF NOT EXISTS idx_sh_symbol         ON shareholding(symbol);
