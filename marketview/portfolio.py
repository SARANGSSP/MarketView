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

# ── GOOGLE OAUTH ──────────────────────────────────────────────────────────────
async def google_login(request: web.Request):
    params = urllib.parse.urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "online",
    })
    raise web.HTTPFound(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


async def google_callback(request: web.Request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="Missing code", status=400)

    # Exchange code for tokens
    async with aiohttp.ClientSession() as session:
        async with session.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }) as resp:
            token_data = await resp.json()

    if "error" in token_data:
        return web.Response(text=f"OAuth error: {token_data['error']}", status=400)

    # Get user info
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"}
        ) as resp:
            user_info = await resp.json()

    google_id = user_info.get("sub")
    email     = user_info.get("email", "")
    name      = user_info.get("name", "")
    picture   = user_info.get("picture", "")

    # Upsert user in DB
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO mv_users (google_id, email, name, picture)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (google_id) DO UPDATE
                    SET email=%s, name=%s, picture=%s
                RETURNING id, google_id, email, name, picture, whatsapp
            """, (google_id, email, name, picture, email, name, picture))
            user = dict(cur.fetchone())
        conn.commit()

    response = web.HTTPFound("/portfolio.html")
    _set_session(response, user)
    return response


async def google_logout(request: web.Request):
    token = request.cookies.get("mv_session")
    if token:
        _sessions.pop(token, None)
        _delete_session(token)
    response = web.HTTPFound("/")
    response.del_cookie("mv_session")
    return response


async def get_me(request: web.Request):
    user = _get_session(request)
    if not user:
        return web.json_response({"authenticated": False})
    return web.json_response({
        "authenticated": True,
        "id":      user["id"],
        "email":   user["email"],
        "name":    user["name"],
        "picture": user.get("picture"),
        "whatsapp": user.get("whatsapp"),
    })

# ── UPDATE WHATSAPP NUMBER ────────────────────────────────────────────────────
@require_auth
async def update_whatsapp(request: web.Request):
    user = request["user"]
    body = await request.json()
    number = body.get("whatsapp", "").strip()
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE mv_users SET whatsapp=%s WHERE id=%s", (number, user["id"]))
        conn.commit()
    # Update session
    for sess in _sessions.values():
        if sess.get("id") == user["id"]:
            sess["whatsapp"] = number
    return web.json_response({"ok": True})

# ── PORTFOLIO ENDPOINTS ───────────────────────────────────────────────────────
@require_auth
async def list_portfolio(request: web.Request):
    user = request["user"]
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, symbol, quantity, buy_price, notes, added_at
                FROM mv_portfolio WHERE user_id=%s ORDER BY added_at DESC
            """, (user["id"],))
            rows = [dict(r) for r in cur.fetchall()]
    # Convert timestamps
    for r in rows:
        r["added_at"] = r["added_at"].isoformat() if r["added_at"] else None
    return web.json_response(rows)


@require_auth
async def add_portfolio(request: web.Request):
    user = request["user"]
    body = await request.json()
    symbol    = body.get("symbol", "").upper().strip()
    quantity  = float(body.get("quantity", 0))
    buy_price = float(body.get("buy_price", 0))
    notes     = body.get("notes", "")

    if not symbol or quantity <= 0 or buy_price <= 0:
        return web.json_response({"error": "symbol, quantity, buy_price required"}, status=400)

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    INSERT INTO mv_portfolio (user_id, symbol, quantity, buy_price, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, symbol) DO UPDATE
                        SET buy_price = ((mv_portfolio.quantity * mv_portfolio.buy_price)
                                         + (excluded.quantity * excluded.buy_price))
                                        / (mv_portfolio.quantity + excluded.quantity),
                            quantity = mv_portfolio.quantity + excluded.quantity,
                            notes = excluded.notes
                    RETURNING id, symbol, quantity, buy_price, notes
                """, (user["id"], symbol, quantity, buy_price, notes))
                row = dict(cur.fetchone())
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        conn.commit()
    return web.json_response(row, status=201)


@require_auth
async def update_portfolio(request: web.Request):
    user = request["user"]
    pid  = int(request.match_info["id"])
    body = await request.json()

    fields = []
    vals   = []
    if "quantity" in body:
        fields.append("quantity=%s"); vals.append(float(body["quantity"]))
    if "buy_price" in body:
        fields.append("buy_price=%s"); vals.append(float(body["buy_price"]))
    if "notes" in body:
        fields.append("notes=%s"); vals.append(body["notes"])
    if not fields:
        return web.json_response({"error": "Nothing to update"}, status=400)

    vals += [pid, user["id"]]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE mv_portfolio SET {','.join(fields)} WHERE id=%s AND user_id=%s", vals)
        conn.commit()
    return web.json_response({"ok": True})


@require_auth
async def delete_portfolio(request: web.Request):
    user = request["user"]
    pid  = int(request.match_info["id"])
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mv_portfolio WHERE id=%s AND user_id=%s", (pid, user["id"]))
        conn.commit()
    return web.json_response({"ok": True})

# ── ALERT ENDPOINTS ───────────────────────────────────────────────────────────
@require_auth
async def list_alerts(request: web.Request):
    user = request["user"]
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, symbol, condition, target, active, triggered_at, created_at
                FROM mv_alerts WHERE user_id=%s ORDER BY created_at DESC
            """, (user["id"],))
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["triggered_at"] = r["triggered_at"].isoformat() if r["triggered_at"] else None
        r["created_at"]   = r["created_at"].isoformat()   if r["created_at"]   else None
    return web.json_response(rows)


@require_auth
async def create_alert(request: web.Request):
    user = request["user"]
    body = await request.json()
    symbol    = body.get("symbol", "").upper().strip()
    condition = body.get("condition", "")  # above | below | pct_change | volume_spike
    target    = float(body.get("target", 0))

    if not symbol or condition not in ("above", "below", "pct_change", "volume_spike"):
        return web.json_response({"error": "symbol + valid condition required"}, status=400)

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO mv_alerts (user_id, symbol, condition, target)
                VALUES (%s, %s, %s, %s) RETURNING id, symbol, condition, target, active
            """, (user["id"], symbol, condition, target))
            row = dict(cur.fetchone())
        conn.commit()
    return web.json_response(row, status=201)


@require_auth
async def delete_alert(request: web.Request):
    user = request["user"]
    aid  = int(request.match_info["id"])
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mv_alerts WHERE id=%s AND user_id=%s", (aid, user["id"]))
        conn.commit()
    return web.json_response({"ok": True})


@require_auth
async def toggle_alert(request: web.Request):
    user = request["user"]
    aid  = int(request.match_info["id"])
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE mv_alerts SET active = NOT active
                WHERE id=%s AND user_id=%s RETURNING active
            """, (aid, user["id"]))
            row = cur.fetchone()
        conn.commit()
    return web.json_response({"active": row["active"] if row else False})

# ── WHATSAPP SENDER ───────────────────────────────────────────────────────────
async def send_whatsapp(to: str, message: str):
    """Send WhatsApp message via Twilio sandbox."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        log.warning("[Alert] Twilio not configured — skipping WhatsApp")
        return
    if not to.startswith("whatsapp:"):
        to = f"whatsapp:{to}"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    async with aiohttp.ClientSession() as session:
        async with session.post(url,
            data={"From": TWILIO_FROM, "To": to, "Body": message},
            auth=aiohttp.BasicAuth(TWILIO_SID, TWILIO_TOKEN)
        ) as resp:
            result = await resp.json()
            if resp.status >= 400:
                log.error("[Alert] Twilio error: %s", result)
            else:
                log.info("[Alert] WhatsApp sent to %s", to)

# ── ALERT CHECKER (called on every tick from server.py) ──────────────────────
async def check_alerts(symbol: str, ltp: float, pct_change: float, volume: int, avg_volume: float):
    """
    Check all active alerts for this symbol and trigger if conditions met.
    Called from the aggregator loop on every new candle.
    """
    try:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.id, a.condition, a.target, a.symbol,
                           u.whatsapp, u.email, u.name
                    FROM mv_alerts a
                    JOIN mv_users u ON u.id = a.user_id
                    WHERE a.symbol=%s AND a.active=TRUE AND a.triggered_at IS NULL
                """, (symbol,))
                alerts = cur.fetchall()

            triggered_ids = []
            for alert in alerts:
                cond   = alert["condition"]
                target = alert["target"]
                hit    = False

                if cond == "above" and ltp >= target:
                    hit = True
                    msg = f"🚨 *MarketView Alert*\n{symbol} crossed ₹{target:.2f} UP\nCurrent: ₹{ltp:.2f}"
                elif cond == "below" and ltp <= target:
                    hit = True
                    msg = f"🚨 *MarketView Alert*\n{symbol} dropped below ₹{target:.2f}\nCurrent: ₹{ltp:.2f}"
                elif cond == "pct_change" and abs(pct_change) >= target:
                    hit = True
                    direction = "▲" if pct_change > 0 else "▼"
                    msg = f"🚨 *MarketView Alert*\n{symbol} moved {direction}{abs(pct_change):.2f}%\nTarget: ±{target}% | LTP: ₹{ltp:.2f}"
                elif cond == "volume_spike" and avg_volume > 0 and volume >= avg_volume * (target or 1.5):
                    hit = True
                    msg = f"🚨 *MarketView Alert*\n{symbol} volume spike!\nVol: {volume:,} ({volume/avg_volume:.1f}x avg) | LTP: ₹{ltp:.2f}"

                if hit:
                    triggered_ids.append(alert["id"])
                    if alert.get("whatsapp"):
                        asyncio.create_task(send_whatsapp(alert["whatsapp"], msg))

            if triggered_ids:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE mv_alerts SET triggered_at=NOW(), active=FALSE
                        WHERE id = ANY(%s)
                    """, (triggered_ids,))
                conn.commit()

    except Exception as e:
        log.error("[AlertChecker] %s: %s", symbol, e)

# ── DAILY SUMMARY HELPERS ────────────────────────────────────────────────────

async def _get_all_whatsapp_users() -> list:
    try:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, whatsapp, name FROM mv_users
                    WHERE whatsapp IS NOT NULL AND whatsapp <> ''
                """)
                return cur.fetchall()
    except Exception as e:
        log.error("[Summary] Failed to fetch users: %s", e)
        return []

async def send_market_open_summary(user_id: int, whatsapp: str, name: str):
    """09:15 IST: portfolio overview WhatsApp message."""
    try:
        from datetime import datetime
        import zoneinfo
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST)
        date_str = now.strftime("%d %b %Y | %H:%M IST")
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT symbol, quantity, buy_price FROM mv_portfolio WHERE user_id = %s ORDER BY symbol", (user_id,))
                holdings = cur.fetchall()
        if not holdings:
            return
        total = sum(h['quantity'] * h['buy_price'] for h in holdings)
        sep = '-' * 32
        rows = [
            '{:<10} {:>5} {:>10} {:>10}'.format('Symbol', 'Qty', 'Buy Price', 'Invested'),
            sep,
        ]
        for h in holdings:
            invested = h['quantity'] * h['buy_price']
            rows.append('{:<10} {:>5} {:>10} {:>10}'.format(
                h['symbol'][:10],
                int(h['quantity']),
                'Rs.' + format(h['buy_price'], ',.0f'),
                'Rs.' + format(invested, ',.0f')
            ))
        rows.append(sep)
        rows.append('{:<16} {:>16}'.format('Total Invested', 'Rs.' + format(total, ',.0f')))
        table = '```' + chr(10) + chr(10).join(rows) + chr(10) + '```'
        parts = [
            '*MarketView Portfolio Summary*',
            date_str,
            '',
            table,
            '',
            'Login: https://stockmarketview.duckdns.org',
        ]
        await send_whatsapp(whatsapp, chr(10).join(parts))
    except Exception as e:
        log.error('[MarketOpen] user %s: %s', user_id, e)

async def send_daily_pnl_summary(user_id: int, whatsapp: str, name: str):
    """15:30 IST: end-of-day P&L WhatsApp message."""
    try:
        from datetime import datetime
        import zoneinfo
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST)
        date_str = now.strftime("%d %b %Y | %H:%M IST")
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT symbol, quantity, buy_price FROM mv_portfolio WHERE user_id = %s ORDER BY symbol", (user_id,))
                holdings = cur.fetchall()
        if not holdings:
            return
        import aiohttp as _aiohttp
        ltp_map = {}
        try:
            async with _aiohttp.ClientSession() as session:
                syms = [h['symbol'] for h in holdings]
                async with session.get('http://localhost:8000/api/ltp?symbols=' + ','.join(syms)) as r:
                    if r.status == 200:
                        ltp_map = await r.json()
        except Exception as e:
            log.warning('[PnL] LTP fetch failed: %s', e)
        total_invested = total_current = 0.0
        sep = '-' * 36
        rows = [
            '{:<10} {:>8} {:>7} {:>9}'.format('Symbol', 'LTP', 'Chg%', 'P&L'),
            sep,
        ]
        for h in holdings:
            sym = h['symbol']; qty = h['quantity']; avg = h['buy_price']
            ltp = ltp_map.get(sym, avg)
            pnl = (ltp - avg) * qty
            pct = ((ltp - avg) / avg * 100) if avg else 0
            total_invested += qty * avg
            total_current  += qty * ltp
            pnl_str = ('+' if pnl >= 0 else '') + 'Rs.' + format(abs(pnl), ',.0f')
            pct_str = ('+' if pct >= 0 else '') + format(pct, '.1f') + '%'
            rows.append('{:<10} {:>8} {:>7} {:>9}'.format(
                sym[:10],
                'Rs.' + format(ltp, ',.0f'),
                pct_str,
                pnl_str
            ))
        total_pnl = total_current - total_invested
        total_pct = (total_pnl / total_invested * 100) if total_invested else 0
        rows.append(sep)
        rows.append('{:<18} {:>16}'.format('Portfolio Value', 'Rs.' + format(total_current, ',.0f')))
        rows.append('{:<18} {:>16}'.format('Total P&L', ('+' if total_pnl >= 0 else '') + 'Rs.' + format(abs(total_pnl), ',.0f')))
        rows.append('{:<18} {:>16}'.format('Day Return', ('+' if total_pct >= 0 else '') + format(total_pct, '.2f') + '%'))
        table = '```' + chr(10) + chr(10).join(rows) + chr(10) + '```'
        parts = [
            '*MarketView End-of-Day P&L Report*',
            date_str,
            '',
            table,
            '',
            'Login: https://stockmarketview.duckdns.org',
        ]
        await send_whatsapp(whatsapp, chr(10).join(parts))
    except Exception as e:
        log.error('[DailyPnL] user %s: %s', user_id, e)

async def broadcast_summary(fn):
    """Call fn(user_id, whatsapp, name) for every user with a WhatsApp number."""
    users = await _get_all_whatsapp_users()
    for u in users:
        asyncio.create_task(fn(u["id"], u["whatsapp"], u["name"]))

# ── ROUTE REGISTRATION ────────────────────────────────────────────────────────

@require_auth
async def get_watchlist(request: web.Request):
    user = request["user"]
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM mv_watchlist WHERE user_id=%s ORDER BY added_at", (user["id"],))
            symbols = [r[0] for r in cur.fetchall()]
    return web.json_response(symbols)

@require_auth
async def add_watchlist(request: web.Request):
    user = request["user"]
    body = await request.json()
    symbol = body.get("symbol", "").strip().upper()
    if not symbol:
        return web.json_response({"error": "symbol required"}, status=400)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO mv_watchlist (user_id, symbol) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user["id"], symbol))
        conn.commit()
    return web.json_response({"ok": True})

@require_auth
async def remove_watchlist(request: web.Request):
    user = request["user"]
    symbol = request.match_info["symbol"].upper()
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mv_watchlist WHERE user_id=%s AND symbol=%s", (user["id"], symbol))
        conn.commit()
    return web.json_response({"ok": True})

def attach_portfolio_routes(app: web.Application):
    """Call from server.py to mount all portfolio/auth routes."""
    try:
        init_db()
    except Exception as e:
        log.error("[Portfolio] DB init failed: %s", e)

    # Google OAuth
    app.router.add_get("/auth/google/login",    google_login)
    app.router.add_get("/auth/google/callback", google_callback)
    app.router.add_get("/auth/google/logout",   google_logout)
    app.router.add_get("/auth/google/me",       get_me)

    # WhatsApp
    app.router.add_post("/user/whatsapp",       update_whatsapp)

    # Portfolio CRUD
    app.router.add_get   ("/portfolio",           list_portfolio)
    app.router.add_post  ("/portfolio",           add_portfolio)
    app.router.add_put   ("/portfolio/{id}",      update_portfolio)
    app.router.add_delete("/portfolio/{id}",      delete_portfolio)

    # Alerts CRUD
    app.router.add_get   ("/watchlist",            get_watchlist)
    app.router.add_post  ("/watchlist",            add_watchlist)
    app.router.add_delete("/watchlist/{symbol}",   remove_watchlist)
    app.router.add_get   ("/alerts",              list_alerts)
    app.router.add_post  ("/alerts",              create_alert)
    app.router.add_delete("/alerts/{id}",         delete_alert)
    app.router.add_put   ("/alerts/{id}/toggle",  toggle_alert)

    # Serve portfolio page
    app.router.add_get("/portfolio.html", lambda r: web.FileResponse("./portfolio.html"))

    log.info("[Portfolio] Routes mounted.")
