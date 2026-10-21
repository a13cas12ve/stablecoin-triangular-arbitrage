# Findings

## 1. The close-based backtest looks great

The notebook's backtest cell (cell 4 in this copy, last six months, 2026-04-13 → 2026-10-13, 0.1 % taker fee per leg except USDC/USDT at 0 %,
10,000 initial capital, best cycle chosen at every hourly close):

| Starting asset | Final capital | Total return | Max drawdown | Trades | Win rate | Avg trade |
|---|---:|---:|---:|---:|---:|---:|
| USD, BTC | 10,417.48 | +4.17 % | 0 % | 40 | 100 % | +10.2 bps |
| USDC | 10,416.86 | +4.17 % | 0 % | 40 | 100 % | +10.2 bps |
| USDT | 10,415.83 | +4.16 % | 0 % | 40 | 100 % | +10.2 bps |

A 100 % win rate with zero drawdown and a Sharpe near 6.6 is the usual signature of a measurement problem rather than of
an edge. That is what the second study tests.

## 2. Why it does not survive contact with the order book

The notebook values each leg at the **close of that market's hourly candle**. A close is the price of the *last trade*
in the hour, so the three closes of a cycle are prices at three different instants. Their mismatch is partly
asynchronous last-trade noise, which can be captured in a spreadsheet but not by sending three orders.

An executable cycle instead pays, on every leg:

* **half the bid-ask spread**: about 0.1 bp on the USD markets and USDC/USDT, and around 1 bp on markets such as
  USD1/USDC, USDe and RLUSD (sampled from the live books);
* **lot-size rounding**: 1 unit on stablecoin/stablecoin markets, which strands dust in intermediate assets;
* **fees**: 0 % on most stablecoin markets, but 0.1 % on BTC markets, so any cycle through BTC pays ≈ 20 bps in two legs.

## 3. Live evidence (Binance Spot Demo)

240 recorded minutes (2026-10-18 18:18 → 2026-10-19 08:14 UTC), 202 cycles evaluated at ~4 Hz, per-minute best over
all cycles (`research/live_session_analysis.py`):

| bps | median | p90 | p99 | max |
|---|---:|---:|---:|---:|
| Best cycle, mid prices + fees | +0.246 | +0.555 | +0.748 | +1.048 |
| Best cycle, executed against the book | −0.014 | −0.012 | −0.004 | −0.004 |
| Execution cost | 0.290 | 0.597 | 0.756 | 1.130 |

* Minutes with an executable cycle ≥ 0 bps: **0 / 240** (≥ 0.1, 0.5 and 1.0 bps: also 0).
* The close-style (mid-price) view was positive in **100 %** of minutes and above 0.5 bps in 13 % of them: that is the
  "opportunity" a close-based study sees.
* The best cycle was almost always the stablecoin-only triangle `USD → USDC → USDT → USD` (167 of 240 minutes), i.e. the
  cheapest-to-cross markets, and it still lost about 0.01-0.05 bps per cycle.
* Across sessions the bot ran in Demo-orders mode with thresholds from 0.0 to 1.0 bps and **sent no orders**: by its own
  decision rule nothing was worth executing.

**Calibration check.** A controlled USDT → USDC → USDT test (10 USDT, 2 legs, 0 % fee) predicted −0.09 bps from the book
and realised −0.09 bps (fills at 1.00113 and 1.00112), so the simulator's cost model is faithful at small size.

## 4. If this were forced to trade

At the observed −0.01 to −0.05 bps per cycle, 1,500 USD per cycle loses ≈ 0.002-0.007 USD each: a slow bleed, not a
blow-up. The research conclusion is therefore not "arbitrage loses money" but "there is nothing to capture at these costs".

## 5. What could change the conclusion

* **Lower fees or rebates** than the Demo account's (VIP tiers, market-making programmes).
* **Dislocations**: depegs and liquidation cascades widen the stablecoin triangle by orders of magnitude; a bot that
  waits for those would trade rarely and needs risk controls (a hold-to-parity strategy is studied in the companion
  repository, `stablecoin-mean-reversion`).
* **Other venues** or latency advantages, which this single-venue study does not address.
* **A much longer live record** including weekdays and volatile periods.

## 6. Quality notes

* An erratum for the original notebook's BTC-implied rate is documented in [`errata.md`](errata.md); the backtest above
  does not use it.
* The 240-minute record mixes sessions with different trade sizes (100-1,500 USD) and asset sets.
* Bugs in the bot found during this work, all fixed in the current code: valuing assets outside its universe at zero (it
  once tripped the session-loss guard by reading a 1,000 USDT holding as a 10 % loss), and a gap where a non-exchange
  error in the middle of a cycle could leave executed legs unrecorded (never triggered).
