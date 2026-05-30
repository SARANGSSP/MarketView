// ════════════════════════════════════════════════════════════════════════
// STEP: "Stream live price updates over WebSocket"
//
// Everything that happens once a message arrives on the socket opened in
// 1-websocket-connection.js: parsing the payload, updating price/badges,
// flashing the UI, and patching charts in place.
// Depends on shared state/functions from marketview core (candles, priceChart,
// timeLabel, fmt, patchCandleChart, checkAlerts, renderWatchlist, etc.) and
// on `ws`, `currentSym`, `openPrice`, `_sessionHigh/_sessionLow` from
// 1-websocket-connection.js.
// ════════════════════════════════════════════════════════════════════════

// Live bar: the currently-forming candle (updated every 250ms, not committed to candles[])
let liveBar = null;

// ── Live data tracking ──────────────────────────────────────────────────────
let _lastPrice          = null;  // previous price for flash direction
let _tickCount          = 0;     // ticks received this minute
let _tickCounterReset   = null;  // interval to reset ticks/min counter
let _ticksThisWindow    = 0;     // ticks in current 60s window
let _lastTickTimestamp  = null;  // for "Updated Xs ago" display
let _tickAgeTimer       = null;  // interval to update "Updated Xs ago"
let _lastBid            = null;
let _lastAsk            = null;
let lastTickAt          = null;  // epoch ms of last message — used for stale detection (see 3-reconnect-logic.js)

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

// ── WS MESSAGE ENTRY POINT ─────────────────────────────────────────────────
// Assigned to ws.onmessage in 1-websocket-connection.js
function handleLiveMessage(event) {
  try {
    const d = JSON.parse(event.data);
    if (d.live_bar) { applyLiveBar(d); }
    else            { applyUpdate(d); }
  }
  catch (e) { console.error("[WS] Parse error:", e); }
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
  resetStaleTimer(); // defined in 3-reconnect-logic.js

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
  checkAlerts(d.symbol, d.close); // defined in 4-alerts.js

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
