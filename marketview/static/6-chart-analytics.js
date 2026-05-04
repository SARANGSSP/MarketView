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

