// ── STATE ──────────────────────────────────────────────────────────────────
let candles = [], rsiArr = [], macdArr = [], signalArr = [], histArr = [];
let displayCount = null; // null = show all; set per range for 1w/1m
let bbUpperArr = [], bbLowerArr = [], bbMidArr = [];
let priceChart, candleChart, rsiChart, macdChart, volChart;

// Live bar: the currently-forming candle (updated every 250ms, not committed to candles[])
let liveBar = null;

// ── Live data tracking ──────────────────────────────────────────────────────
let _lastPrice = null;          // previous price for flash direction
let _tickCount = 0;             // ticks received this minute
let _tickCounterReset = null;   // interval to reset ticks/min counter
let _ticksThisWindow = 0;       // ticks in current 60s window
let _lastTickTimestamp = null;  // for "Updated Xs ago" display
let _tickAgeTimer = null;       // interval to update "Updated Xs ago"
let _lastBid = null;
let _lastAsk = null;

// Watchlist: array of symbol strings, persisted in localStorage
let watchlist = JSON.parse(localStorage.getItem("mv_watchlist") || '["RELIANCE","TCS","INFY","HDFCBANK"]');
// Price cache for watchlist items: { SYMBOL: { price, change } }
let wlPriceCache = JSON.parse(localStorage.getItem("mv_price_cache") || "{}");
// Alerts: [{ id, symbol, price, direction:"above"|"below", triggered:false }]
let alerts = JSON.parse(localStorage.getItem("mv_alerts") || "[]");

// ── HELPERS ────────────────────────────────────────────────────────────────
// Track whether current candles are intraday ticks or daily EOD candles
window.candleType = "daily";   // "daily" | "intraday"

function timeLabel(ts) {
  const d = new Date(ts);
  if (window.candleType === "intraday") {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  // Daily candles: show date
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "2-digit" });
}

function fmt(n) {
  if (n == null) return "—";
  return "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2 });
}

// ── Live data display helpers ───────────────────────────────────────────────

// Flash the price element green (up) or red (down) on each tick
function flashPrice(direction) {
  const el = document.getElementById("stockPrice");
  if (!el) return;
  el.classList.remove("price-flash-up", "price-flash-down");
  void el.offsetWidth; // force reflow to restart animation
  el.classList.add(direction === "up" ? "price-flash-up" : "price-flash-down");
  setTimeout(() => el.classList.remove("price-flash-up", "price-flash-down"), 600);
}

// Flash a watchlist row when its price updates
function flashWatchlistItem(sym, direction) {
  const items = document.querySelectorAll(".wl-item");
  items.forEach(item => {
    if (item.querySelector(".wl-sym")?.textContent === sym) {
      item.classList.remove("wl-flash-up", "wl-flash-down");
      void item.offsetWidth;
      item.classList.add(direction === "up" ? "wl-flash-up" : "wl-flash-down");
      setTimeout(() => item.classList.remove("wl-flash-up", "wl-flash-down"), 800);
    }
  });
}

// Update the "Updated Xs ago" display
function _startTickAgeDisplay() {
  if (_tickAgeTimer) clearInterval(_tickAgeTimer);
  _tickAgeTimer = setInterval(() => {
    if (_lastTickTimestamp == null) return;
    const el = document.getElementById("tickAge");
    if (!el) return;
    const secs = Math.floor((Date.now() - _lastTickTimestamp) / 1000);
    if (secs < 5)       el.textContent = "Updated just now";
    else if (secs < 60) el.textContent = `Updated ${secs}s ago`;
    else                el.textContent = `Updated ${Math.floor(secs/60)}m ago`;
  }, 1000);
}

// Update tick rate counter (ticks/min)
function _recordTick() {
  _ticksThisWindow++;
  if (!_tickCounterReset) {
    _tickCounterReset = setInterval(() => {
      const el = document.getElementById("tickRate");
      if (el) el.textContent = _ticksThisWindow + "/min";
      _ticksThisWindow = 0;
    }, 60000);
  }
  // Flicker the activity indicator
  const dot = document.getElementById("activityDot");
  if (dot) {
    dot.classList.add("active-flicker");
    setTimeout(() => dot.classList.remove("active-flicker"), 200);
  }
}

// Update bid/ask display
function _updateBidAsk(bid, ask) {
  if (bid == null && ask == null) return;
  _lastBid = bid;
  _lastAsk = ask;
  const bidEl = document.getElementById("liveBid");
  const askEl = document.getElementById("liveAsk");
  const spreadEl = document.getElementById("liveSpread");
  if (bidEl && bid != null)  bidEl.textContent = fmt(bid);
  if (askEl && ask != null)  askEl.textContent = fmt(ask);
  if (spreadEl && bid != null && ask != null) {
    const spread = Math.abs(ask - bid);
    spreadEl.textContent = "₹" + spread.toFixed(2);
  }
}

// ── CHART DEFAULTS ─────────────────────────────────────────────────────────
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 10;
const gridColor = "rgba(46,56,80,0.6)";
const tickColor = "#4d5e7a";
const baseScales = () => ({
  x: {
    grid: { color: gridColor, drawBorder: false },
    ticks: { color: tickColor, maxRotation: 45, minRotation: 0, maxTicksLimit: 10 },
    border: { display: false },
  },
  y: { grid: { color: gridColor, drawBorder: false }, ticks: { color: tickColor }, border: { display: false } },
});

function buildAllCharts() {
  buildPriceChart();
  buildCandleChart();
  buildRSIChart();
  buildMACDChart();
  buildVolChart();
}

// ── PRICE CHART (line + Bollinger Bands) ───────────────────────────────────
function buildPriceChart() {
  if (priceChart) priceChart.destroy();
  const dc = displayCount;
  const dispCandles = dc ? candles.slice(-dc) : candles;
  const dispBbU = dc ? bbUpperArr.slice(-dc) : bbUpperArr;
  const dispBbM = dc ? bbMidArr.slice(-dc)   : bbMidArr;
  const dispBbL = dc ? bbLowerArr.slice(-dc) : bbLowerArr;
  const labels = dispCandles.map(c => timeLabel(c.t));
  const closes = dispCandles.map(c => c.c);
  const ctx = document.getElementById("priceChart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 110);
  grad.addColorStop(0, "rgba(59,130,246,0.20)");
  grad.addColorStop(1, "rgba(59,130,246,0.00)");

  priceChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Price", data: closes,
          borderColor: "#3b82f6", borderWidth: 1.5,
          fill: true, backgroundColor: grad,
          tension: 0.3, pointRadius: 0, pointHoverRadius: 3,
          order: 1,
        },
        {
          label: "BB Upper", data: dispBbU,
          borderColor: "rgba(239,68,68,0.45)", borderWidth: 1,
          borderDash: [4, 3], fill: false,
          pointRadius: 0, tension: 0.3, order: 2,
        },
        {
          label: "BB Mid", data: dispBbM,
          borderColor: "rgba(245,158,11,0.45)", borderWidth: 1,
          borderDash: [4, 3], fill: false,
          pointRadius: 0, tension: 0.3, order: 3,
        },
        {
          label: "BB Lower", data: dispBbL,
          borderColor: "rgba(34,197,94,0.45)", borderWidth: 1,
          borderDash: [4, 3], fill: false,
          pointRadius: 0, tension: 0.3, order: 4,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: {
          display: true,
          labels: { color: tickColor, boxWidth: 20, font: { size: 9 } },
        },
        tooltip: {
          mode: "index", intersect: false,
          callbacks: {
            label: c => c.dataset.label + ": " + fmt(c.parsed.y),
          },
        },
      },
      scales: baseScales(),
    },
  });
}

// ── CANDLESTICK CHART (stacked-bar, patched in-place for performance) ───────
function buildCandleChart() {
  if (candleChart) candleChart.destroy();
  const slice = candles.slice(-40);
  const ctx = document.getElementById("candleChart").getContext("2d");
  candleChart = new Chart(ctx, {
    type: "bar",
    data: _candleData(slice, liveBar),
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: item => item.datasetIndex === 2,
          callbacks: {
            label: ctx => {
              const full = liveBar ? [...slice, liveBar] : slice;
              const i = ctx.dataIndex, c = full[i];
              const suffix = (liveBar && i === full.length - 1) ? " ● forming" : "";
              return [`O: ${fmt(c.o)}`, `H: ${fmt(c.h)}`, `L: ${fmt(c.l)}`, `C: ${fmt(c.c)}${suffix}`];
            },
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { color: tickColor, maxRotation: 0, maxTicksLimit: 8 }, border: { display: false } },
        y: {
          stacked: true,
          beginAtZero: false,
          grid: { color: gridColor, drawBorder: false },
          ticks: { color: tickColor },
          border: { display: false }
        },
      },
    },
  });
}

// Compute all 4 stacked-bar datasets for a candle slice
// If liveBar is provided it is appended as the last bar with a pulsing amber style
function _candleData(slice, lb) {
  const full = lb ? [...slice, lb] : slice;
  const labels = full.map((c, i) => (lb && i === full.length - 1) ? "● LIVE" : timeLabel(c.t));
  const wickBtm    = full.map(c => c.l);
  const wickToBody = full.map(c => Math.min(c.o, c.c) - c.l);
  const bodyTop    = full.map(c => Math.abs(c.c - c.o) || 0.5);
  const bodyToWick = full.map(c => c.h - Math.max(c.o, c.c));
  const isUp = full.map(c => c.c >= c.o);
  const upColor = "#4caf50", dnColor = "#ef5350";
  // Live bar gets amber colours to distinguish it from committed candles
  const liveUp = "rgba(255,193,7,0.95)", liveDn = "rgba(255,152,0,0.95)";
  const liveWick = "rgba(255,193,7,0.7)";
  const bodyColors = full.map((c, i) => {
    if (lb && i === full.length - 1) return c.c >= c.o ? liveUp : liveDn;
    return c.c >= c.o ? upColor : dnColor;
  });
  const wickColors = full.map((c, i) => {
    if (lb && i === full.length - 1) return liveWick;
    return c.c >= c.o ? "rgba(76,175,80,0.6)" : "rgba(239,83,80,0.6)";
  });
  return {
    labels,
    datasets: [
      { label: "Wick Bottom", data: wickBtm,    backgroundColor: "transparent", stack: "s", borderWidth: 0, barPercentage: 0.1 },
      { label: "Wick Lower",  data: wickToBody, backgroundColor: wickColors,    stack: "s", borderWidth: 0, barPercentage: 0.1 },
      { label: "Body",        data: bodyTop,    backgroundColor: bodyColors,    stack: "s", borderWidth: 0.5, borderColor: bodyColors, borderSkipped: false, barPercentage: 0.55 },
      { label: "Wick Upper",  data: bodyToWick, backgroundColor: wickColors,    stack: "s", borderWidth: 0, barPercentage: 0.1 },
    ],
  };
}

// Patch candlestick chart in-place instead of destroying and rebuilding
function patchCandleChart(lb) {
  if (!candleChart) { buildCandleChart(); return; }
  const slice = candles.slice(-40);
  const nd = _candleData(slice, lb);
  candleChart.data.labels = nd.labels;
  for (let i = 0; i < 4; i++) {
    candleChart.data.datasets[i].data = nd.datasets[i].data;
    if (nd.datasets[i].backgroundColor !== "transparent") {
      candleChart.data.datasets[i].backgroundColor = nd.datasets[i].backgroundColor;
    }
    if (i === 2) candleChart.data.datasets[i].borderColor = nd.datasets[i].borderColor;
  }
  candleChart.update("none");
}

// ── RSI CHART ──────────────────────────────────────────────────────────────
function buildRSIChart() {
  if (rsiChart) rsiChart.destroy();
  const rsiSlice = displayCount ? Math.min(displayCount, 60) : 60;
  const labels = candles.slice(-rsiSlice).map(c => timeLabel(c.t));
  const data = rsiArr.slice(-rsiSlice);
  const ctx = document.getElementById("rsiChart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 80);
  grad.addColorStop(0, "rgba(167,139,250,0.25)");
  grad.addColorStop(1, "rgba(167,139,250,0.02)");
  rsiChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { data, borderColor: "#a78bfa", borderWidth: 1.5, fill: true, backgroundColor: grad, tension: 0.4, pointRadius: 0 },
        { data: labels.map(() => 70), borderColor: "rgba(239,68,68,0.35)", borderWidth: 1, borderDash: [4, 3], pointRadius: 0, fill: false },
        { data: labels.map(() => 30), borderColor: "rgba(34,197,94,0.35)", borderWidth: 1, borderDash: [4, 3], pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => "RSI: " + c.parsed.y.toFixed(2) } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: tickColor, maxRotation: 0, maxTicksLimit: 6 }, border: { display: false } },
        y: { min: 0, max: 100, grid: { color: gridColor, drawBorder: false }, ticks: { color: tickColor, stepSize: 25 }, border: { display: false } },
      },
    },
  });
}

// ── MACD CHART ─────────────────────────────────────────────────────────────
function buildMACDChart() {
  if (macdChart) macdChart.destroy();
  const s = displayCount ? Math.min(displayCount, 60) : 60;
  const labels = candles.slice(-s).map(c => timeLabel(c.t));
  const mLine = macdArr.slice(-s);
  const sLine = signalArr.slice(-s);
  const hBars = histArr.slice(-s);
  const ctx = document.getElementById("macdChart").getContext("2d");
  macdChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        { type: "bar",  label: "Histogram", data: hBars,  backgroundColor: hBars.map(v => v >= 0 ? "rgba(59,130,246,0.55)" : "rgba(239,68,68,0.55)"), borderWidth: 0, barPercentage: 0.7 },
        { type: "line", label: "MACD",      data: mLine,  borderColor: "#3b82f6", borderWidth: 1.5, pointRadius: 0, tension: 0.3 },
        { type: "line", label: "Signal",    data: sLine,  borderColor: "#ef4444", borderWidth: 1.5, pointRadius: 0, tension: 0.3, borderDash: [3, 2] },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: tickColor, maxRotation: 0, maxTicksLimit: 6 }, border: { display: false } },
        y: { grid: { color: gridColor, drawBorder: false }, ticks: { color: tickColor }, border: { display: false } },
      },
    },
  });
}

// ── VOLUME CHART ───────────────────────────────────────────────────────────
function buildVolChart() {
  if (volChart) volChart.destroy();
  const dispVol = displayCount ? candles.slice(-displayCount) : candles;
  const labels = dispVol.map(c => timeLabel(c.t));
  const data = dispVol.map(c => c.v);
  const ctx = document.getElementById("volChart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 100);
  grad.addColorStop(0, "rgba(59,130,246,0.25)");
  grad.addColorStop(1, "rgba(59,130,246,0.02)");
  volChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{ data, borderColor: "#3b82f6", borderWidth: 1.5, fill: true, backgroundColor: grad, tension: 0.4, pointRadius: 0 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => "Vol: " + (c.parsed.y / 1e6).toFixed(2) + "M" } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: tickColor, maxRotation: 0, maxTicksLimit: 10 }, border: { display: false } },
        y: { grid: { color: gridColor, drawBorder: false }, ticks: { color: tickColor, callback: v => (v / 1e6).toFixed(1) + "M" }, border: { display: false } },
      },
    },
  });
}

// ── TIME RANGE BUTTONS ─────────────────────────────────────────────────────
async function setRange(btn, range) {
  document.querySelectorAll(".time-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  window.currentRange = range;

  if (range === "1d") {
    displayCount = null;
    connect(currentSym);
    return;
  }

  // Historical ranges: disconnect WS, fetch from REST only
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  clearTimeout(reconnectTimer);
  window.candleType = "daily";
  liveBar = null;
  if (range === "1w")      displayCount = 7;
  else if (range === "1m") displayCount = 31;
  else                     displayCount = null;

  try {
    const res  = await fetch(apiUrl(`/history?symbol=${currentSym}&range=${range}`));
    const data = await res.json();
    if (data.error) { console.error("History error:", data.error); return; }

    candles    = data.candles    || [];
    bbUpperArr = data.bb_upper_arr || [];
    bbLowerArr = data.bb_lower_arr || [];
    bbMidArr   = data.bb_mid_arr   || [];
    rsiArr     = data.rsi_arr      || [];
    macdArr    = data.macd_arr     || [];
    signalArr  = data.signal_arr   || [];
    histArr    = data.hist_arr     || [];

    _applyBadges(data);
    buildAllCharts();
  } catch (err) {
    console.error("History fetch failed:", err);
  }
}
// ── CONFIG (injected by server or set manually for local dev) ──────────────
// In production, server.py serves this file and can template these values.
// For local dev, they default to localhost.
const WS_URL   = window.MV_WS_URL   || "wss://stockmarketview.duckdns.org/ws";
const REST_BASE = window.MV_REST_URL || "http://localhost:8000";
const API_KEY  = window.MV_API_KEY  || "";   // set by server template in prod

// Helper: build authenticated fetch URL
function apiUrl(path) {
  const sep = path.includes("?") ? "&" : "?";
  return API_KEY ? `${REST_BASE}${path}${sep}api_key=${API_KEY}` : `${REST_BASE}${path}`;
}
let ws = null;
let reconnectTimer = null;
let currentSym = "RELIANCE";
let openPrice = null;
let lastTickAt = null;   // epoch ms of last message — used for stale detection
let staleTimer = null;
let _sessionHigh = null;  // dynamic day high for live updating
let _sessionLow  = null;  // dynamic day low for live updating

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

function resetStaleTimer() {
  clearTimeout(staleTimer);
  staleTimer = setTimeout(() => setStatus("stale"), 30000);   // 30 s with no tick → stale
}

// ── PRICE ALERTS ───────────────────────────────────────────────────────────
function saveAlerts() { localStorage.setItem("mv_alerts", JSON.stringify(alerts)); }
async function loadAlertsFromServer() {
  try {
    const res = await fetch(apiUrl("/alerts"), { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data)) {
      alerts = data.map(a => ({ id: a.id, symbol: a.symbol, price: parseFloat(a.target_price), direction: a.direction, triggered: a.triggered || false }));
      saveAlerts();
      renderAlerts();
      updateAlertCount();
    }
  } catch(e) {}
}

async function addAlert() {
  const sym = document.getElementById("alertSym").value.trim().toUpperCase();
  const price = parseFloat(document.getElementById("alertPrice").value);
  const dir = document.getElementById("alertDir").value;
  if (!sym || isNaN(price)) return;
  try {
    const res = await fetch(apiUrl("/alerts"), {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: sym, condition: dir, target: price })
    });
    if (res.ok) {
      await loadAlertsFromServer();
      document.getElementById("alertSym").value = "";
      document.getElementById("alertPrice").value = "";
      return;
    }
  } catch(e) {}
  alerts.push({ id: Date.now(), symbol: sym, price, direction: dir, triggered: false });
  saveAlerts();
  renderAlerts();
  document.getElementById("alertSym").value = "";
  document.getElementById("alertPrice").value = "";
  updateAlertCount();
}

async function removeAlert(id) {
  try {
    await fetch(apiUrl(`/alerts/${id}`), { method: "DELETE", credentials: "include" });
    await loadAlertsFromServer();
    return;
  } catch(e) {}
  alerts = alerts.filter(a => a.id !== id);
  saveAlerts();
  renderAlerts();
  updateAlertCount();
}

function playAlertSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(660, ctx.currentTime + 0.15);
    osc.frequency.setValueAtTime(880, ctx.currentTime + 0.3);
    gain.gain.setValueAtTime(0.5, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.5);
  } catch(e) {}
}
function checkAlerts(symbol, price) {
  alerts.forEach(a => {
    if (a.triggered || a.symbol !== symbol) return;
    const hit = (a.direction === "above" && price >= a.price)
      || (a.direction === "below" && price <= a.price);
    if (hit) {
      a.triggered = true;
      saveAlerts();
      showToast(`🔔 ${symbol} ${a.direction === "above" ? "crossed above" : "fell below"} ${fmt(a.price)}  —  now ${fmt(price)}`, "alert");
      playAlertSound();
      playAlertSound();
      renderAlerts();
      updateAlertCount();
    }
  });
}

function updateAlertCount() {
  const active = alerts.filter(a => !a.triggered).length;
  const el = document.getElementById("alertCount");
  if (el) { el.textContent = active; el.style.display = active ? "inline" : "none"; }
}

function renderAlerts() {
  const list = document.getElementById("alertList");
  if (!list) return;
  list.innerHTML = "";
  if (!alerts.length) { list.innerHTML = '<div class="alert-empty">No alerts set.</div>'; return; }
  alerts.slice().reverse().forEach(a => {
    const div = document.createElement("div");
    div.className = "alert-item" + (a.triggered ? " triggered" : "");
    div.innerHTML = `
      <span class="alert-sym">${a.symbol}</span>
      <span class="alert-cond">${a.direction === "above" ? "↑ above" : "↓ below"} ${fmt(a.price)}</span>
      <span class="alert-status">${a.triggered ? "✓ Triggered" : "⏳ Active"}</span>
      <button class="alert-del" onclick="removeAlert(${a.id})">✕</button>`;
    list.appendChild(div);
  });
}

function toggleAlerts() {
  const panel = document.getElementById("alertsPanel");
  if (!panel) return;
  panel.style.display = panel.style.display === "none" ? "flex" : "none";
  if (panel.style.display === "flex") renderAlerts();
}

// ── WATCHLIST ──────────────────────────────────────────────────────────────
function saveWatchlist() { localStorage.setItem("mv_watchlist", JSON.stringify(watchlist)); }
async function loadWatchlistFromServer() {
  try {
    const res = await fetch(apiUrl("/watchlist"), { credentials: "include" });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data) && data.length) {
      watchlist = data;
      saveWatchlist();
      renderWatchlist();
      setTimeout(refreshWatchlistPrices, 500);
    }
  } catch(e) {}
}
function savePriceCache() { localStorage.setItem("mv_price_cache", JSON.stringify(wlPriceCache)); }

function addToWatchlist() {
  const sym = document.getElementById("wlInput").value.trim().toUpperCase();
  if (!sym || watchlist.includes(sym)) return;
  watchlist.push(sym);
  saveWatchlist();
  fetch(apiUrl("/watchlist"), { method: "POST", credentials: "include", headers: {"Content-Type":"application/json"}, body: JSON.stringify({symbol: sym}) }).catch(()=>{});
  renderWatchlist();
  document.getElementById("wlInput").value = "";
  // Fetch a quick price for the newly added symbol
  fetchWatchlistPrice(sym);
}

// Fetch latest price for a single watchlist symbol via the history REST endpoint
async function fetchWatchlistPrice(sym) {
  try {
    const res = await fetch(apiUrl(`/history?symbol=${sym}&range=1w`));
    const data = await res.json();
    if (data.candles && data.candles.length >= 2) {
      const last  = data.candles[data.candles.length - 1];
      const prev  = data.candles[data.candles.length - 2];
      // Change = today's close vs yesterday's close (prev day EOD → today EOD)
      const chg = prev.c > 0 ? ((last.c - prev.c) / prev.c) * 100 : 0;
      wlPriceCache[sym] = { price: last.c, chg, open: last.o };
      savePriceCache();
      renderWatchlist();
    }
  } catch (_) { /* silently ignore — will update when symbol is selected */ }
}

// On load, refresh prices for all watchlist symbols
async function refreshWatchlistPrices() {
  for (const sym of watchlist) {
    await fetchWatchlistPrice(sym);
  }
}

function removeFromWatchlist(sym) {
  watchlist = watchlist.filter(s => s !== sym);
  saveWatchlist();
  fetch(apiUrl(`/watchlist/${sym}`), { method: "DELETE", credentials: "include" }).catch(()=>{});
  renderWatchlist();
}

function renderWatchlist() {
  const container = document.getElementById("wlItems");
  if (!container) return;
  container.innerHTML = "";
  watchlist.forEach(sym => {
    const cached = wlPriceCache[sym] || {};
    const price = cached.price != null ? fmt(cached.price) : "—";
    const chg = cached.chg != null ? cached.chg : null;
    const chgStr = chg != null ? (chg >= 0 ? `+${chg.toFixed(2)}%` : `${chg.toFixed(2)}%`) : "";
    const chgCls = chg != null ? (chg >= 0 ? "up" : "dn") : "";
    const active = sym === currentSym ? " wl-active" : "";
    const div = document.createElement("div");
    div.className = "wl-item" + active;
    div.innerHTML = `
      <div class="wl-item-info" onclick="switchSymbol('${sym}')">
        <span class="wl-sym">${sym}</span>
        <span class="wl-price">${price}</span>
        ${chgStr ? `<span class="wl-chg ${chgCls}">${chgStr}</span>` : ""}
      </div>
      <button class="wl-del" onclick="removeFromWatchlist('${sym}')">✕</button>`;
    container.appendChild(div);
  });
}

function switchSymbol(sym) {
  candles = []; rsiArr = []; macdArr = []; signalArr = []; histArr = [];
  bbUpperArr = []; bbLowerArr = []; bbMidArr = [];
  openPrice = null;
  document.getElementById("stockSymbol").textContent = sym;
  connect(sym);
}

// ── TOAST NOTIFICATIONS ────────────────────────────────────────────────────
function showToast(msg, type = "info") {
  const container = document.getElementById("toastContainer");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "toast toast-" + type;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => toast.classList.add("toast-show"), 10);
  setTimeout(() => {
    toast.classList.remove("toast-show");
    setTimeout(() => toast.remove(), 400);
  }, 4000);
}

// ── APPLY LIVE BAR (250ms preview of forming candle) ──────────────────────
function applyLiveBar(d) {
  // Only show live bar in intraday mode
  if (window.candleType !== "intraday") return;

  liveBar = { t: d.time, o: d.open, h: d.high, l: d.low, c: d.close, v: d.volume };

  // Show the live bar badge
  const badge = document.getElementById("liveBarBadge");
  if (badge) badge.style.display = "inline-flex";

  // ── Flash + update price ──
  const newPrice = d.ltp;
  if (_lastPrice !== null && newPrice !== _lastPrice) {
    flashPrice(newPrice > _lastPrice ? "up" : "down");
  }
  _lastPrice = newPrice;
  _lastTickTimestamp = Date.now();
  _recordTick();

  document.getElementById("stockPrice").textContent = fmt(d.ltp);
  if (openPrice !== null) {
    const chg = d.ltp - openPrice;
    const pct = (chg / openPrice) * 100;
    const chgEl = document.getElementById("stockChange");
    chgEl.className = "stock-change " + (chg >= 0 ? "pos" : "neg");
    chgEl.textContent = (chg >= 0 ? "▲ +" : "▼ -") + "₹" + Math.abs(chg).toFixed(2)
      + " (" + (chg >= 0 ? "+" : "") + pct.toFixed(2) + "%)";
  }

  // ── Bid / Ask ──
  _updateBidAsk(d.bid, d.ask);

  // ── Dynamic Day High / Low update ──
  if (d.ltp != null) {
    const highEl = document.getElementById("msHigh");
    const lowEl  = document.getElementById("msLow");
    if (highEl && _sessionHigh !== null && d.ltp > _sessionHigh) {
      _sessionHigh = d.ltp;
      highEl.textContent = fmt(_sessionHigh);
      highEl.classList.add("val-updated"); setTimeout(() => highEl.classList.remove("val-updated"), 800);
    }
    if (lowEl && _sessionLow !== null && d.ltp < _sessionLow) {
      _sessionLow = d.ltp;
      lowEl.textContent = fmt(_sessionLow);
      lowEl.classList.add("val-updated"); setTimeout(() => lowEl.classList.remove("val-updated"), 800);
    }
  }

  // ── Live tick chart ──
  _pushLiveTick(d.ltp, d.symbol);

  // Patch price line chart: append live close as last point
  if (priceChart) {
    const allLabels = [...candles.map(c => timeLabel(c.t)), "● LIVE"];
    const allCloses = [...candles.map(c => c.c), d.ltp];
    priceChart.data.labels = allLabels;
    priceChart.data.datasets[0].data = allCloses;
    // Highlight the live endpoint with a visible amber dot
    priceChart.data.datasets[0].pointRadius = allCloses.map((_, i) => i === allCloses.length - 1 ? 5 : 0);
    priceChart.data.datasets[0].pointBackgroundColor = allCloses.map((_, i) =>
      i === allCloses.length - 1 ? "#ffc107" : "#1565c0"
    );
    priceChart.update("none");
  }

  // Patch candlestick chart with amber live bar
  patchCandleChart(liveBar);
}

// ── APPLY ONE SERVER MESSAGE ───────────────────────────────────────────────
function applyUpdate(d) {
  if (d.error) { console.warn("[WS] Server error:", d.error); return; }

  lastTickAt = Date.now();
  _lastTickTimestamp = Date.now();
  _recordTick();
  resetStaleTimer();

  if (d.is_stale) setStatus("stale");
  else setStatus("live");

  const t = d.time;

  // ── Price header ──
  document.getElementById("stockSymbol").textContent = d.symbol;
  document.getElementById("stockName").textContent = d.company_name;

  // Flash price on change
  const newPrice = d.close;
  if (_lastPrice !== null && newPrice !== _lastPrice) {
    flashPrice(newPrice > _lastPrice ? "up" : "down");
  }
  _lastPrice = newPrice;
  document.getElementById("stockPrice").textContent = fmt(d.close);

  // Use session_open from server if available, otherwise fall back to d.open
  if (openPrice === null) openPrice = d.session_open != null ? d.session_open : d.open;
  const chg = d.close - openPrice;
  const pct = (chg / openPrice) * 100;
  const chgEl = document.getElementById("stockChange");
  chgEl.className = "stock-change " + (chg >= 0 ? "pos" : "neg");
  chgEl.textContent = (chg >= 0 ? "▲ +" : "▼ -") + "₹" + Math.abs(chg).toFixed(2)
    + " (" + (chg >= 0 ? "+" : "") + pct.toFixed(2) + "%)";

  // ── Bid / Ask ──
  _updateBidAsk(d.bid, d.ask);

  // ── Composite signal near price ──
  const sigBadgeEl = document.getElementById("priceSignalBadge");
  if (sigBadgeEl && d.composite_signal) {
    sigBadgeEl.textContent = d.composite_signal;
    const conf = d.composite_confidence != null ? " · " + (d.composite_confidence * 100).toFixed(0) + "%" : "";
    sigBadgeEl.textContent = d.composite_signal + conf;
    sigBadgeEl.className = "price-signal-badge " + (d.composite_signal === "Buy" ? "sig-bullish" : d.composite_signal === "Sell" ? "sig-bearish" : "sig-neutral");
    sigBadgeEl.style.display = "inline-block";
  }

  // ── Market stats ──
  document.getElementById("msOpen").textContent = fmt(d.open);

  // Dynamic session high/low
  if (_sessionHigh === null || d.high > _sessionHigh) _sessionHigh = d.high;
  if (_sessionLow  === null || d.low  < _sessionLow)  _sessionLow  = d.low;
  // Also check live close price
  if (d.close > _sessionHigh) _sessionHigh = d.close;
  if (d.close < _sessionLow)  _sessionLow  = d.close;

  document.getElementById("msHigh").textContent = fmt(_sessionHigh ?? d.high);
  document.getElementById("msLow").textContent  = fmt(_sessionLow  ?? d.low);
  document.getElementById("msVol").textContent = (d.volume / 1e6).toFixed(2) + "M";

  // S/R
  const msSupport = document.getElementById("msSupport");
  const msResistance = document.getElementById("msResistance");
  if (msSupport) msSupport.textContent = d.support != null ? fmt(d.support) : "—";
  if (msResistance) msResistance.textContent = d.resistance != null ? fmt(d.resistance) : "—";

  // ── Prev close, VWAP, 52W, Adj Price ──
  if (d.prev_close     != null) { const el = document.getElementById("msPrevClose"); if (el) el.textContent = fmt(d.prev_close); }
  if (d.vwap           != null) { const el = document.getElementById("msVwap");      if (el) el.textContent = fmt(d.vwap); }
  if (d.w52_high       != null) { const el = document.getElementById("ms52High");    if (el) el.textContent = fmt(d.w52_high); }
  if (d.w52_low        != null) { const el = document.getElementById("ms52Low");     if (el) el.textContent = fmt(d.w52_low); }
  if (d.adjusted_price != null) { const el = document.getElementById("msAdjPrice");  if (el) el.textContent = fmt(d.adjusted_price); }

  // ── RSI ──
  document.getElementById("rsiVal").textContent = d.rsi.toFixed(2);
  const rsiSigEl = document.getElementById("rsiSig");
  rsiSigEl.textContent = d.rsi_signal;
  rsiSigEl.className = "ind-signal " + (d.rsi_signal === "Overbought" ? "sig-bearish" : d.rsi_signal === "Oversold" ? "sig-bullish" : "sig-neutral");

  // ── MACD ──
  document.getElementById("macdVal").textContent = d.macd.toFixed(2);
  document.getElementById("macdVal").style.color = d.macd >= 0 ? "#22c55e" : "#ef4444";
  const macdSigEl = document.getElementById("macdSig");
  macdSigEl.textContent = d.macd_signal;
  macdSigEl.className = "ind-signal " + (d.macd_signal === "Bullish" ? "sig-bullish" : "sig-bearish");

  // ── Volume ──
  document.getElementById("volAvg").textContent = "Volume Signal: " + d.volume_signal;

  // ── ML signal ──
  const mlEl = document.getElementById("mlSignal");
  if (mlEl) {
    mlEl.textContent = d.composite_signal + " (" + (d.composite_confidence * 100).toFixed(0) + "% confidence)";
    mlEl.className = "ind-signal " + (d.composite_signal === "Buy" ? "sig-bullish" : d.composite_signal === "Sell" ? "sig-bearish" : "sig-neutral");
  }

  // ── Pattern counts ──
  const bullEl = document.getElementById("bullCount");
  const bearEl = document.getElementById("bearCount");
  const neutEl = document.getElementById("neutCount");
  if (bullEl) bullEl.textContent = d.bull_count ?? "—";
  if (bearEl) bearEl.textContent = d.bear_count ?? "—";
  if (neutEl) neutEl.textContent = d.neut_count ?? "—";

  // ── Latest pattern badge ──
  const patternNameEl = document.querySelector(".pattern-name");
  const patternSignalEl = document.querySelector(".pattern-signal-badge");
  if (patternNameEl && d.latest_pattern && d.latest_pattern !== "None") {
    const isBull = d.latest_signal === "Bullish";
    const isBear = d.latest_signal === "Bearish";
    patternNameEl.textContent = (isBull ? "🟢" : isBear ? "🔴" : "⚪") + " " + d.latest_pattern;
    if (patternSignalEl) {
      patternSignalEl.textContent = d.latest_signal;
      patternSignalEl.style.background = isBull ? "#e8f5e9" : isBear ? "#fce4ec" : "#f0f2f7";
      patternSignalEl.style.color = isBull ? "#2e7d32" : isBear ? "#c62828" : "#5a6a85";
    }
  }

  // ── Watchlist price cache update ──
  if (openPrice !== null) {
    // For active symbol: compute change vs yesterday's close (from session_open)
    const sessionOpen = d.session_open != null ? d.session_open : openPrice;
    const dayChg = sessionOpen > 0 ? ((d.close - sessionOpen) / sessionOpen) * 100 : pct;
    const prevCached = wlPriceCache[d.symbol];
    const direction = prevCached && prevCached.price != null
      ? (d.close > prevCached.price ? "up" : d.close < prevCached.price ? "down" : null)
      : null;
    wlPriceCache[d.symbol] = { price: d.close, chg: dayChg };
    savePriceCache();
    renderWatchlist();
    if (direction) flashWatchlistItem(d.symbol, direction);
  }

  // ── Price alerts ──
  checkAlerts(d.symbol, d.close);

  // ── Chart arrays ──
  const newC = { t, o: d.open, h: d.high, l: d.low, c: d.close, v: d.volume };

  if (d.snapshot) {
    // WS snapshot arrives after connect() has already loaded intraday history.
    // Only use snapshot data to fill charts if we have NO candles yet (market closed).
    if (openPrice === null) openPrice = d.session_open ?? d.close;

    if (candles.length === 0) {
      // No intraday data loaded — fall back to snapshot's daily history
      window.candleType = d.candle_type || "daily";
      candles    = (d.history && d.history.length) ? d.history : [newC];
      rsiArr     = d.rsi_arr      || [d.rsi];
      macdArr    = d.macd_arr     || [d.macd];
      signalArr  = d.signal_arr   || [d.macd - d.macd_histogram];
      histArr    = d.hist_arr     || [d.macd_histogram];
      bbUpperArr = d.bb_upper_arr || [d.bb_upper];
      bbLowerArr = d.bb_lower_arr || [d.bb_lower];
      bbMidArr   = d.bb_mid_arr   || [d.bb_mid];
      buildAllCharts();
    }
    return;  // never wipe candles that were already loaded from intraday REST
  }

  // ── Live tick — append to existing intraday candles ──
  if (window.candleType === "daily") {
    // Edge case: should not happen, but if somehow still daily just flip
    window.candleType = "intraday";
  }

  candles = [...candles.slice(-149), newC];
  liveBar = null;   // committed — clear the forming bar
  const badge = document.getElementById("liveBarBadge");
  if (badge) badge.style.display = "none";
  rsiArr = [...rsiArr.slice(-149), d.rsi];
  macdArr = [...macdArr.slice(-149), d.macd];
  histArr = [...histArr.slice(-149), d.macd_histogram];
  signalArr = [...signalArr.slice(-149), d.macd - d.macd_histogram];
  bbUpperArr = [...bbUpperArr.slice(-149), d.bb_upper];
  bbLowerArr = [...bbLowerArr.slice(-149), d.bb_lower];
  bbMidArr = [...bbMidArr.slice(-149), d.bb_mid];

  // ── Patch charts in-place ──
  if (priceChart) {
    priceChart.data.labels = candles.map(c => timeLabel(c.t));
    priceChart.data.datasets[0].data = candles.map(c => c.c);
    priceChart.data.datasets[1].data = bbUpperArr;
    priceChart.data.datasets[2].data = bbMidArr;
    priceChart.data.datasets[3].data = bbLowerArr;
    priceChart.update("none");
  }
  if (rsiChart) {
    const lab = candles.slice(-60).map(c => timeLabel(c.t));
    rsiChart.data.labels = lab;
    rsiChart.data.datasets[0].data = rsiArr.slice(-60);
    rsiChart.data.datasets[1].data = lab.map(() => 70);
    rsiChart.data.datasets[2].data = lab.map(() => 30);
    rsiChart.update("none");
  }
  if (macdChart) {
    const s = 60;
    macdChart.data.labels = candles.slice(-s).map(c => timeLabel(c.t));
    macdChart.data.datasets[0].data = histArr.slice(-s);
    macdChart.data.datasets[0].backgroundColor = histArr.slice(-s).map(v => v >= 0 ? "rgba(59,130,246,0.55)" : "rgba(239,68,68,0.55)");
    macdChart.data.datasets[1].data = macdArr.slice(-s);
    macdChart.data.datasets[2].data = signalArr.slice(-s);
    macdChart.update("none");
  }
  if (volChart) {
    volChart.data.labels = candles.map(c => timeLabel(c.t));
    volChart.data.datasets[0].data = candles.map(c => c.v);
    volChart.update("none");
  }

  // Candlestick: patch instead of rebuild
  patchCandleChart();
}

// ── WEBSOCKET CONNECT ──────────────────────────────────────────────────────
// connect(sym): load today's intraday candles via REST, then attach WS for live ticks
async function connect(symbol) {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  clearTimeout(reconnectTimer);

  currentSym          = symbol || currentSym;
  openPrice           = null;
  liveBar             = null;
  _lastPrice          = null;
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
    resetStaleTimer();
  };

  ws.onmessage = event => {
    try {
      const d = JSON.parse(event.data);
      if (d.live_bar) { applyLiveBar(d); }
      else            { applyUpdate(d); }
    }
    catch (e) { console.error("[WS] Parse error:", e); }
  };

  ws.onerror = () => {
    setStatus("error");
    _hideLiveTickCard();
    console.error("[WS] Connection error. Is server.py running?");
  };

  ws.onclose = () => {
    setStatus("disconnected");
    _hideLiveTickCard();
    if (window.currentRange === "1d") {
      reconnectTimer = setTimeout(() => connect(currentSym), 5000);
    }
  };
}

// Helper: apply badge values from any REST or WS payload
function _applyBadges(d) {
  if (d.rsi != null) {
    document.getElementById("rsiVal").textContent = Number(d.rsi).toFixed(2);
    const el = document.getElementById("rsiSig");
    if (el) { el.textContent = d.rsi_signal; el.className = "ind-signal " + (d.rsi_signal === "Overbought" ? "sig-bearish" : d.rsi_signal === "Oversold" ? "sig-bullish" : "sig-neutral"); }
  }
  if (d.macd != null) {
    const mv = document.getElementById("macdVal");
    if (mv) { mv.textContent = Number(d.macd).toFixed(2); mv.style.color = d.macd >= 0 ? "#22c55e" : "#ef4444"; }
    const el = document.getElementById("macdSig");
    if (el) { el.textContent = d.macd_signal; el.className = "ind-signal " + (d.macd_signal === "Bullish" ? "sig-bullish" : "sig-bearish"); }
  }
  if (d.support    != null) { const el = document.getElementById("msSupport");    if (el) el.textContent = fmt(d.support); }
  if (d.resistance != null) { const el = document.getElementById("msResistance"); if (el) el.textContent = fmt(d.resistance); }
  if (d.volume_signal) { const el = document.getElementById("volAvg"); if (el) el.textContent = "Volume Signal: " + d.volume_signal; }
  const p = d.patterns || d;
  const bc = document.getElementById("bullCount"); if (bc) bc.textContent = p.bull_count ?? d.bull_count ?? "—";
  const br = document.getElementById("bearCount"); if (br) br.textContent = p.bear_count ?? d.bear_count ?? "—";
  const nc = document.getElementById("neutCount"); if (nc) nc.textContent = p.neut_count ?? d.neut_count ?? "—";
}

// ── SEARCH & AUTOCOMPLETE ──────────────────────────────────────────────────
let _searchDebounce = null;
let _searchActive   = false;   // is the dropdown open?
let _searchResults  = [];      // current result set
let _searchFocus    = -1;      // keyboard-focused row index

function _getDropdown() {
  let el = document.getElementById("searchDropdown");
  if (!el) {
    el = document.createElement("div");
    el.id = "searchDropdown";
    el.className = "search-dropdown";
    // Insert right after the search-box div
    document.body.appendChild(el);
  }
  return el;
}

function _closeDropdown() {
  _searchActive = false;
  _searchFocus  = -1;
  const el = document.getElementById("searchDropdown");
  if (el) el.style.display = "none";
}

function _renderDropdownFor(results, inputEl, onPick) {
  const dd = _getDropdown();
  _searchResults = results;
  _searchFocus = -1;
  if (!results.length) { _closeDropdown(); return; }
  dd.innerHTML = results.map((r, i) => `<div class="sd-item" data-idx="${i}" data-sym="${r.symbol}"><span class="sd-sym">${r.symbol}</span><span class="sd-name">${r.name}</span></div>`).join("");
  dd.querySelectorAll(".sd-item").forEach(item => {
    item.addEventListener("mousedown", e => {
      e.preventDefault();
      onPick(_searchResults[parseInt(item.dataset.idx)].symbol);
    });
  });
  const rect = inputEl.getBoundingClientRect();
  dd.style.left = rect.left + "px";
  dd.style.top = (rect.bottom + 4) + "px";
  dd.style.width = rect.width + "px";
  dd.style.display = "block";
  _searchActive = true;
}
function _renderDropdown(results) {
  const dd = _getDropdown();
  _searchResults = results;
  _searchFocus   = -1;

  if (!results.length) { _closeDropdown(); return; }

  dd.innerHTML = results.map((r, i) => `
    <div class="sd-item" data-idx="${i}" data-sym="${r.symbol}">
      <span class="sd-sym">${r.symbol}</span>
      <span class="sd-name">${r.name}</span>
    </div>
  `).join("");

  // Click to select
  dd.querySelectorAll(".sd-item").forEach(item => {
    item.addEventListener("mousedown", e => {
      e.preventDefault();   // prevent blur firing first
      _pickResult(parseInt(item.dataset.idx));
    });
  });

  const inp = document.getElementById("searchInput");
  const rect = inp.getBoundingClientRect();
  dd.style.left   = rect.left + "px";
  dd.style.top    = (rect.bottom + 4) + "px";
  dd.style.width  = rect.width + "px";
  dd.style.display = "block";
  _searchActive = true;
}

function _highlightRow(idx) {
  const items = document.querySelectorAll(".sd-item");
  items.forEach((el, i) => el.classList.toggle("sd-focused", i === idx));
}

function _pickResult(idx) {
  const r = _searchResults[idx];
  if (!r) return;
  const input = document.getElementById("searchInput");
  input.value = "";
  _closeDropdown();
  handleSearch(r.symbol);
}

async function _fetchSuggestionsFor(q, inputEl, onPick) {
  if (q.length < 1) { _closeDropdown(); return; }
  try {
    const url = apiUrl(`/search?q=${encodeURIComponent(q)}&limit=10`);
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    _renderDropdownFor(data, inputEl, onPick);
  } catch (e) {}
}
async function _fetchSuggestions(q) {
  if (q.length < 1) { _closeDropdown(); return; }
  try {
    const url = apiUrl(`/search?q=${encodeURIComponent(q)}&limit=10`);
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    _renderDropdown(data);
  } catch (e) {
    // silently ignore network errors during autocomplete
  }
}

function handleSearch(val) {
  // val may be a symbol (from autocomplete pick) or raw user input
  // If it looks like a company name (contains space or matches no known pattern),
  // the server already resolved it via the search endpoint — we always get a symbol here.
  const sym = val.trim().toUpperCase();
  if (!sym) return;
  candles = []; rsiArr = []; macdArr = []; signalArr = []; histArr = [];
  bbUpperArr = []; bbLowerArr = []; bbMidArr = [];
  openPrice = null;
  document.getElementById("stockSymbol").textContent = sym;
  connect(sym);
}

// Wire up the search input
(function _initSearch() {
  const input = document.getElementById("searchInput");
  if (!input) return;

  input.setAttribute("autocomplete", "off");
  // Remove the static datalist so our dropdown takes over
  input.removeAttribute("list");

  input.addEventListener("input", function () {
    const q = this.value.trim();
    clearTimeout(_searchDebounce);
    if (!q) { _closeDropdown(); return; }
    _searchDebounce = setTimeout(() => _fetchSuggestions(q), 150);
  });

  input.addEventListener("keydown", function (e) {
    const items = _searchResults;
    if (_searchActive && items.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        _searchFocus = Math.min(_searchFocus + 1, items.length - 1);
        _highlightRow(_searchFocus);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        _searchFocus = Math.max(_searchFocus - 1, 0);
        _highlightRow(_searchFocus);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (_searchFocus >= 0) {
          _pickResult(_searchFocus);
        } else if (items.length) {
          _pickResult(0);   // auto-pick top result on Enter
        }
        this.value = "";
        return;
      }
      if (e.key === "Escape") {
        _closeDropdown();
        return;
      }
    } else if (e.key === "Enter") {
      // No dropdown — treat raw input as a symbol (legacy behaviour)
      handleSearch(this.value);
      this.value = "";
    }
  });

  input.addEventListener("blur", () => {
    // Small delay so mousedown on a result fires first
    setTimeout(_closeDropdown, 150);
  });
})();

// Wire up alert symbol input
(function _initAlertSearch() {
  const input = document.getElementById("alertSym");
  if (!input) return;
  input.setAttribute("autocomplete", "off");
  input.removeAttribute("list");
  input.addEventListener("input", function () {
    const q = this.value.trim();
    clearTimeout(_searchDebounce);
    if (!q) { _closeDropdown(); return; }
    _searchDebounce = setTimeout(() => _fetchSuggestionsFor(q, input, (sym) => { input.value = sym; _closeDropdown(); }), 150);
  });
  input.addEventListener("blur", () => setTimeout(_closeDropdown, 150));
})();

// ── BOOT ───────────────────────────────────────────────────────────────────
window.addEventListener("load", () => {
  candles = []; rsiArr = []; macdArr = []; signalArr = []; histArr = [];
  bbUpperArr = []; bbLowerArr = []; bbMidArr = [];
  // Set Day button active by default
  const dayBtn = document.querySelector(".time-btn");
  if (dayBtn) dayBtn.classList.add("active");
  buildAllCharts();
  renderWatchlist();
  updateAlertCount();
  loadAlertsFromServer();
  _startTickAgeDisplay();
  connect("RELIANCE");
  // Refresh cached prices for watchlist symbols after a short delay
  setTimeout(refreshWatchlistPrices, 3000);
  // Check session
  fetch(apiUrl("/auth/google/me"), { credentials: "include" })
    .then(r => r.ok ? r.json() : null)
    .then(user => {
      if (user && user.name) {
        loadWatchlistFromServer();
        const btn = document.getElementById("navSignInBtn");
        if (btn) {
          btn.textContent = user.name.split(" ")[0];
          btn.href = "/portfolio.html";
          btn.title = "Go to portfolio";
          btn.style.textDecoration = "none";
        }
        // Add separate sign out button
        const signOutBtn = document.createElement("a");
        signOutBtn.href = "/auth/google/logout";
        signOutBtn.textContent = "Sign out";
        signOutBtn.style.cssText = "font-size:11px;color:var(--text3);text-decoration:none;padding:4px 8px;border:1px solid var(--border2);border-radius:7px;font-family:var(--mono);cursor:pointer;";
        signOutBtn.onmouseover = () => signOutBtn.style.color = "var(--red)";
        signOutBtn.onmouseout  = () => signOutBtn.style.color = "var(--text3)";
        if (btn.parentNode) btn.parentNode.insertBefore(signOutBtn, btn.nextSibling);
      }
    }).catch(() => {});
});

// ══════════════════════════════════════════════════════════════════════════════
// FUNDAMENTALS — Company Overview, Key Ratios & Financial Tables
// ══════════════════════════════════════════════════════════════════════════════

// Cache fetched data per symbol so switching back doesn't re-fetch
const _fundCache = {};
let _finChart = null;         // Chart.js instance for the mini trend chart
let _activeFinTab = "quarterly";

// ── Number formatters ────────────────────────────────────────────────────────

function fmtCr(n) {
  // Converts raw INR value (yfinance returns base currency) to Crores string
  if (n == null) return "—";
  const cr = n / 1e7;
  if (Math.abs(cr) >= 1e5)      return (cr / 1e5).toFixed(2) + " L Cr";  // Lakh Crore
  if (Math.abs(cr) >= 1)        return cr.toFixed(2) + " Cr";
  return n.toLocaleString("en-IN");
}

function fmtPct(n) {
  if (n == null) return "—";
  return (n * 100).toFixed(2) + "%";
}

function fmtRatio(n, dec = 2) {
  if (n == null) return "—";
  return Number(n).toFixed(dec);
}

function fmtMarketCap(n) {
  if (n == null) return "—";
  const cr = n / 1e7;
  if (cr >= 1e5) return "₹" + (cr / 1e5).toFixed(2) + " L Cr";
  return "₹" + cr.toFixed(0) + " Cr";
}

// Quarter label: "2024-09-30" → "Sep '24"
function fmtPeriod(dateStr) {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  return d.toLocaleDateString("en-IN", { month: "short", year: "2-digit" });
}

// ── Main loader ──────────────────────────────────────────────────────────────

async function loadFundamentals(symbol) {
  // Hide cards until data arrives
  const fCard = document.getElementById("fundamentalsCard");
  const finCard = document.getElementById("financialsCard");
  if (fCard) fCard.style.display = "none";
  if (finCard) finCard.style.display = "none";

  // Show loading spinner in financials card
  _showFinLoading(true);

  // Use cache if available
  if (_fundCache[symbol]) {
    _renderAll(_fundCache[symbol]);
    return;
  }

  try {
    const res = await fetch(apiUrl(`/financials/all?symbol=${symbol}`));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    _fundCache[symbol] = data;
    _renderAll(data);
  } catch (err) {
    console.warn("[Fundamentals] Fetch failed:", err);
    _showFinLoading(false);
    _showFinError("Could not load financial data: " + err.message);
  }
}

function _showFinLoading(show) {
  const el = document.getElementById("finLoading");
  if (el) el.style.display = show ? "flex" : "none";
  const errEl = document.getElementById("finError");
  if (errEl) errEl.style.display = "none";
}

function _showFinError(msg) {
  const el = document.getElementById("finError");
  const msgEl = document.getElementById("finErrorMsg");
  if (el) el.style.display = "block";
  if (msgEl) msgEl.textContent = msg;
}

function _renderAll(data) {
  _showFinLoading(false);

  // Show both cards
  const fCard = document.getElementById("fundamentalsCard");
  const finCard = document.getElementById("financialsCard");
  if (fCard) fCard.style.display = "";
  if (finCard) finCard.style.display = "";

  _renderRatios(data.fundamentals);
  _renderQuarterly(data.quarterly || []);
  _renderAnnual(data.annual || []);
  _renderBalanceSheet(data.balance_sheet || []);
  _renderCashFlow(data.cashflow || []);
}

// ── Ratios panel ─────────────────────────────────────────────────────────────

function _renderRatios(f) {
  if (!f) return;

  // Sector/industry line
  const parts = [f.sector, f.industry].filter(Boolean);
  const sectorEl = document.getElementById("fundSectorLine");
  if (sectorEl) sectorEl.textContent = parts.join("  ·  ") || "—";

  // Updated timestamp
  const updEl = document.getElementById("fundUpdated");
  if (updEl && f.fetched_at) {
    const d = new Date(f.fetched_at);
    updEl.textContent = "Updated " + d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  }

  // Set ratio cells
  _setRatioVal("frMarketCap",  fmtMarketCap(f.market_cap));
  _setRatioVal("frPE",         fmtRatio(f.pe_ratio));
  _setRatioVal("frPB",         fmtRatio(f.pb_ratio));
  _setRatioVal("frROE",        f.roe    != null ? fmtPct(f.roe)    : "—");
  _setRatioVal("frROCE",       f.roce   != null ? fmtRatio(f.roce, 2) + "%" : "—");
  _setRatioVal("frDE",         fmtRatio(f.debt_to_equity));
  _setRatioVal("frEPS",        f.eps    != null ? "₹" + fmtRatio(f.eps) : "—");
  _setRatioVal("frDivYield",   f.dividend_yield != null ? fmtPct(f.dividend_yield) : "—");
  _setRatioVal("frNetMargin",  f.profit_margin  != null ? fmtPct(f.profit_margin)  : "—");

  // Colour ROE / ROCE / Net Margin
  _colourRatio("frROE",       f.roe);
  _colourRatio("frROCE",      f.roce != null ? f.roce / 100 : null);
  _colourRatio("frNetMargin", f.profit_margin);

  // Description
  const descWrap = document.getElementById("fundDescWrap");
  const descEl   = document.getElementById("fundDesc");
  if (f.description && descEl && descWrap) {
    descEl.textContent = f.description;
    descWrap.style.display = "";
  }

  // Website
  const webWrap = document.getElementById("fundWebsite");
  const webLink = document.getElementById("fundWebsiteLink");
  if (f.website && webWrap && webLink) {
    webLink.href = f.website;
    webLink.textContent = f.website.replace(/^https?:\/\//, "");
    webWrap.style.display = "";
  }
}

function _setRatioVal(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _colourRatio(id, value) {
  const el = document.getElementById(id);
  if (!el || value == null) return;
  el.classList.remove("positive", "negative");
  el.classList.add(value >= 0 ? "positive" : "negative");
}

function toggleFundDesc() {
  const el = document.getElementById("fundDesc");
  const btn = document.getElementById("fundDescToggle");
  if (!el || !btn) return;
  const expanded = el.classList.toggle("expanded");
  btn.textContent = expanded ? "Show less ▴" : "Show more ▾";
}

// ── Financial tables ──────────────────────────────────────────────────────────

// Generic table builder.
// rows: array of objects
// cols: array of { key, label, fmt }
// periodKey: which field on each row is the column header
function _buildFinTable(headId, bodyId, rows, metrics, periodKey, periodFmt) {
  const thead = document.getElementById(headId);
  const tbody = document.getElementById(bodyId);
  if (!thead || !tbody || !rows.length) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#aab4d0;padding:18px">No data available</td></tr>';
    return;
  }

  // Header row — first cell blank, then one cell per period
  thead.innerHTML = "";
  const thBlank = document.createElement("th");
  thBlank.textContent = "";
  thead.appendChild(thBlank);
  rows.forEach(row => {
    const th = document.createElement("th");
    th.textContent = periodFmt(row[periodKey]);
    thead.appendChild(th);
  });

  // Body — one row per metric
  tbody.innerHTML = "";
  metrics.forEach(m => {
    const tr = document.createElement("tr");
    const tdLabel = document.createElement("td");
    tdLabel.textContent = m.label;
    tr.appendChild(tdLabel);

    rows.forEach(row => {
      const td = document.createElement("td");
      const raw = row[m.key];
      if (raw == null) {
        td.textContent = "—";
        td.className = "null-val";
      } else {
        const formatted = m.fmt(raw);
        td.textContent = formatted;
        // Colour net profit / free cashflow rows
        if (m.colour) {
          td.className = raw >= 0 ? "pos" : "neg";
        }
      }
      tr.appendChild(td);
    });

    tbody.appendChild(tr);
  });
}

function _renderQuarterly(rows) {
  // rows are newest-first from server
  const metrics = [
    { key: "sales",            label: "Revenue",         fmt: fmtCr },
    { key: "expenses",         label: "Total Expenses",  fmt: fmtCr },
    { key: "operating_profit", label: "Operating Profit",fmt: fmtCr, colour: true },
    { key: "net_profit",       label: "Net Profit",      fmt: fmtCr, colour: true },
    { key: "eps",              label: "EPS (₹)",         fmt: v => fmtRatio(v, 2) },
  ];
  _buildFinTable("qHead", "qBody", rows, metrics, "period", fmtPeriod);
  _buildNetProfitChart(rows);
}

function _renderAnnual(rows) {
  const metrics = [
    { key: "sales",            label: "Revenue",         fmt: fmtCr },
    { key: "expenses",         label: "Total Expenses",  fmt: fmtCr },
    { key: "operating_profit", label: "Operating Profit",fmt: fmtCr, colour: true },
    { key: "net_profit",       label: "Net Profit",      fmt: fmtCr, colour: true },
    { key: "eps",              label: "EPS (₹)",         fmt: v => fmtRatio(v, 2) },
  ];
  _buildFinTable("aHead", "aBody", rows, metrics, "year", y => String(y));
}

function _renderBalanceSheet(rows) {
  const metrics = [
    { key: "total_assets",      label: "Total Assets",      fmt: fmtCr },
    { key: "total_liabilities", label: "Total Liabilities", fmt: fmtCr },
    { key: "total_equity",      label: "Total Equity",      fmt: fmtCr, colour: true },
    { key: "borrowings",        label: "Borrowings",        fmt: fmtCr },
    { key: "reserves",          label: "Reserves",          fmt: fmtCr },
  ];
  _buildFinTable("bHead", "bBody", rows, metrics, "year", y => String(y));
}

function _renderCashFlow(rows) {
  const metrics = [
    { key: "operating_cashflow",  label: "Operating Cash Flow",  fmt: fmtCr, colour: true },
    { key: "investing_cashflow",  label: "Investing Cash Flow",  fmt: fmtCr, colour: true },
    { key: "financing_cashflow",  label: "Financing Cash Flow",  fmt: fmtCr, colour: true },
    { key: "free_cashflow",       label: "Free Cash Flow",       fmt: fmtCr, colour: true },
  ];
  _buildFinTable("cfHead", "cfBody", rows, metrics, "year", y => String(y));
}

// ── Mini net profit trend chart ───────────────────────────────────────────────

function _buildNetProfitChart(rows) {
  const ctx = document.getElementById("finChart");
  if (!ctx) return;

  // Reverse so oldest is on the left
  const sorted = [...rows].reverse();
  const labels = sorted.map(r => fmtPeriod(r.period));
  const profits = sorted.map(r => r.net_profit != null ? Math.round(r.net_profit / 1e7) : null); // in Crores

  if (_finChart) { _finChart.destroy(); _finChart = null; }

  const colors = profits.map(v => v == null ? "#252d40" : v >= 0 ? "rgba(34,197,94,0.70)" : "rgba(239,68,68,0.70)");

  _finChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Net Profit (Cr)",
        data: profits,
        backgroundColor: colors,
        borderRadius: 3,
        borderWidth: 0,
        barPercentage: 0.65,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => "Net Profit: ₹" + (c.parsed.y != null ? c.parsed.y.toFixed(2) : "—") + " Cr",
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#9aa5be", maxRotation: 0, font: { size: 9 } },
          border: { display: false },
        },
        y: {
          grid: { color: "rgba(0,0,0,0.05)", drawBorder: false },
          ticks: { color: "#9aa5be", font: { size: 9 }, callback: v => v + " Cr" },
          border: { display: false },
        },
      },
    },
  });
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function setFinTab(btn, tab) {
  document.querySelectorAll(".fin-tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _activeFinTab = tab;

  const panes = { quarterly: "tabQuarterly", annual: "tabAnnual", balance: "tabBalance", cashflow: "tabCashflow" };
  Object.entries(panes).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = key === tab ? "" : "none";
  });

  // Resize chart when switching to quarterly (Chart.js needs visible container)
  if (tab === "quarterly" && _finChart) {
    setTimeout(() => _finChart.resize(), 50);
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// FEATURE 1 — LIVE TICK CHART
// A rolling window line chart, visible only during intraday streaming.
// Shows every price tick with a 200-point rolling window.
// ══════════════════════════════════════════════════════════════════════════════

const LT_MAX_POINTS = 200;
let _ltPrices  = [];   // raw price values
let _ltTimes   = [];   // time labels
let _ltHigh    = null;
let _ltLow     = null;
let _ltCount   = 0;
let _ltChart   = null;
let _ltVisible = false;

function _showLiveTickCard(symbol) {
  const card = document.getElementById("liveTickCard");
  if (!card) return;
  card.style.display = "";
  const symEl = document.getElementById("liveTickSym");
  if (symEl) symEl.textContent = symbol || currentSym;
  _ltVisible = true;
  if (!_ltChart) _buildLiveTickChart();
}

function _hideLiveTickCard() {
  const card = document.getElementById("liveTickCard");
  if (card) card.style.display = "none";
  _ltVisible = false;
  _ltPrices = []; _ltTimes = [];
  _ltHigh = null; _ltLow = null; _ltCount = 0;
  if (_ltChart) { _ltChart.destroy(); _ltChart = null; }
}

function _buildLiveTickChart() {
  const ctx = document.getElementById("liveTickChart");
  if (!ctx) return;
  const c = ctx.getContext("2d");
  const grad = c.createLinearGradient(0, 0, 0, 160);
  grad.addColorStop(0, "rgba(59,130,246,0.18)");
  grad.addColorStop(1, "rgba(59,130,246,0.00)");

  _ltChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        data: [],
        borderColor: "#3b82f6",
        borderWidth: 1.5,
        fill: true,
        backgroundColor: grad,
        tension: 0.2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: "#3b82f6",
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            label: c => "₹" + Number(c.parsed.y).toLocaleString("en-IN", { minimumFractionDigits: 2 }),
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#4d5e7a", maxRotation: 0, maxTicksLimit: 8, font: { size: 9 } },
          border: { display: false },
        },
        y: {
          grid: { color: "rgba(46,56,80,0.5)", drawBorder: false },
          ticks: {
            color: "#4d5e7a",
            font: { size: 9 },
            callback: v => "₹" + Number(v).toLocaleString("en-IN"),
          },
          border: { display: false },
        },
      },
    },
  });
}

function _pushLiveTick(price, symbol) {
  if (price == null) return;

  // Show card on first tick if market is intraday
  if (!_ltVisible && window.candleType === "intraday") {
    _showLiveTickCard(symbol);
  }
  if (!_ltVisible) return;

  const now = new Date();
  const label = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  _ltPrices.push(price);
  _ltTimes.push(label);
  _ltCount++;

  // Rolling window
  if (_ltPrices.length > LT_MAX_POINTS) {
    _ltPrices.shift();
    _ltTimes.shift();
  }

  // Update hi/lo
  if (_ltHigh === null || price > _ltHigh) _ltHigh = price;
  if (_ltLow  === null || price < _ltLow)  _ltLow  = price;

  // Update stat labels
  const hiEl = document.getElementById("ltHigh");
  const loEl = document.getElementById("ltLow");
  const cntEl = document.getElementById("ltCount");
  if (hiEl) hiEl.textContent = "₹" + Number(_ltHigh).toLocaleString("en-IN", { minimumFractionDigits: 2 });
  if (loEl) loEl.textContent = "₹" + Number(_ltLow).toLocaleString("en-IN", { minimumFractionDigits: 2 });
  if (cntEl) cntEl.textContent = _ltCount;

  // Patch chart
  if (_ltChart) {
    _ltChart.data.labels = _ltTimes;
    _ltChart.data.datasets[0].data = _ltPrices;

    // Colour last point amber as the leading edge
    const n = _ltPrices.length;
    _ltChart.data.datasets[0].pointRadius = _ltPrices.map((_, i) => i === n - 1 ? 4 : 0);
    _ltChart.data.datasets[0].pointBackgroundColor = _ltPrices.map((_, i) => i === n - 1 ? "#f59e0b" : "#3b82f6");
    _ltChart.update("none");
  }
}

// Reset tick state when switching symbols
const _origSwitchSymbol = window.switchSymbol;
function switchSymbol(sym) {
  _hideLiveTickCard();
  candles = []; rsiArr = []; macdArr = []; signalArr = []; histArr = [];
  bbUpperArr = []; bbLowerArr = []; bbMidArr = [];
  openPrice = null;
  document.getElementById("stockSymbol").textContent = sym;
  connect(sym);
}


// ══════════════════════════════════════════════════════════════════════════════
// FEATURE 2 — STOCK COMPARISON
// Compare up to 4 symbols across: normalised price %, key ratios,
// quarterly revenue/profit, and volume.
// ══════════════════════════════════════════════════════════════════════════════

const CMP_COLOURS = ["#3b82f6", "#22c55e", "#f59e0b", "#a78bfa"];
const CMP_MAX     = 4;

let _cmpSymbols   = [];    // active comparison symbols
let _cmpData      = {};    // fetched data cache per symbol: { history, fundamentals }
let _cmpTab       = "price";
let _cmpQMetric   = "revenue";
let _cmpPriceChart  = null;
let _cmpQChart      = null;
let _cmpVolChart    = null;
let _compareOpen    = false;

// ── Toggle panel ─────────────────────────────────────────────────────────────
function toggleCompare() {
  _compareOpen = !_compareOpen;
  const overlay = document.getElementById("compareOverlay");
  const btn     = document.getElementById("compareNavBtn");
  if (!overlay) return;
  overlay.style.display = _compareOpen ? "flex" : "none";
  if (btn) btn.classList.toggle("active", _compareOpen);
  if (_compareOpen && _cmpSymbols.length === 0) {
    // Pre-seed with current symbol
    _cmpAddSymbol(currentSym);
  }
}

// ── Add / remove symbols ─────────────────────────────────────────────────────
function addCompareSymbol() {
  const inp = document.getElementById("compareInput");
  if (!inp) return;
  const sym = inp.value.trim().toUpperCase();
  inp.value = "";
  if (!sym) return;
  _cmpAddSymbol(sym);
}

function _cmpAddSymbol(sym) {
  if (_cmpSymbols.includes(sym)) return;
  if (_cmpSymbols.length >= CMP_MAX) {
    _cmpShowError(`Maximum ${CMP_MAX} symbols for comparison.`);
    return;
  }
  _cmpSymbols.push(sym);
  _renderCmpChips();
  _cmpFetchAndRender();
}

function _cmpRemoveSymbol(sym) {
  _cmpSymbols = _cmpSymbols.filter(s => s !== sym);
  delete _cmpData[sym];
  _renderCmpChips();
  _cmpFetchAndRender();
}

function clearCompare() {
  _cmpSymbols = [];
  _cmpData    = {};
  _renderCmpChips();
  _cmpClearCharts();
}

// ── Chip rendering ────────────────────────────────────────────────────────────
function _renderCmpChips() {
  const wrap = document.getElementById("compareChips");
  if (!wrap) return;
  wrap.innerHTML = "";
  _cmpSymbols.forEach((sym, i) => {
    const chip = document.createElement("div");
    chip.className = "cmp-chip";
    chip.style.borderColor = CMP_COLOURS[i];
    chip.innerHTML = `<span class="cmp-chip-dot" style="background:${CMP_COLOURS[i]}"></span>${sym}<button onclick="_cmpRemoveSymbol('${sym}')">✕</button>`;
    wrap.appendChild(chip);
  });
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function setCompareTab(btn, tab) {
  document.querySelectorAll(".compare-tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _cmpTab = tab;
  const panes = { price: "cmpPanePrice", ratios: "cmpPaneRatios", quarterly: "cmpPaneQuarterly", volume: "cmpPaneVolume" };
  Object.entries(panes).forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = k === tab ? "" : "none";
  });
  _cmpRender();
}

function setCmpQTab(btn, metric) {
  document.querySelectorAll(".compare-sub-tab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _cmpQMetric = metric;
  _cmpRenderQuarterly();
}

// ── Data fetch ────────────────────────────────────────────────────────────────
async function _cmpFetchAndRender() {
  if (_cmpSymbols.length === 0) { _cmpClearCharts(); return; }

  _cmpShowLoading(true);
  _cmpShowError("");

  const needed = _cmpSymbols.filter(s => !_cmpData[s]);
  await Promise.all(needed.map(async sym => {
    try {
      const [histRes, finRes] = await Promise.all([
        fetch(apiUrl(`/history?symbol=${sym}&range=1y`)),
        fetch(apiUrl(`/financials/all?symbol=${sym}`)),
      ]);
      const [histJson, finJson] = await Promise.all([histRes.json(), finRes.json()]);
      _cmpData[sym] = {
        history:      histJson.candles || [],
        fundamentals: finJson.fundamentals || null,
        quarterly:    finJson.quarterly    || [],
      };
    } catch (e) {
      console.warn(`[Compare] Fetch failed for ${sym}:`, e);
      _cmpData[sym] = { history: [], fundamentals: null, quarterly: [] };
    }
  }));

  _cmpShowLoading(false);
  _cmpRender();
}

function _cmpRender() {
  if (_cmpSymbols.length === 0) return;
  if (_cmpTab === "price")     _cmpRenderPrice();
  if (_cmpTab === "ratios")    _cmpRenderRatios();
  if (_cmpTab === "quarterly") _cmpRenderQuarterly();
  if (_cmpTab === "volume")    _cmpRenderVolume();
}

// ── Price % normalised chart ──────────────────────────────────────────────────
function _cmpRenderPrice() {
  const ctx = document.getElementById("cmpPriceChart");
  if (!ctx) return;

  const datasets = _cmpSymbols.map((sym, i) => {
    const candles = (_cmpData[sym] || {}).history || [];
    if (!candles.length) return null;
    const base = candles[0].c;
    const pctData = candles.map(c => base > 0 ? +((c.c - base) / base * 100).toFixed(3) : 0);
    const labels  = candles.map(c => {
      const d = new Date(c.t);
      return d.toLocaleDateString([], { day: "numeric", month: "short" });
    });
    return { sym, labels, pctData, colour: CMP_COLOURS[i] };
  }).filter(Boolean);

  if (!datasets.length) return;

  // Use longest dataset's labels as x-axis
  const longest = datasets.reduce((a, b) => a.labels.length > b.labels.length ? a : b);

  if (_cmpPriceChart) { _cmpPriceChart.destroy(); _cmpPriceChart = null; }

  _cmpPriceChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: longest.labels,
      datasets: datasets.map(d => ({
        label: d.sym,
        data: d.pctData,
        borderColor: d.colour,
        borderWidth: 1.8,
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: true, labels: { color: "#8a9bbe", boxWidth: 20, font: { size: 10 } } },
        tooltip: {
          mode: "index", intersect: false,
          callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y >= 0 ? "+" : ""}${c.parsed.y.toFixed(2)}%` },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#4d5e7a", maxRotation: 0, maxTicksLimit: 8, font: { size: 9 } },
          border: { display: false },
        },
        y: {
          grid: { color: "rgba(46,56,80,0.5)", drawBorder: false },
          ticks: {
            color: "#4d5e7a", font: { size: 9 },
            callback: v => (v >= 0 ? "+" : "") + v.toFixed(1) + "%",
          },
          border: { display: false },
        },
      },
    },
  });

  // Legend
  const leg = document.getElementById("cmpPriceLegend");
  if (leg) {
    leg.innerHTML = datasets.map(d => {
      const last = d.pctData[d.pctData.length - 1];
      const sign = last >= 0 ? "+" : "";
      return `<div class="cmp-leg-item"><span style="background:${d.colour}" class="cmp-leg-dot"></span><span>${d.sym}</span><span class="${last >= 0 ? "up" : "dn"}" style="font-weight:700">${sign}${last.toFixed(2)}%</span></div>`;
    }).join("");
  }
}

// ── Key ratios comparison table ───────────────────────────────────────────────
function _cmpRenderRatios() {
  const tbl = document.getElementById("cmpRatiosTable");
  if (!tbl) return;

  const METRICS = [
    { key: "market_cap",      label: "Market Cap",      fmt: v => fmtMarketCap(v) },
    { key: "pe_ratio",        label: "P/E Ratio",       fmt: v => v != null ? fmtRatio(v) : "—" },
    { key: "pb_ratio",        label: "P/B Ratio",       fmt: v => v != null ? fmtRatio(v) : "—" },
    { key: "roe",             label: "ROE",             fmt: v => v != null ? fmtPct(v) : "—" },
    { key: "roce",            label: "ROCE",            fmt: v => v != null ? fmtRatio(v, 2) + "%" : "—" },
    { key: "debt_to_equity",  label: "D/E Ratio",       fmt: v => v != null ? fmtRatio(v) : "—" },
    { key: "eps",             label: "EPS (TTM)",       fmt: v => v != null ? "₹" + fmtRatio(v) : "—" },
    { key: "dividend_yield",  label: "Dividend Yield",  fmt: v => v != null ? fmtPct(v) : "—" },
    { key: "profit_margin",   label: "Net Margin",      fmt: v => v != null ? fmtPct(v) : "—" },
    { key: "sector",          label: "Sector",          fmt: v => v || "—" },
  ];

  const syms = _cmpSymbols.filter(s => _cmpData[s]);

  // Header
  let html = "<thead><tr><th></th>";
  syms.forEach((sym, i) => {
    html += `<th style="color:${CMP_COLOURS[i]}">${sym}</th>`;
  });
  html += "</tr></thead><tbody>";

  METRICS.forEach(m => {
    html += `<tr><td class="cmp-row-label">${m.label}</td>`;
    syms.forEach(sym => {
      const f = (_cmpData[sym] || {}).fundamentals || {};
      const raw = f[m.key];
      let val = m.fmt(raw);
      // Colour numeric ratio cells
      let cls = "";
      if (typeof raw === "number" && m.key !== "pe_ratio" && m.key !== "pb_ratio" && m.key !== "debt_to_equity") {
        cls = raw >= 0 ? " class=\"pos\"" : " class=\"neg\"";
      }
      html += `<td${cls}>${val}</td>`;
    });
    html += "</tr>";
  });
  html += "</tbody>";
  tbl.innerHTML = html;
}

// ── Quarterly revenue / profit chart ─────────────────────────────────────────
function _cmpRenderQuarterly() {
  const ctx = document.getElementById("cmpQChart");
  if (!ctx) return;

  const syms = _cmpSymbols.filter(s => _cmpData[s]);
  if (!syms.length) return;

  // Collect all unique periods
  const allPeriods = [...new Set(
    syms.flatMap(s => (_cmpData[s].quarterly || []).map(r => r.period))
  )].sort();

  const key = _cmpQMetric === "revenue" ? "sales" : "net_profit";

  const datasets = syms.map((sym, i) => {
    const rows = (_cmpData[sym] || {}).quarterly || [];
    const periodMap = Object.fromEntries(rows.map(r => [r.period, r[key]]));
    return {
      label: sym,
      data: allPeriods.map(p => {
        const v = periodMap[p];
        return v != null ? Math.round(v / 1e7) : null; // in Crores
      }),
      backgroundColor: CMP_COLOURS[i] + "aa",
      borderColor: CMP_COLOURS[i],
      borderWidth: 1,
      borderRadius: 3,
      barPercentage: 0.7,
    };
  });

  if (_cmpQChart) { _cmpQChart.destroy(); _cmpQChart = null; }

  _cmpQChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: allPeriods.map(p => fmtPeriod(p)),
      datasets,
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: true, labels: { color: "#8a9bbe", boxWidth: 14, font: { size: 10 } } },
        tooltip: {
          mode: "index", intersect: false,
          callbacks: { label: c => `${c.dataset.label}: ₹${c.parsed.y != null ? c.parsed.y.toFixed(0) : "—"} Cr` },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#4d5e7a", maxRotation: 0, font: { size: 9 } },
          border: { display: false },
        },
        y: {
          grid: { color: "rgba(46,56,80,0.5)", drawBorder: false },
          ticks: { color: "#4d5e7a", font: { size: 9 }, callback: v => v + " Cr" },
          border: { display: false },
        },
      },
    },
  });
}

// ── Volume comparison chart ───────────────────────────────────────────────────
function _cmpRenderVolume() {
  const ctx = document.getElementById("cmpVolChart");
  if (!ctx) return;

  const syms = _cmpSymbols.filter(s => _cmpData[s] && _cmpData[s].history.length);
  if (!syms.length) return;

  const longest = syms.reduce((a, b) =>
    (_cmpData[a] || {}).history.length > (_cmpData[b] || {}).history.length ? a : b
  );
  const baseLabels = (_cmpData[longest].history || []).map(c => {
    const d = new Date(c.t);
    return d.toLocaleDateString([], { day: "numeric", month: "short" });
  });

  const datasets = syms.map((sym, i) => {
    const candles = (_cmpData[sym] || {}).history || [];
    return {
      label: sym,
      data: candles.map(c => c.v),
      borderColor: CMP_COLOURS[i],
      borderWidth: 1.5,
      fill: false,
      tension: 0.3,
      pointRadius: 0,
    };
  });

  if (_cmpVolChart) { _cmpVolChart.destroy(); _cmpVolChart = null; }

  _cmpVolChart = new Chart(ctx, {
    type: "line",
    data: { labels: baseLabels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: true, labels: { color: "#8a9bbe", boxWidth: 14, font: { size: 10 } } },
        tooltip: {
          mode: "index", intersect: false,
          callbacks: { label: c => `${c.dataset.label}: ${(c.parsed.y / 1e6).toFixed(2)}M` },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: "#4d5e7a", maxRotation: 0, maxTicksLimit: 8, font: { size: 9 } },
          border: { display: false },
        },
        y: {
          grid: { color: "rgba(46,56,80,0.5)", drawBorder: false },
          ticks: { color: "#4d5e7a", font: { size: 9 }, callback: v => (v / 1e6).toFixed(1) + "M" },
          border: { display: false },
        },
      },
    },
  });
}

// ── Utility ───────────────────────────────────────────────────────────────────
function _cmpClearCharts() {
  if (_cmpPriceChart) { _cmpPriceChart.destroy(); _cmpPriceChart = null; }
  if (_cmpQChart)     { _cmpQChart.destroy();     _cmpQChart     = null; }
  if (_cmpVolChart)   { _cmpVolChart.destroy();   _cmpVolChart   = null; }
  const tbl = document.getElementById("cmpRatiosTable");
  if (tbl) tbl.innerHTML = "";
  const leg = document.getElementById("cmpPriceLegend");
  if (leg) leg.innerHTML = "";
}

function _cmpShowLoading(show) {
  const el = document.getElementById("compareLoading");
  if (el) el.style.display = show ? "flex" : "none";
}

function _cmpShowError(msg) {
  const el = document.getElementById("compareError");
  if (!el) return;
  el.style.display = msg ? "" : "none";
  el.textContent = msg;
}

// Close compare panel with Escape key
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && _compareOpen) toggleCompare();
});


// ══════════════════════════════════════════════════════════════════════════
// FIBONACCI RETRACEMENT
// ══════════════════════════════════════════════════════════════════════════
let fibChart = null;

function buildFibChart() {
  if (!candles || candles.length < 2) return;

  const fibCandles = displayCount ? candles.slice(-displayCount) : candles;
  const highs  = fibCandles.map(c => c.h);
  const lows   = fibCandles.map(c => c.l);
  const swingH = Math.max(...highs);
  const swingL = Math.min(...lows);
  const diff   = swingH - swingL;

  const ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const levels = ratios.map(r => ({ r, price: swingH - diff * r }));

  // Render level badges
  const container = document.getElementById("fibLevels");
  if (container) {
    container.innerHTML = levels.map(l => {
      const pct = (l.r * 100).toFixed(1);
      const isKey = [0.382, 0.5, 0.618].includes(l.r);
      return `<div style="background:var(--bg3);border:1px solid ${isKey ? 'rgba(59,130,246,0.4)' : 'var(--border)'};border-radius:7px;padding:7px 10px;text-align:center">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:3px">${pct}%</div>
        <div style="font-size:13px;font-weight:700;color:${isKey ? 'var(--accent)' : 'var(--text)'};font-family:var(--sans)">₹${l.price.toLocaleString('en-IN',{maximumFractionDigits:1})}</div>
      </div>`;
    }).join('');
  }

  const badge = document.getElementById("fibRangeBadge");
  if (badge) badge.textContent = `H: ₹${swingH.toLocaleString('en-IN',{maximumFractionDigits:1})}  L: ₹${swingL.toLocaleString('en-IN',{maximumFractionDigits:1})}`;

  // Chart
  const labels = fibCandles.map(c => timeLabel(c.t));
  const closes = fibCandles.map(c => c.c);
  const ctx = document.getElementById("fibChart");
  if (!ctx) return;

  if (fibChart) fibChart.destroy();
  fibChart = new Chart(ctx.getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Price", data: closes, borderColor: "#3b82f6", borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false, order: 1 },
        ...levels.map(l => ({
          label: (l.r * 100).toFixed(1) + "%",
          data: Array(labels.length).fill(l.price),
          borderColor: [0, 1].includes(l.r) ? "rgba(139,148,168,0.4)" :
                       l.r === 0.618 ? "rgba(34,197,94,0.7)" :
                       l.r === 0.5   ? "rgba(245,158,11,0.7)" :
                       l.r === 0.382 ? "rgba(239,68,68,0.7)" : "rgba(139,148,168,0.3)",
          borderWidth: [0.382, 0.5, 0.618].includes(l.r) ? 1.5 : 1,
          borderDash: [4, 4],
          pointRadius: 0,
          fill: false,
          order: 2
        }))
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { display: false },
        y: { position: "right", grid: { color: "rgba(255,255,255,0.04)" }, ticks: { color: "#4d5e7a", font: { size: 9 }, maxTicksLimit: 6,
          callback: v => "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) } }
      }
    }
  });
}

// ══════════════════════════════════════════════════════════════════════════
// ICHIMOKU CLOUD  ─ TradingView-style
// Cloud fill plugin — defined once outside function to avoid redefinition errors
const ichimokuCloudPlugin = {
  beforeDatasetsDraw(chart) {
    if (!chart.canvas || chart.canvas.id !== "ichChart") return;
    const { ctx: c, scales: { x, y }, chartArea } = chart;
    const aIdx = chart.data.datasets.findIndex(d => d.label === "Senkou A");
    const bIdx = chart.data.datasets.findIndex(d => d.label === "Senkou B");
    if (aIdx < 0 || bIdx < 0) return;

    const aData = chart.data.datasets[aIdx].data;
    const bData = chart.data.datasets[bIdx].data;

    c.save();
    c.beginPath();
    c.rect(chartArea.left, chartArea.top, chartArea.width, chartArea.height);
    c.clip();

    for (let i = 1; i < aData.length; i++) {
      const a0 = aData[i-1], b0 = bData[i-1];
      const a1 = aData[i],   b1 = bData[i];
      if (a0 == null || b0 == null || a1 == null || b1 == null) continue;

      const x0  = x.getPixelForValue(i - 1);
      const x1  = x.getPixelForValue(i);
      const aY0 = y.getPixelForValue(a0), aY1 = y.getPixelForValue(a1);
      const bY0 = y.getPixelForValue(b0), bY1 = y.getPixelForValue(b1);
      const bull0 = a0 >= b0, bull1 = a1 >= b1;

      if (bull0 === bull1) {
        c.beginPath();
        c.moveTo(x0, aY0); c.lineTo(x1, aY1);
        c.lineTo(x1, bY1); c.lineTo(x0, bY0);
        c.closePath();
        c.fillStyle = bull0 ? "rgba(34,197,94,0.18)" : "rgba(239,68,68,0.18)";
        c.fill();
      } else {
        const t  = (b0 - a0) / ((a1 - a0) - (b1 - b0));
        const xi = x0 + t * (x1 - x0);
        const yi = y.getPixelForValue(a0 + t * (a1 - a0));

        c.beginPath();
        c.moveTo(x0, aY0); c.lineTo(xi, yi); c.lineTo(x0, bY0); c.closePath();
        c.fillStyle = bull0 ? "rgba(34,197,94,0.18)" : "rgba(239,68,68,0.18)";
        c.fill();

        c.beginPath();
        c.moveTo(xi, yi); c.lineTo(x1, aY1); c.lineTo(x1, bY1); c.closePath();
        c.fillStyle = bull1 ? "rgba(34,197,94,0.18)" : "rgba(239,68,68,0.18)";
        c.fill();
      }
    }
    c.restore();
  }
};

// ══════════════════════════════════════════════════════════════════════
let ichChart = null;

function calcIchimoku(candles) {
  const n = candles.length;
  const donchian = (start, end) => {
    let h = -Infinity, l = Infinity;
    for (let i = start; i <= end; i++) {
      h = Math.max(h, candles[i].h);
      l = Math.min(l, candles[i].l);
    }
    return (h + l) / 2;
  };
  const tenkan = [], kijun = [], senkouA = [], senkouB = [];
  for (let i = 0; i < n; i++) {
    tenkan.push(i >= 8  ? donchian(i - 8,  i) : null);
    kijun.push( i >= 25 ? donchian(i - 25, i) : null);
  }
  for (let i = 0; i < n; i++) {
    const a = (tenkan[i] != null && kijun[i] != null) ? (tenkan[i] + kijun[i]) / 2 : null;
    const b = i >= 51 ? donchian(i - 51, i) : null;
    senkouA.push(a);
    senkouB.push(b);
  }
  return { tenkan, kijun, senkouA, senkouB };
}

function buildIchimokuChart() {
  const minC = (window.candleType === "intraday") ? 9 : 52;
  if (!candles || candles.length < minC) {
    const el = document.getElementById("ichSignal");
    if (el) { el.textContent = "Need more data"; el.className = "ind-signal sig-neutral"; }
    return;
  }

  const { tenkan, kijun, senkouA, senkouB } = calcIchimoku(candles);
  const n = candles.length;
  const last = n - 1;
  const SHIFT = (displayCount && displayCount <= 31) ? 0 : 10;  // no future cloud on 1w/1m
  const totalLen = n + SHIFT;

  // ── Signal badge ────────────────────────────────────────────────────────────────────
  const sigEl = document.getElementById("ichSignal");
  if (sigEl && tenkan[last] != null && kijun[last] != null) {
    const cloudTop = Math.max(senkouA[last] || 0, senkouB[last] || 0);
    const cloudBot = Math.min(senkouA[last] || Infinity, senkouB[last] || Infinity);
    const close = candles[last].c;
    const aboveCloud = close > cloudTop;
    const belowCloud = close < cloudBot;
    const tkCross = tenkan[last] > kijun[last];
    if (aboveCloud && tkCross)       { sigEl.textContent = "Bullish"; sigEl.className = "ind-signal sig-bullish"; }
    else if (belowCloud && !tkCross) { sigEl.textContent = "Bearish"; sigEl.className = "ind-signal sig-bearish"; }
    else                             { sigEl.textContent = "Neutral"; sigEl.className = "ind-signal sig-neutral"; }
  }

  const ctxEl = document.getElementById("ichChart");
  if (!ctxEl) return;
  if (ichChart) ichChart.destroy();

  // ── Build data arrays ───────────────────────────────────────────────────────────────
  // For display: slice to displayCount if set (but use full arrays for Ichimoku math above)
  const dispStart = displayCount ? Math.max(0, n - displayCount) : 0;
  const dispCandles = candles.slice(dispStart);
  const dn = dispCandles.length;
  const dispTotal = dn + SHIFT;

  const baseLabels = dispCandles.map(c => timeLabel(c.t));
  const allLabels  = [...baseLabels, ...Array(SHIFT).fill("")];

  // Price — display slice only
  const priceData  = [...dispCandles.map(c => c.c), ...Array(SHIFT).fill(null)];

  // Tenkan & Kijun — display slice only
  const tenkanData = [...tenkan.slice(dispStart), ...Array(SHIFT).fill(null)];
  const kijunData  = [...kijun.slice(dispStart),  ...Array(SHIFT).fill(null)];

  // Senkou A & B shifted 26 forward (projects future cloud)
  const senkouAData = new Array(dispTotal).fill(null);
  const senkouBData = new Array(dispTotal).fill(null);
  for (let i = 0; i < dn; i++) {
    senkouAData[i + SHIFT] = senkouA[dispStart + i];
    senkouBData[i + SHIFT] = senkouB[dispStart + i];
  }

  // Chikou — close shifted 26 backward (lagging span)
  const chikouData = new Array(dispTotal).fill(null);
  for (let i = SHIFT; i < dn; i++) {
    chikouData[i - SHIFT] = dispCandles[i].c;
  }

  // ── Chart ────────────────────────────────────────────────────────────────────────────────
  ichChart = new Chart(ctxEl.getContext("2d"), {
    type: "line",
    plugins: [ichimokuCloudPlugin],
    data: {
      labels: allLabels,
      datasets: [
        { label: "Price",    data: priceData,   borderColor: "#cbd5e1",               borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false, order: 1 },
        { label: "Chikou",   data: chikouData,  borderColor: "rgba(167,139,250,0.75)", borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: false, order: 2 },
        { label: "Tenkan",   data: tenkanData,  borderColor: "#38bdf8",               borderWidth: 1.5, pointRadius: 0, tension: 0,   fill: false, order: 2 },
        { label: "Kijun",    data: kijunData,   borderColor: "#f97316",               borderWidth: 2,   pointRadius: 0, tension: 0,   fill: false, order: 2 },
        { label: "Senkou A", data: senkouAData, borderColor: "rgba(34,197,94,0.9)",   borderWidth: 1,   pointRadius: 0, tension: 0,   fill: false, order: 3 },
        { label: "Senkou B", data: senkouBData, borderColor: "rgba(239,68,68,0.9)",   borderWidth: 1,   pointRadius: 0, tension: 0,   fill: false, order: 3 }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: {
          display: true,
          position: "top",
          labels: {
            color: "#94a3b8", font: { size: 10 }, boxWidth: 18, padding: 12,
            filter: item => item && item.text !== "Price"
          }
        },
        tooltip: {
          mode: "index", intersect: false,
          filter: i => ["Price","Tenkan","Kijun"].includes(i.dataset.label),
          callbacks: {
            label: ctx => {
              if (ctx.raw == null) return null;
              return ctx.dataset.label + ": ₹" + ctx.raw.toLocaleString("en-IN", { maximumFractionDigits: 2 });
            }
          }
        }
      },
      scales: {
        x: {
          display: true,
          ticks: {
            color: "#4d5e7a", font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 7,
            callback: function(val, idx) {
              // Hide labels in the future projection zone (last SHIFT slots have empty labels)
              const lbl = this.getLabelForValue(idx);
              return lbl ? lbl : null;
            }
          },
          grid: { color: "rgba(255,255,255,0.03)" }
        },
        y: {
          position: "right",
          grid: { color: "rgba(255,255,255,0.04)" },
          ticks: { color: "#4d5e7a", font: { size: 9 }, maxTicksLimit: 6,
            callback: v => "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) }
        }
      }
    }
  });
}

// ── Hook into buildAllCharts ──────────────────────────────────────────────
const _origBuildAllCharts = buildAllCharts;
buildAllCharts = function() {
  _origBuildAllCharts();
  buildFibChart();
  buildIchimokuChart();
};
