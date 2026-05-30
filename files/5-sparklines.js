// ════════════════════════════════════════════════════════════════════════
// STEP: "Add per-stock sparklines"
//
// NOT IMPLEMENTED — no matching code found in marketview.js.
//
// The watchlist (renderWatchlist(), in the original file) shows only a
// symbol, last price, and % change per row — there is no mini-chart, no
// stored intraday price series per watchlist symbol, and no canvas/SVG
// rendering for sparklines anywhere in the source. The closest related
// features are `wlPriceCache` (single latest price + change per symbol,
// not a series) and `_buildLiveTickChart`/`_pushLiveTick` (a single
// full-size live tick chart for the *active* symbol only, not per-row
// sparklines for the whole watchlist).
//
// Per instructions, this step is left unimplemented rather than invented.
// If you want it built, let me know and I can add it as a real feature
// (it would need: a small rolling price history per watchlist symbol,
// and a lightweight canvas/SVG renderer per .wl-item row).
// ════════════════════════════════════════════════════════════════════════
