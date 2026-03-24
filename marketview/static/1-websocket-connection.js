// ════════════════════════════════════════════════════════════════════════
// STEP: "Add client-side WebSocket connection"
//
// Establishes the WebSocket used for live ticks, plus the config/state it
// needs. Load this file before 2-stream-live-prices.js and
// 3-reconnect-logic.js — connect() below hands off to handler functions
// (handleLiveMessage, handleSocketError, handleSocketClose) that those
// files define.
// ════════════════════════════════════════════════════════════════════════

// ── CONFIG (injected by server or set manually for local dev) ──────────────
// In production, server.py serves this file and can template these values.
// For local dev, they default to localhost.
const WS_URL    = window.MV_WS_URL   || "wss://stockmarketview.duckdns.org/ws";
const REST_BASE = window.MV_REST_URL || "http://localhost:8000";
const API_KEY   = window.MV_API_KEY  || "";   // set by server template in prod

// Helper: build authenticated fetch URL
function apiUrl(path) {
  const sep = path.includes("?") ? "&" : "?";
  return API_KEY ? `${REST_BASE}${path}${sep}api_key=${API_KEY}` : `${REST_BASE}${path}`;
}

let ws             = null;
let currentSym      = "RELIANCE";
let openPrice       = null;
let _sessionHigh    = null;  // dynamic day high for live updating
let _sessionLow     = null;  // dynamic day low for live updating

function setStatus(state) {
  const dot = document.querySelector(".live-dot");
  const text = document.querySelector(".nav-live-text");
  const badge = document.querySelector(".nav-badge");
  const liveBadge = document.querySelector(".stock-live-badge");
  const colors = { live: "#4caf50", stale: "#ff9800", connecting: "#ff9800", disconnected: "#9e9e9e", error: "#f44336" };
  if (dot) dot.style.background = colors[state] || colors.disconnected;
  if (text) text.textContent = { live: "Live Market Data", stale: "No ticks — market paused?", connecting: "Connecting to server…", error: "Server error", disconnected: "Disconnected – retrying…" }[state] || state;
  if (liveBadge) liveBadge.style.background = state === "live" ? "#e8f5e9" : "#fce4ec";
  if (badge) badge.textContent = state === "live" ? "NSE LIVE ●" : "NSE OFFLINE";
}

// ── WEBSOCKET CONNECT ──────────────────────────────────────────────────────
// connect(sym): load today's intraday candles via REST, then attach WS for live ticks
async function connect(symbol) {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  clearTimeout(reconnectTimer); // defined in 3-reconnect-logic.js

  currentSym          = symbol || currentSym;
  openPrice           = null;
  liveBar             = null;   // defined in 2-stream-live-prices.js
  _lastPrice          = null;   // defined in 2-stream-live-prices.js
  _sessionHigh        = null;
  _sessionLow         = null;
  window.currentRange = "1d";
  window.candleType   = "intraday";

  setStatus("connecting");

  // ── Load fundamentals in parallel (non-blocking) ──
  loadFundamentals(currentSym);

  // ── Step 1: load today's intraday history from REST ──
  try {
    const res  = await fetch(apiUrl(`/history?symbol=${currentSym}&range=1d`));
    const data = await res.json();

    if (!data.error && data.candles) {
      candles    = data.candles;
      bbUpperArr = data.bb_upper_arr || [];
      bbLowerArr = data.bb_lower_arr || [];
      bbMidArr   = data.bb_mid_arr   || [];
      rsiArr     = data.rsi_arr      || [];
      macdArr    = data.macd_arr     || [];
      signalArr  = data.signal_arr   || [];
      histArr    = data.hist_arr     || [];

      // Set openPrice = first candle's open (= day open price)
      if (candles.length > 0) openPrice = data.session_open ?? candles[0].o;

      // Update badges
      _applyBadges(data);
      buildAllCharts();
    }
  } catch (err) {
    console.warn("[connect] Intraday fetch failed (market may be closed):", err);
    // continue — WS snapshot will supply whatever data is available
  }

  // ── Step 2: open WebSocket for live 1-second ticks ──
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setStatus("live");
    // Send API key first if configured, then symbol
    if (API_KEY) ws.send(API_KEY);
    ws.send(currentSym);
    resetStaleTimer(); // defined in 3-reconnect-logic.js
  };

  // Message handling lives in 2-stream-live-prices.js
  ws.onmessage = handleLiveMessage;

  // Error/close handling (incl. reconnect) lives in 3-reconnect-logic.js
  ws.onerror = handleSocketError;
  ws.onclose = handleSocketClose;
}
