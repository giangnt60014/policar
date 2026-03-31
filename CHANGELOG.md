# Changelog

All notable changes to Policar are documented here.

## [0.2.0.0] - 2026-03-31

### Added
- **Chainlink price feed integration** — reads ETH/USD, BTC/USD, XRP/USD directly from
  on-chain Chainlink aggregators on Polygon (no API key required)
- **Price to Beat** — fetched at exactly `xx:00:00` / `xx:05:00` (window open), saved to
  state.json and shown on each coin card in the dashboard
- **Closing price** — fetched at `xx:04:59` (1 second before window close), compared
  against Price to Beat to determine win/loss without relying on Polymarket resolution
- Dashboard coin cards now display Price to Beat and Close Price live

### Changed
- **Removed Polymarket resolution polling (Phase 0)** — no more `check_resolution` retry
  loop waiting for `outcomePrices` to hit 1.0; resolution is now instant via Chainlink
- Main loop split into two waits: sleep to `xx:00:00` → fetch price → sleep 10s → run cycle
- Price to Beat is fetched before placing orders (not after), ensuring accurate reference price
- Martingale win/loss logic now driven by Chainlink price comparison instead of Polymarket API

### Fixed
- Silent ETH/coin fetch drop — `fetch_coin_market` now catches all exceptions (not just
  `RuntimeError, ValueError, JSONDecodeError`), so curl_cffi errors are retried properly
- `fetch_all_markets` guards each `future.result()` so one coin crash never drops others

## [0.1.0.0] - 2026-03-29

### Added
- Martingale strategy per coin with configurable base/max bet
- Parallel Phase 0 (resolution) + Phase 1 (market fetch)
- Live CLOB price check before signing (`MAX_PRICE = 0.60`)
- Per-thread curl_cffi Session with error-35 SSL recovery
- Flask web dashboard with auto-refresh, coin cards, activity log
- Atomic state.json writes (no partial reads from dashboard)
