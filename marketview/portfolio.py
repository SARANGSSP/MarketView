"""
portfolio.py – MarketView Portfolio, Alerts & Google OAuth
===========================================================
Provides:
  GET  /auth/google/login      → redirect to Google
  GET  /auth/google/callback   → exchange code, set session
  GET  /auth/google/logout     → clear session
  GET  /auth/google/me         → current user info

  GET  /portfolio              → list user's holdings
  POST /portfolio              → add holding
  PUT  /portfolio/{id}         → update holding
  DELETE /portfolio/{id}       → remove holding

  GET  /alerts                 → list user's alerts
  POST /alerts                 → create alert
  DELETE /alerts/{id}          → remove alert
  PUT  /alerts/{id}/toggle     → enable/disable alert
"""

import os
import json
import time
import hashlib
import hmac
import logging
import asyncio
import urllib.parse
import psycopg2
import psycopg2.extras
import aiohttp
from aiohttp import web

log = logging.getLogger("portfolio")

# ── CONFIG ────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI",
                       "https://stockmarketview.duckdns.org/auth/google/callback")
SESSION_SECRET       = os.environ.get("SESSION_SECRET", hashlib.sha256(os.urandom(32)).hexdigest())
DB_URL               = os.environ.get("DATABASE_URL", "")

TWILIO_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # sandbox number

# In-memory session store: token → {user_id, email, name, picture, expires}
_sessions: dict[str, dict] = {}

def _load_sessions_from_db():
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT token, data FROM mv_sessions WHERE expires > %s", (time.time(),))
                for row in cur.fetchall():
                    import json
                    _sessions[row[0]] = json.loads(row[1])
        print("[Sessions] Loaded from DB.")
    except Exception as e:
        print(f"[Sessions] Could not load (table may not exist yet): {e}")

def _persist_session(token: str, user: dict):
    try:
        import json
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mv_sessions (token, data, expires) VALUES (%s, %s, %s) ON CONFLICT (token) DO UPDATE SET data=EXCLUDED.data, expires=EXCLUDED.expires",
                    (token, json.dumps(user), user["expires"])
                )
            conn.commit()
    except Exception as e:
        print(f"[Sessions] Could not persist: {e}")

def _delete_session(token: str):
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mv_sessions WHERE token=%s", (token,))
            conn.commit()
    except Exception as e:
        print(f"[Sessions] Could not delete: {e}")

# Alert checker callback reference (set by server.py)
_alert_checker = None

# ── DB ────────────────────────────────────────────────────────────────────────
def _get_conn():
    return psycopg2.connect(DB_URL)

def init_db():
    """Create portfolio/alerts/users tables if they don't exist."""
    sql = """
    CREATE TABLE IF NOT EXISTS mv_users (
        id          SERIAL PRIMARY KEY,
        google_id   TEXT UNIQUE NOT NULL,
        email       TEXT NOT NULL,
        name        TEXT,
        picture     TEXT,
        whatsapp    TEXT,
        created_at  TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS mv_portfolio (
        id          SERIAL PRIMARY KEY,
        user_id     INT REFERENCES mv_users(id) ON DELETE CASCADE,
        symbol      TEXT NOT NULL,
        quantity    FLOAT NOT NULL DEFAULT 0,
        buy_price   FLOAT NOT NULL DEFAULT 0,
        notes       TEXT,
        added_at    TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, symbol)
    );

    CREATE TABLE IF NOT EXISTS mv_alerts (
        id           SERIAL PRIMARY KEY,
        user_id      INT REFERENCES mv_users(id) ON DELETE CASCADE,
        symbol       TEXT NOT NULL,
        condition    TEXT NOT NULL,  -- 'above', 'below', 'pct_change', 'volume_spike'
        target       FLOAT,          -- price target or % for pct_change
        active       BOOLEAN DEFAULT TRUE,
        triggered_at TIMESTAMP,
        created_at   TIMESTAMP DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_mv_portfolio_user ON mv_portfolio(user_id);
    CREATE INDEX IF NOT EXISTS idx_mv_alerts_user    ON mv_alerts(user_id);
    CREATE INDEX IF NOT EXISTS idx_mv_alerts_active  ON mv_alerts(active) WHERE active = TRUE;
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("[Portfolio] DB tables ready.")
    _load_sessions_from_db()

# ── SESSION HELPERS ───────────────────────────────────────────────────────────
def _make_token(user_id: int) -> str:
    raw = f"{user_id}:{time.time()}:{os.urandom(16).hex()}"
    return hashlib.sha256(raw.encode()).hexdigest()

def _set_session(response: web.Response, user: dict) -> str:
    token = _make_token(user["id"])
    sess = {**user, "expires": time.time() + 86400 * 30}  # 30 days
    _sessions[token] = sess
    _persist_session(token, sess)
    response.set_cookie("mv_session", token, max_age=86400*30, httponly=True, samesite="Lax", secure=True)
    return token

def _get_session(request: web.Request) -> dict | None:
    token = request.cookies.get("mv_session")
    if not token:
        return None
    sess = _sessions.get(token)
    if not sess or sess["expires"] < time.time():
        _sessions.pop(token, None)
        return None
    return sess

def require_auth(handler):
    """Decorator: returns 401 if not logged in."""
    async def wrapper(request):
        user = _get_session(request)
        if not user:
            return web.json_response({"error": "Not authenticated"}, status=401)
        request["user"] = user
        return await handler(request)
    return wrapper

