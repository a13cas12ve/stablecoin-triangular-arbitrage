"""Reproduce the erratum of the original notebook: the BTC-implied stablecoin rate was inverted.

For a stablecoin pair (a, b) the direct rate is "units of b per 1 a". Going through BTC:

    implied(a -> b) = (b per BTC) / (a per BTC) = btc_b / btc_a        <- correct
    notebook cell 0 used btc_a / btc_b                                   <- inverse of the correct value

Because every stablecoin trades close to 1, the inverse is close to 2 - implied, so the "gap" the notebook
measured is roughly twice the deviation of the direct rate from parity instead of the BTC-triangle mismatch.

Usage (from the repository root):
    python research/gap_errata_check.py
"""

from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "binance_hourly_wide.csv.gz"
if not DATA.exists():  # uncompressed file written by research/download_data.py
    DATA = DATA.with_suffix("")

# pair -> (direct market, market giving "a per BTC", market giving "b per BTC")
PAIRS = {
    "USDC/USD": ("USDCUSD", "BTCUSDC", "BTCUSD"),
    "USDT/USD": ("USDTUSD", "BTCUSDT", "BTCUSD"),
    "USDC/USDT": ("USDCUSDT", "BTCUSDC", "BTCUSDT"),
}


def gap_bps(direct, implied):
    return ((direct - implied) / implied * 1e4).abs()


def main():
    wide = pd.read_csv(DATA, index_col=0, parse_dates=True)
    rows = []
    for name, (direct, a, b) in PAIRS.items():
        d = pd.concat([wide[f"{direct}_close"], wide[f"{a}_close"], wide[f"{b}_close"]], axis=1,
                      keys=["direct", "btc_a", "btc_b"]).dropna()
        for label, implied in (("notebook (btc_a / btc_b)", d.btc_a / d.btc_b),
                               ("corrected (btc_b / btc_a)", d.btc_b / d.btc_a)):
            g = gap_bps(d.direct, implied)
            rows.append({"pair": name, "version": label, "hours": len(d), "mean_abs_gap_bps": g.mean(),
                         "median_abs_gap_bps": g.median(), "max_abs_gap_bps": g.max(),
                         "share_gt_10bps_%": (g > 10).mean() * 100, "share_gt_50bps_%": (g > 50).mean() * 100})
    out = pd.DataFrame(rows).round(2)
    pd.set_option("display.width", 200)
    print(out.to_string(index=False))
    out.to_csv(Path(__file__).resolve().parent.parent / "docs" / "errata_gap_statistics.csv", index=False)


if __name__ == "__main__":
    main()
