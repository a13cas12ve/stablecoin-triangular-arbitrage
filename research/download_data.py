"""Download hourly Binance spot candles for arbitrage analysis.

Dependencies: python -m pip install requests pandas
Example: python research/download_data.py --start 2022-01-01 --end 2024-01-01

Dates use UTC; --end is exclusive. Binance may not list every requested
USD-quoted market, so missing markets are reported and never fabricated.
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests


KLINES_URL = "https://api.binance.com/api/v3/klines"
HOUR_MS = 60 * 60 * 1000
PAIRS = {
	"BTC/USD": "BTCUSD",
	"BTC/USDT": "BTCUSDT",
	"BTC/USDC": "BTCUSDC",
	"USDC/USD": "USDCUSD",
	"USDT/USD": "USDTUSD",
	"USDC/USDT": "USDCUSDT",
}
KLINE_COLUMNS = [
	"open_time", "open", "high", "low", "close", "volume", "close_time",
	"quote_volume", "trade_count", "taker_buy_base_volume",
	"taker_buy_quote_volume", "ignore",
]


def date_to_ms(value):
	return int(datetime.strptime(value, "%Y-%m-%d").replace(
		tzinfo=timezone.utc
	).timestamp() * 1000)


def get_hourly_candles(session, pair, symbol, start_ms, end_ms):
	"""Fetch one symbol in Binance's maximum 1,000-candle request batches."""
	candles = []
	cursor = start_ms
	while cursor < end_ms:
		response = session.get(KLINES_URL, params={
			"symbol": symbol,
			"interval": "1h",
			"startTime": cursor,
			"endTime": end_ms - 1,
			"limit": 1000,
		}, timeout=30)
		if response.status_code == 429:
			time.sleep(int(response.headers.get("Retry-After", "2")))
			continue
		response.raise_for_status()
		batch = response.json()
		if not batch:
			break
		candles.extend(batch)
		next_cursor = int(batch[-1][0]) + HOUR_MS
		if next_cursor <= cursor:
			break
		cursor = next_cursor
		time.sleep(0.1)

	if not candles:
		return pd.DataFrame()
	df = pd.DataFrame(candles, columns=KLINE_COLUMNS)
	df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
	df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
	numeric = [c for c in KLINE_COLUMNS if c not in ("open_time", "close_time")]
	df[numeric] = df[numeric].apply(pd.to_numeric, errors="coerce")
	df = df.drop_duplicates("open_time").sort_values("open_time")
	df.insert(0, "symbol", symbol)
	df.insert(0, "pair", pair)
	return df


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--start", default="2020-01-01", help="UTC start date (YYYY-MM-DD)")
	parser.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
						help="UTC end date, exclusive (YYYY-MM-DD)")
	parser.add_argument("--output-dir", default="data", help="Directory for CSV output")
	args = parser.parse_args()
	try:
		start_ms, end_ms = date_to_ms(args.start), date_to_ms(args.end)
	except ValueError as exc:
		parser.error(f"Dates must be YYYY-MM-DD: {exc}")
	if start_ms >= end_ms:
		parser.error("--start must be earlier than --end")

	output_dir = Path(args.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	session = requests.Session()
	session.headers["User-Agent"] = "binance-hourly-arbitrage-downloader/1.0"
	frames, unavailable = {}, []

	for pair, symbol in PAIRS.items():
		try:
			df = get_hourly_candles(session, pair, symbol, start_ms, end_ms)
		except requests.HTTPError as exc:
			# Binance returns HTTP 400 / code -1121 for symbols it does not list.
			if exc.response is not None and exc.response.status_code == 400:
				print(f"Unavailable Binance market: {pair} ({symbol})")
				unavailable.append(pair)
				continue
			raise
		if df.empty:
			print(f"No candles returned for {pair} ({symbol})")
			unavailable.append(pair)
			continue
		frames[pair] = df
		print(f"Downloaded {len(df)} hourly candles for {pair}")

	if not frames:
		raise SystemExit("No requested spot symbols returned data from Binance.")

	# Long CSV: one row per pair and UTC candle, with complete Binance kline data.
	pd.concat(frames.values(), ignore_index=True).to_csv(
		output_dir / "binance_hourly_long.csv", index=False
	)

	# Wide CSV: aligned OHLCV series, convenient for cross-market calculations.
	wide_parts = []
	measures = ["open", "high", "low", "close", "volume", "quote_volume", "trade_count"]
	for pair, df in frames.items():
		symbol = PAIRS[pair]
		part = df.set_index("open_time")[measures].rename(
			columns={name: f"{symbol}_{name}" for name in measures}
		)
		wide_parts.append(part)
	wide = pd.concat(wide_parts, axis=1).sort_index()

	# BTC cross-implied USDC/USDT rate versus the direct market rate.
	if {"BTCUSDT_close", "BTCUSDC_close"}.issubset(wide.columns):
		implied = wide["BTCUSDT_close"] / wide["BTCUSDC_close"].where(
			wide["BTCUSDC_close"] != 0
		)
		wide["implied_USDCUSDT_from_BTC"] = implied
		if "USDCUSDT_close" in wide.columns:
			wide["USDCUSDT_direct_minus_implied"] = wide["USDCUSDT_close"] - implied
	wide.index.name = "open_time"
	wide.to_csv(output_dir / "binance_hourly_wide.csv")

	if unavailable:
		print("Unavailable pairs: " + ", ".join(unavailable))
		print("Binance may not list these USD spot markets; no substitute prices were used.")
	print(f"Saved long and wide datasets in {output_dir.resolve()}")


if __name__ == "__main__":
	main()
