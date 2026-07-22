# MarketView

MarketView is a stock market analysis tool that lets you track live prices,
study a company's technicals, monitor your portfolio, and get alerted when
things move.

## Features

**Live market data**
- Real-time price streaming over WebSockets, with automatic reconnect on
  dropped connections
- Historical OHLCV data with range-based candle fetching
- Symbol search and autocomplete for instrument lookup

**Technical analysis**
- Candlestick pattern detection and support/resistance levels
- Price, candle, RSI, MACD, and volume charts (Chart.js)
- Per-stock sparklines for at-a-glance trend views

**Fundamentals**
- Company fundamentals data with a PostgreSQL-backed read layer
- Async fundamentals API

**Portfolio & watchlists**
- Add and track holdings, with a modal for quick entry
- User watchlists
- Google OAuth login

**Alerts**
- Price alerts with WhatsApp delivery
- Daily WhatsApp portfolio summaries on a schedule

**Performance**
- Two-tier caching (in-memory + SQLite) for instrument and OHLCV data
- Live bar broadcast to connected clients

**UI**
- Responsive dashboard layout with a dedicated mobile stylesheet

## Tech stack

- **Backend:** Python (server, data providers, caching, portfolio and
  fundamentals services)
- **Database:** PostgreSQL (schema, fundamentals, portfolio persistence),
  SQLite (local cache layer)
- **Frontend:** Vanilla JavaScript, Chart.js, WebSockets
- **Auth:** Google OAuth
- **Alerts:** WhatsApp integration

## Getting started

```bash
git clone https://github.com/SARANGSSP/MarketView.git
cd MarketView
pip install -r marketview/requirements.txt
```

Set up your environment variables (API keys, database credentials, OAuth
client details) before running the server. Then start the app:

```bash
python marketview/server.py
```

Open the dashboard in your browser and start tracking.

## Project structure

```
marketview/
  server.py               # Core server, WebSocket handling, routing
  data_provider.py         # Instrument lookup and OHLCV fetching
  fundamentals_provider.py # Company fundamentals API
  cache.py                 # Memory + SQLite caching layer
  portfolio.py              # Portfolio, watchlists, alerts, WhatsApp
  schema.sql                # PostgreSQL schema
  marketview.html            # Dashboard markup
  portfolio.html              # Portfolio page markup
  static/
    marketview.js              # Client-side app logic
    style.css                   # Base stylesheet
mobile-responsive.css           # Mobile layout overrides
```

## License

See [LICENSE](LICENSE) for details.
