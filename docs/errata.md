# Erratum: inverted BTC-implied stablecoin rate in the original notebook

**Affected:** `notebooks/01_close_based_study.ipynb`, cell 1 (where the gaps are computed) and everything derived from
`*_gap_bps`: the statistics table, histograms/box plots and "cumulative inefficiency" charts of cell 3.
**Not affected:** the cycle enumeration (cell 2) and the backtest and its metrics (cells 4 and 5), which use direct directed
rates only. (Cell numbers count the intro cell added in this repository as cell 0.)

## The bug

For a stablecoin pair `(a, b)` the direct market gives "units of `b` per 1 `a`". Going through BTC, the implied rate is

```
implied(a → b) = (b per BTC) / (a per BTC)  = btc_b / btc_a
```

The notebook computes `btc_a / btc_b`, the **inverse**. Since every stablecoin trades close to 1, the inverse is
approximately `2 − implied`, so the measured "gap" is about **twice the deviation of the direct rate from parity**
rather than the mismatch of the BTC triangle.

Worked example, first row of the dataset (2020-01-01 00:00, USDC/USDT): direct 1.0038, `BTCUSDC` 7153.57, `BTCUSDT` 7177.02.

| | implied rate | gap |
|---|---:|---:|
| Notebook (`btc_a / btc_b`) | 0.99673 | **+70.9 bps** |
| Corrected (`btc_b / btc_a`) | 1.00328 | **+5.2 bps** |

## Impact on the reported statistics

Reproduce with `python research/gap_errata_check.py` (output in `docs/errata_gap_statistics.csv`):

| Pair | Version | Mean \|gap\| (bps) | Median | Max | Share > 10 bps | Share > 50 bps |
|---|---|---:|---:|---:|---:|---:|
| USDC/USD | notebook | 6.38 | 4.59 | 74.8 | 19.3 % | 0.20 % |
| USDC/USD | corrected | 5.50 | 3.66 | 74.2 | 14.7 % | 0.16 % |
| USDT/USD | notebook | 12.40 | 10.60 | 83.3 | 52.5 % | 0.33 % |
| USDT/USD | corrected | 5.49 | 3.62 | 72.7 | 14.9 % | 0.17 % |
| USDC/USDT | notebook | 10.79 | 7.38 | 1,171.4 | 37.9 % | 1.39 % |
| USDC/USDT | corrected | **1.40** | 0.85 | 115.5 | 0.55 % | 0.01 % |

For USDC/USDT, the market with 6 years of history, most of the reported inefficiency (≈ 90 % of the mean gap) was
produced by the inversion. For the USD pairs the corrected gap (~5.5 bps) is about what one expects from comparing the
last trades of three different markets in the same hour.

## What I did about it

The original notebook is kept as it was, with a banner on top pointing here, so that the history of the analysis stays
honest. The live order-book bot (`bot/arbitrage.py`) uses the correct orientation and has a unit test for it
(`test_stable_pegs_report_deviation_and_btc_gap`).
