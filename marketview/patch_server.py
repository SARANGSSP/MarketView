#!/usr/bin/env python3
"""
patch_server.py
Patches server.py to integrate portfolio.py routes and alert checking.
Run once: python3 patch_server.py
"""
import re

with open('/opt/marketview/server.py', 'r') as f:
    src = f.read()

# 1. Add import
old_import = "from token_refresh import attach_auth_routes"
new_import = """from token_refresh import attach_auth_routes
from portfolio import attach_portfolio_routes, check_alerts"""
src = src.replace(old_import, new_import, 1)

# 2. Add alert check call inside aggregator_loop after 'result = run_ta(symbol, candle)'
old_ta = "                if result is None:\n                    continue"
new_ta = """                if result is None:
                    continue

                # Check alerts
                try:
                    avg_vol = float(hist_data.get(symbol, pd.DataFrame()).get('volume', pd.Series([0])).rolling(20).mean().iloc[-1] or 0) if symbol in hist_data else 0
                    pct_change = round(((candle['close'] - candle['open']) / candle['open']) * 100, 2) if candle['open'] else 0
                    asyncio.create_task(check_alerts(symbol, candle['close'], pct_change, candle['volume'], avg_vol))
                except Exception as _ae:
                    pass"""
src = src.replace(old_ta, new_ta, 1)

# 3. Mount portfolio routes after attach_auth_routes
old_routes = "    attach_auth_routes(app)  # /auth/login, /auth/callback, /auth/status"
new_routes = """    attach_auth_routes(app)  # /auth/login, /auth/callback, /auth/status
    attach_portfolio_routes(app)  # Google OAuth, portfolio, alerts"""
src = src.replace(old_routes, new_routes, 1)

# 4. Add search route before health
old_health = '    app.router.add_get("/health",  lambda r: web.json_response({"status": "ok"}))'
new_health = """    app.router.add_get("/search",  handle_search)
    app.router.add_get("/health",  lambda r: web.json_response({"status": "ok"}))"""
src = src.replace(old_health, new_health, 1)

# 5. Add handle_search function before main()
search_fn = '''
async def handle_search(request):
    """GET /search?q=hdfc&limit=10"""
    q     = request.query.get("q", "").strip()
    limit = int(request.query.get("limit", "10"))
    if not q:
        return web.json_response([])
    results = dp.search(q, limit=limit)
    return web.json_response(results)

'''
# Insert before 'async def main():'
src = src.replace("async def main():", search_fn + "async def main():", 1)

with open('/opt/marketview/server.py', 'w') as f:
    f.write(src)

print("✅ server.py patched successfully")
