# Data

| File | Size | Description |
|---|---:|---|
| `binance_hourly_long.csv.gz` | 8.9 MB | One row per market and UTC hour with the full kline fields. Input of the notebook. |
| `binance_hourly_wide.csv.gz` | 6.2 MB | One row per UTC hour, OHLCV columns per market (`BTCUSDT_close`, `USDCUSDT_volume`, …). Used by the research scripts. |
| `live_demo_session_market_minutes.csv` | tiny | Minute-level record of the live order-book bot (see below). |

All candle files are produced by `research/download_data.py` from Binance's **public** klines endpoint (no API key) and
cover 2020-01-01 → 2026-10-14 UTC. Markets that did not exist yet are simply empty, never back-filled:

| Market | First hour in the file |
|---|---|
| BTCUSDT | 2020-01-01 |
| BTCUSDC, USDCUSDT | 2020-01-01 (USDCUSDT has a 166-day hole, 2022-09 → 2023-03) |
| USDCUSD, USDTUSD | 2025-11-18 |
| BTCUSD | 2025-12-04 |

Hours without any trade carry the previous close and `volume = 0` (Binance behaviour); the research code drops them.

`binance_data/arbitrage_hourly_dataset.csv` is **not** included: it is regenerated (about 24 MB) by cell 1 of the
notebook and is git-ignored.

## `live_demo_session_market_minutes.csv`

Recorded by the bot on the Binance Spot **Demo** environment. One row per minute of operation:

| Column | Meaning |
|---|---|
| `minute_utc` | start of the minute (UTC) |
| `best_executable_bps` | highest net return of any cycle that minute, simulated against the live order book |
| `best_theoretical_bps` | same, valued at mid prices with fees (close-style) |
| `best_route` | the cycle that achieved `best_executable_bps` |

Both values are maxima over the evaluations of that minute and over all cycles. The bot ran in several sessions with
different trade sizes (100-1,500 USD) and asset sets; the record is therefore not specific to one size. The SQLite
database it comes from contains account balances and is deliberately not published.
