"""Analyse the minute-level market record of the live order-book arbitrage bot (Binance Spot Demo).

Each row of data/live_demo_session_market_minutes.csv is one minute of the bot's evaluation loop (4 Hz):
  - best_theoretical_bps: best cycle valued at mid prices with fees  (the analogue of the notebook's close-based view)
  - best_executable_bps:  best cycle simulated against the real order book (spread, depth, lot size, fees)
Both are the maximum over the seconds of that minute and over all evaluated cycles.

Usage (from the repository root):
    python research/live_session_analysis.py
"""

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "live_demo_session_market_minutes.csv"
FIG = ROOT / "docs" / "figures" / "theoretical_vs_executable.png"


def main():
    d = pd.read_csv(CSV, parse_dates=["minute_utc"])
    d["execution_cost_bps"] = d.best_theoretical_bps - d.best_executable_bps
    print(f"minutes: {len(d)}  ({d.minute_utc.min()} -> {d.minute_utc.max()} UTC)")
    q = [0.5, 0.9, 0.99, 1.0]
    summary = pd.DataFrame({
        "best theoretical (mid + fees)": d.best_theoretical_bps.quantile(q),
        "best executable (order book)": d.best_executable_bps.quantile(q),
        "gap = execution cost": d.execution_cost_bps.quantile(q),
    }).round(3)
    summary.index = ["median", "p90", "p99", "max"]
    print("\nbps, per-minute best over all cycles:\n", summary.to_string())
    for thr in (-0.05, 0.0, 0.1, 0.5, 1.0):
        print(f"minutes with an executable cycle >= {thr:+.2f} bps: {(d.best_executable_bps >= thr).sum()} / {len(d)}")
    print("\nmost frequent best cycle:\n", d.best_route.value_counts().head(5).to_string())
    print(f"\nminutes where the theoretical view was > 0: {(d.best_theoretical_bps > 0).mean() * 100:.0f} %  |  "
          f"> 0.5 bps: {(d.best_theoretical_bps > 0.5).mean() * 100:.0f} %")

    FIG.parent.mkdir(parents=True, exist_ok=True)
    # The bot was stopped for hours at a time: leave those periods blank instead of joining the two ends with a line.
    full = d.set_index("minute_utc")[["best_theoretical_bps", "best_executable_bps"]].resample("1min").asfreq()
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(full.index, full.best_theoretical_bps, color="#2a78d6", lw=1.4, label="best cycle, mid prices + fees (close-style)")
    ax.plot(full.index, full.best_executable_bps, color="#eb6834", lw=1.4, label="best cycle, executed against the order book")
    ax.axhline(0, color="#52514e", lw=1, ls="--")
    ax.set_ylabel("net return per cycle (bps)")
    ax.set_title("Same cycles, two valuations: the edge disappears once execution is modelled\n(blank = bot stopped, no data)",
                 loc="left", fontsize=11)
    ax.legend(frameon=False, loc="upper left")
    ax.grid(color="#e6e5e1")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIG, dpi=150)
    print("figure ->", FIG)


if __name__ == "__main__":
    main()
