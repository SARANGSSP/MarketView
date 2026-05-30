// ════════════════════════════════════════════════════════════════════════
// STEP: "Add reconnect logic for dropped sockets"
//
// Detects a dropped/erroring socket and schedules a reconnect, plus a
// "stale" watchdog that flags the UI if ticks stop arriving without a
// formal close event. Depends on `ws`, `currentSym`, `setStatus()` from
// 1-websocket-connection.js and `connect()` also from that file.
// ════════════════════════════════════════════════════════════════════════

let reconnectTimer = null;
let staleTimer      = null;

// Reset the "no ticks for 30s" watchdog. Called on connect and on every
// message received (see handleLiveMessage's callers in 2-stream-live-prices.js).
function resetStaleTimer() {
  clearTimeout(staleTimer);
  staleTimer = setTimeout(() => setStatus("stale"), 30000);   // 30 s with no tick → stale
}

// Assigned to ws.onerror in 1-websocket-connection.js
function handleSocketError() {
  setStatus("error");
  _hideLiveTickCard();
  console.error("[WS] Connection error. Is server.py running?");
}

// Assigned to ws.onclose in 1-websocket-connection.js
// Schedules a reconnect after 5s, but only while still viewing the live "1d" range.
function handleSocketClose() {
  setStatus("disconnected");
  _hideLiveTickCard();
  if (window.currentRange === "1d") {
    reconnectTimer = setTimeout(() => connect(currentSym), 5000);
  }
}
