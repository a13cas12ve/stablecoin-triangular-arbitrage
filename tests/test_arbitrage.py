"""Unit tests for the order-book arbitrage maths. Expected values are worked out by hand in the comments."""

import pytest

from arbitrage import Universe, mid, simulate_cycle, simulate_leg, stable_pegs, theoretical_bps, usd_prices
from binance_api import floor_to, fmt

# Fully connected 4-asset graph (K4): 6 markets.
MARKETS = {
    "USDCUSDT": ("USDC", "USDT"), "USDTUSD": ("USDT", "USD"), "USDCUSD": ("USDC", "USD"),
    "BTCUSDT": ("BTC", "USDT"), "BTCUSDC": ("BTC", "USDC"), "BTCUSD": ("BTC", "USD"),
}
FILT = {"step": 1.0, "min_notional": 5.0, "quote_step": 0.01}


def book(bids, asks):
    return {"bids": bids, "asks": asks}


def flat_book(price, size=1e9):
    """Book with zero spread and effectively unlimited depth at one price."""
    return book([(price, size)], [(price, size)])


# ------------------------------------------------------------------ graph
def test_edge_market_direction():
    u = Universe(MARKETS)
    assert u.edge_market("USDC", "USDT") == ("USDCUSDT", "SELL")   # selling the base USDC for the quote USDT
    assert u.edge_market("USDT", "USDC") == ("USDCUSDT", "BUY")    # buying the base with the quote
    assert u.edge_market("USDC", "EUR") == (None, None)


def test_cycle_count_on_a_complete_graph():
    # K4, per source asset: 3 two-leg + 3*2 three-leg + 3! four-leg cycles.
    assert len(Universe(MARKETS, max_legs=4).cycles) == 4 * (3 + 6 + 6)
    assert len(Universe(MARKETS, max_legs=3).cycles) == 4 * (3 + 6)
    assert len(Universe(MARKETS, max_legs=2).cycles) == 4 * 3


def test_cycles_are_closed_simple_and_connected():
    u = Universe(MARKETS, max_legs=4)
    for cycle in u.cycles:
        assert cycle[0] == cycle[-1]
        assert len(set(cycle[:-1])) == len(cycle) - 1, "intermediate assets must not repeat"
        assert len(cycle) - 1 <= 4
        assert all(u.edge_market(a, b)[0] for a, b in zip(cycle, cycle[1:]))


def test_isolated_pair_only_has_round_trips():
    u = Universe({"RLUSDUSDT": ("RLUSD", "USDT")})
    assert {len(c) - 1 for c in u.cycles} == {2}
    assert all(n == 0 for n in u.routes_by_asset(min_legs=3).values())


# ------------------------------------------------------------------ prices
def test_usd_prices_propagate_from_usdt():
    mids = {"USDCUSDT": 1.0010, "USDTUSD": 0.9990, "USDCUSD": 1.0, "BTCUSDT": 80_000.0, "BTCUSDC": 79_900.0, "BTCUSD": 80_080.0}
    books = {s: flat_book(m) for s, m in mids.items()}
    p = usd_prices(books, Universe(MARKETS))
    assert p["USDT"] == 1.0
    assert p["USDC"] == pytest.approx(1.0010)            # direct USDC/USDT market
    assert p["USD"] == pytest.approx(1 / 0.9990)         # USDT/USD inverted
    assert p["BTC"] == pytest.approx(80_000.0)


def test_mid_handles_empty_books():
    assert mid(None) is None
    assert mid(book([], [(1.0, 1)])) is None
    assert mid(book([(0.9990, 1)], [(1.0010, 1)])) == pytest.approx(1.0)


# ------------------------------------------------------------------ single leg against the book
def test_sell_walks_the_bids_and_charges_the_fee():
    b = book([(0.9990, 500), (0.9980, 5000)], [(1.0, 1)])
    leg, reason = simulate_leg(b, "SELL", 600, FILT, 0.001)
    # 500 @ 0.9990 + 100 @ 0.9980 = 499.5 + 99.8 = 599.3 ; fee 0.1 % on the proceeds
    assert reason is None
    assert leg["spent"] == 600
    assert leg["notional"] == pytest.approx(599.3)
    assert leg["received"] == pytest.approx(599.3 * 0.999)
    assert leg["avg_price"] == pytest.approx(599.3 / 600)
    assert leg["worst_price"] == 0.9980


def test_sell_rounds_down_to_the_lot_size():
    leg, _ = simulate_leg(flat_book(1.0), "SELL", 99.7, FILT, 0.0)
    assert leg["spent"] == 99          # step 1.0 -> 99 units, 0.7 stays as dust


def test_sell_without_enough_depth_is_rejected():
    leg, reason = simulate_leg(book([(0.999, 10)], [(1.0, 10)]), "SELL", 100, FILT, 0.0)
    assert leg is None and reason == "profundidad insuficiente"


def test_min_notional_is_enforced():
    leg, reason = simulate_leg(flat_book(1.0), "SELL", 4, FILT, 0.0)
    assert leg is None and reason == "por debajo del nocional mínimo"


def test_buy_with_quote_amount_rounds_the_base_to_the_lot():
    b = book([(1.0, 1)], [(1.001, 300), (1.002, 5000)])
    leg, reason = simulate_leg(b, "BUY", 500, FILT, 0.0)
    # budget 500: 300 @ 1.001 = 300.3, rest 199.7 / 1.002 = 199.30 -> 499.30 base -> lot 499
    # cost of 499: 300 @ 1.001 + 199 @ 1.002 = 300.3 + 199.398 = 499.698
    assert reason is None
    assert leg["received"] == 499
    assert leg["spent"] == pytest.approx(499.698)
    assert leg["avg_price"] == pytest.approx(499.698 / 499)


# ------------------------------------------------------------------ whole cycles
def test_theoretical_return_matches_the_hand_calculation():
    u = Universe(MARKETS)
    mids = {"USDCUSDT": 1.0010, "USDTUSD": 0.9990, "USDCUSD": 1.0010}
    cycle = ("USDT", "USDC", "USD", "USDT")
    # USDT->USDC buys USDC (x 1/1.0010), USDC->USD sells USDC (x 1.0010), USD->USDT buys USDT (x 1/0.9990)
    gross = (1 / 1.0010) * 1.0010 * (1 / 0.9990)
    assert theoretical_bps(cycle, mids, {s: 0.0 for s in mids}, u) == pytest.approx((gross - 1) * 1e4)
    with_fees = gross * (1 - 0.001) ** 3
    assert theoretical_bps(cycle, mids, {s: 0.001 for s in mids}, u) == pytest.approx((with_fees - 1) * 1e4)


def test_theoretical_return_is_none_without_a_price():
    u = Universe(MARKETS)
    assert theoretical_bps(("USDT", "USDC", "USDT"), {"USDCUSDT": None}, {}, u) is None


def test_simulated_cycle_on_a_frictionless_book_returns_the_theoretical_value():
    u = Universe(MARKETS)
    mids = {"USDCUSDT": 1.0010, "USDTUSD": 0.9990, "USDCUSD": 1.0010, "BTCUSDT": 80_000.0, "BTCUSDC": 80_000.0, "BTCUSD": 80_000.0}
    books = {s: flat_book(m) for s, m in mids.items()}
    filters = {s: {"step": 1e-8, "min_notional": 0.0, "quote_step": 1e-8} for s in mids}
    fees = {s: 0.0 for s in mids}
    prices = usd_prices(books, u)
    cycle = ("USDT", "USDC", "USD", "USDT")
    sim = simulate_cycle(cycle, 1000.0, books, filters, fees, prices, u)
    assert sim["ok"] and len(sim["legs"]) == 3
    assert sim["exec_bps"] == pytest.approx(theoretical_bps(cycle, mids, fees, u), rel=1e-6)
    assert sim["pnl_usdt"] == pytest.approx(1000.0 * sim["exec_bps"] / 1e4, rel=1e-3)


def test_a_zero_spread_zero_fee_round_trip_is_flat_but_a_spread_makes_it_lose():
    u = Universe(MARKETS)
    filters = {"USDCUSDT": {"step": 1.0, "min_notional": 5.0, "quote_step": 0.01}}
    prices = {"USDT": 1.0, "USDC": 1.0}
    flat = {"USDCUSDT": flat_book(1.0)}
    wide = {"USDCUSDT": book([(0.9999, 1e9)], [(1.0001, 1e9)])}      # 2 bps wide
    cyc = ("USDT", "USDC", "USDT")
    s0 = simulate_cycle(cyc, 1000.0, flat, filters, {"USDCUSDT": 0.0}, prices, u)
    s1 = simulate_cycle(cyc, 1000.0, wide, filters, {"USDCUSDT": 0.0}, prices, u)
    assert s0["exec_bps"] == pytest.approx(0.0, abs=1e-9)
    assert s1["exec_bps"] < -1.9, "crossing a 2 bps spread costs about 2 bps (minus lot-size dust)"


# ------------------------------------------------------------------ pegs
def test_stable_pegs_report_deviation_and_btc_gap():
    u = Universe(MARKETS)
    mids = {"USDCUSDT": 1.0010, "USDTUSD": 0.9990, "USDCUSD": 1.0000, "BTCUSDT": 80_000.0, "BTCUSDC": 79_920.0, "BTCUSD": 80_080.0}
    books = {s: flat_book(m) for s, m in mids.items()}
    prices = usd_prices(books, u)
    pegs = stable_pegs(mids, prices, u)
    assert pegs["USDC"]["peg_bps"] == pytest.approx(10.0)
    # implied USDC/USDT through BTC = (USDT per BTC) / (USDC per BTC) = 80000 / 79920
    assert pegs["USDC"]["implied"] == pytest.approx(80_000.0 / 79_920.0)
    assert pegs["USDC"]["gap_bps"] == pytest.approx((1.0010 / (80_000.0 / 79_920.0) - 1) * 1e4)


# ------------------------------------------------------------------ exchange helpers
def test_floor_to_and_fmt():
    assert floor_to(99.7, 1.0) == 99
    assert floor_to(0.30000000000000004, 0.1) == pytest.approx(0.3)   # float noise must not drop a whole lot
    assert fmt(0.123456, 0.001) == "0.123"
    assert fmt(5, 1) == "5"
    assert fmt(2499.999, 0.01) == "2499.99"
