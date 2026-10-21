# Methodology

## 1. Data

| Source | What | Where |
|---|---|---|
| Binance public `GET /api/v3/klines` | hourly OHLCV for `BTCUSD, BTCUSDT, BTCUSDC, USDCUSD, USDTUSD, USDCUSDT`, 2020-01-01 → 2026-10-14 | `data/*.csv.gz`, created by `research/download_data.py` |
| Binance Spot Demo, `depth20@100ms` stream | live order books of every active stablecoin market | consumed by `bot/`, summarised per minute in `data/live_demo_session_market_minutes.csv` |

Coverage caveats: `USDCUSD` and `USDTUSD` only exist from 2025-11, `BTCUSD` from 2025-12, and `USDCUSDT` has a 166-day
hole (2022-09 → 2023-03). Missing markets are never filled with substitutes.

## 2. Close-based study (notebook)

* **Rates.** For a market `BASEQUOTE` the directed rate "units of quote per unit of base" is its price; the reverse
  direction is the inverse price.
* **Cycles.** All simple cycles (no repeated intermediate asset) among {USDT, USDC, USD, BTC}, both directions.
  Gross return of a cycle = product of the directed rates around it; net return applies a taker fee per leg
  (0.1 %, 0 % on USDC/USDT) → `net = gross · Π(1 − fee)`.
* **Backtest.** At every hourly close, take the cycle with the highest net return; if it exceeds 0.01 bps, "trade" it
  and compound capital. One account per starting asset, initial capital 10,000, last six months.
* **Assumption that turns out to matter:** every conversion is valued at the candle **close** of its own market.

## 3. Live order-book engine

### 3.1 From closes to executable prices

For each cycle `A → B → … → A` and a trade size, the engine simulates the legs in order, feeding the amount received
on one leg into the next:

* **SELL** `base → quote`: consume bids level by level. Quantity is floored to the market's `stepSize`; the trade must
  clear `minNotional`; insufficient depth rejects the cycle.
* **BUY** `quote → base`: spend the quote amount (`quoteOrderQty`) consuming asks; the resulting base quantity is
  floored to `stepSize`, and the cost of that rounded quantity is recomputed.
* **Fees.** The account's taker fee per market (`/api/v3/account/commission`), read at start-up.
* **Two numbers per cycle.**
  * `theoretical_bps`: mid prices and fees. This is what a close-style valuation would show.
  * `value_bps`: executable return, including the dust left in intermediate assets by lot rounding, valued at mid.
    This is the **decision metric**; `exec_bps` (amount returned to the source asset) is reported alongside it.

### 3.2 Market universe

At start-up the engine reads `exchangeInfo`, keeps every `TRADING` market between the chosen assets, and enumerates all
simple cycles up to `max_legs` (default 4). For the four original assets that is 60 cycles; with the stablecoins
USD1, USDS, USDe and RLUSD it is 202. The WebSocket reconnects when the universe changes. Quiet markets only emit on
change, so they are refreshed over REST after 30 s.

### 3.3 Execution and risk controls

* Market orders leg by leg; each leg's realised amount is the input of the next. A failed leg triggers an unwind back to
  the source asset through the direct market (configurable).
* A trade is only attempted if the stream is fresh (max book age), the best executable return clears the threshold,
  the cooldown and hourly-trade limits allow it, and (in demo mode) the account has the source balance.
* Session-loss limit (account value vs session start) and consecutive-failure limit stop the bot.
* Any failure inside a cycle, including network errors, is recorded as a failed trade with the legs already executed.
* Account value is computed from mid prices; assets without a price are reported as unvalued instead of being counted as
  zero.

### 3.4 Observability

SQLite stores trades (with every leg: expected vs realised price and fees), account value snapshots every minute,
per-minute market summaries (best theoretical / executable return, stablecoin deviations) and an event log. The
dashboard shows books, peg deviations, every cycle's theoretical vs executable return, balances and history.

## 4. Validation

* **Unit tests** (`tests/`, 17 tests): cycle counts on a complete graph (4 · (3 + 6 + 6) = 60), closed/simple/connected
  cycles, hand-computed leg simulations (depth walking, lot rounding, minimum notional, fees), theoretical return,
  peg deviation and the BTC-implied rate.
* **Order-path check on the Demo account**: a 10 USDT USDT → USDC → USDT cycle predicted −0.09 bps and realised −0.09 bps
  (both legs filled, 517 ms). Order parameters for every market shape were also validated with `/api/v3/order/test`.

## 5. What was *not* done

No latency model, no queue-position model for limit orders, no multi-venue prices, no production-account fees, and no
observation of stress events. These are discussed in [`findings.md`](findings.md).
