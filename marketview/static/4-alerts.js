// ════════════════════════════════════════════════════════════════════════
// STEP: "Add alert delivery status tracking to live price polling"
//
// NOTE ON THIS STEP: the source file has no price-polling loop — price
// updates arrive via the WebSocket stream (see 2-stream-live-prices.js),
// not polling. There's no separate mechanism to relabel as "polling," so
// this file contains the existing alert-delivery-status code as-is: each
// alert carries a `triggered` flag (its delivery status), flipped by
// checkAlerts() and surfaced via toast + sound + badge count. checkAlerts()
// is called from applyUpdate() in 2-stream-live-prices.js on every tick.
// ════════════════════════════════════════════════════════════════════════

// Alerts: [{ id, symbol, price, direction:"above"|"below", triggered:false }]
let alerts = JSON.parse(localStorage.getItem("mv_alerts") || "[]");

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

// Checks incoming price ticks against active alerts and flips their
// delivery/triggered status when a condition is hit.
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

// Wire up alert symbol input (autocomplete)
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
