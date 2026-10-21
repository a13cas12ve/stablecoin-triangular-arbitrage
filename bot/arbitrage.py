"""Cálculo de ciclos de arbitraje sobre libros de órdenes.

El estudio usa precios de cierre: cada conversión se valora al close y se aplica la
comisión. En vivo eso no es ejecutable, así que aquí cada pata se simula contra el
libro real: vender base consume bids, comprar base consume asks, nivel a nivel, con
el stepSize, el mínimo nocional y la comisión taker de cada mercado. El rendimiento
"teórico" a precio medio se calcula también para compararlo con el del estudio.

Los mercados no son fijos: `Universe` se construye con los mercados que Binance tiene
activos entre los activos elegidos, y de ahí salen los ciclos.
"""

from binance_api import floor_to
from config import ASSET_ORDER


class Universe:
    """Mercados activos entre los activos elegidos y todos sus ciclos simples."""

    def __init__(self, markets, max_legs=4):
        self.markets = dict(markets)                                   # símbolo -> (base, quote)
        self.pair_to_symbol = {pair: sym for sym, pair in self.markets.items()}
        present = {a for pair in self.markets.values() for a in pair}
        self.assets = sorted(present, key=lambda a: (ASSET_ORDER.index(a) if a in ASSET_ORDER else 99, a))
        self.max_legs = max_legs
        self.cycles = self._enumerate_cycles()

    def edge_market(self, frm, to):
        """(símbolo, lado) que convierte frm en to."""
        if (frm, to) in self.pair_to_symbol:
            return self.pair_to_symbol[(frm, to)], "SELL"
        if (to, frm) in self.pair_to_symbol:
            return self.pair_to_symbol[(to, frm)], "BUY"
        return None, None

    def neighbors(self, asset):
        return sorted(b for b in self.assets if b != asset and self.edge_market(asset, b)[0])

    def _enumerate_cycles(self):
        """Ciclos simples de 2..max_legs patas por activo de origen (como en el estudio, con tope de patas)."""
        cycles = []
        for source in self.assets:
            def explore(path):
                current = path[-1]
                if len(path) >= 2 and self.edge_market(current, source)[0]:
                    cycles.append(tuple(path) + (source,))
                if len(path) >= self.max_legs:
                    return
                for nxt in self.neighbors(current):
                    if nxt != source and nxt not in path:
                        explore(path + [nxt])
            explore([source])
        return cycles

    def routes_by_asset(self, min_legs=3):
        """Ciclos de al menos `min_legs` patas (contando cada activo de origen) en los que participa cada activo.

        Con min_legs=3 se excluyen las idas y vueltas dentro de un mismo mercado, que solo pagan el spread.
        """
        count = {a: 0 for a in self.assets}
        for c in self.cycles:
            if len(c) - 1 >= min_legs:
                for a in set(c):
                    count[a] += 1
        return count


def mid(book):
    if not book or not book["bids"] or not book["asks"]:
        return None
    return (book["bids"][0][0] + book["asks"][0][0]) / 2


def usd_prices(books, universe):
    """Valor de una unidad de cada activo en USDT a precio medio, recorriendo el grafo desde USDT.

    Se propaga por anchura, así cada activo se valora por el camino más corto (un mercado directo con USDT
    si existe).
    """
    mids = {s: mid(books.get(s)) for s in universe.markets}
    prices = {"USDT": 1.0}
    frontier = ["USDT"]
    while frontier:
        nxt = []
        for asset in frontier:
            for sym, (base, quote) in universe.markets.items():
                m = mids.get(sym)
                if not m:
                    continue
                if quote == asset and base not in prices:
                    prices[base] = prices[asset] * m
                    nxt.append(base)
                elif base == asset and quote not in prices:
                    prices[quote] = prices[asset] / m
                    nxt.append(quote)
        frontier = nxt
    return prices


def simulate_leg(book, side, amount_in, filt, fee):
    """Ejecuta una orden a mercado contra el libro. Devuelve None si no es ejecutable."""
    if not book:
        return None, "sin libro"
    step, min_notional = filt["step"], filt["min_notional"]
    if side == "SELL":
        qty = floor_to(amount_in, step)
        remaining, proceeds, worst = qty, 0.0, None
        for price, size in book["bids"]:
            take = min(remaining, size)
            proceeds += take * price
            remaining -= take
            worst = price
            if remaining <= 1e-12:
                break
        if qty <= 0 or remaining > 1e-12:
            return None, "profundidad insuficiente" if qty > 0 else "por debajo del lote mínimo"
        if proceeds < min_notional:
            return None, "por debajo del nocional mínimo"
        return {
            "spent": qty, "received": proceeds * (1 - fee), "avg_price": proceeds / qty,
            "worst_price": worst, "notional": proceeds, "fee_paid": proceeds * fee,
        }, None
    # BUY con quoteOrderQty: gasta quote y recibe base redondeada al stepSize.
    budget = floor_to(amount_in, filt["quote_step"])
    remaining, base = budget, 0.0
    for price, size in book["asks"]:
        cost = price * size
        if cost >= remaining:
            base += remaining / price
            remaining = 0.0
            break
        base += size
        remaining -= cost
    if remaining > 1e-9:
        return None, "profundidad insuficiente"
    qty = floor_to(base, step)
    left, spent, worst = qty, 0.0, None
    for price, size in book["asks"]:
        take = min(left, size)
        spent += take * price
        left -= take
        worst = price
        if left <= 1e-12:
            break
    if qty <= 0:
        return None, "por debajo del lote mínimo"
    if spent < min_notional:
        return None, "por debajo del nocional mínimo"
    return {
        "spent": spent, "received": qty * (1 - fee), "avg_price": spent / qty,
        "worst_price": worst, "notional": spent, "fee_paid": qty * fee,
    }, None


def theoretical_bps(cycle, mids, fees, universe):
    """Rendimiento del ciclo a precio medio con comisiones: el equivalente del estudio con close."""
    factor = 1.0
    for frm, to in zip(cycle, cycle[1:]):
        symbol, side = universe.edge_market(frm, to)
        price = mids.get(symbol)
        if not price:
            return None
        factor *= (price if side == "SELL" else 1 / price) * (1 - fees.get(symbol, 0.001))
    return (factor - 1) * 10_000


def simulate_cycle(cycle, start_amount, books, filters, fees, prices, universe):
    """Simula el ciclo completo contra los libros.

    exec_bps: lo que vuelve al activo de origen frente a lo que salió.
    value_bps: incluye el polvo que queda en activos intermedios (redondeo de lotes),
               valorado a precio medio. Es el criterio de decisión del bot.
    """
    deltas = {a: 0.0 for a in universe.assets}
    amount, legs = start_amount, []
    for frm, to in zip(cycle, cycle[1:]):
        symbol, side = universe.edge_market(frm, to)
        leg, reason = simulate_leg(books.get(symbol), side, amount, filters[symbol], fees.get(symbol, 0.001))
        if leg is None:
            return {"ok": False, "reason": f"{symbol}: {reason}"}
        deltas[frm] -= leg["spent"]
        deltas[to] += leg["received"]
        legs.append({"symbol": symbol, "side": side, "from": frm, "to": to, **leg})
        amount = leg["received"]
    source = cycle[0]
    start_value = start_amount * prices.get(source, 1.0)
    value_change = sum(d * prices.get(a, 0.0) for a, d in deltas.items())
    return {
        "ok": True,
        "start_amount": start_amount,
        "end_amount": amount,
        "exec_bps": (amount / start_amount - 1) * 10_000,
        "value_bps": value_change / start_value * 10_000 if start_value else 0.0,
        "pnl_usdt": value_change,
        "legs": legs,
    }


def stable_pegs(mids, prices, universe):
    """Paridad de cada stablecoin frente a USDT (bps) y, si hay mercados con BTC, el gap frente a la tasa
    implícita vía BTC (como en el estudio).

    Directa = USDT por 1 unidad, por el camino más corto del grafo. Implícita vía BTC = (USDT por BTC) / (S por BTC).
    """
    btc_usdt = prices.get("BTC")
    out = {}
    for s in universe.assets:
        if s in ("USDT", "BTC") or s not in prices:
            continue
        direct = prices[s]
        row = {"direct": direct, "peg_bps": (direct - 1) * 10_000}
        sym, side = universe.edge_market("BTC", s)
        if sym and mids.get(sym) and btc_usdt:
            s_per_btc = mids[sym] if side == "SELL" else 1 / mids[sym]
            implied = btc_usdt / s_per_btc
            row.update(implied=implied, gap_bps=(direct - implied) / implied * 10_000)
        out[s] = row
    return out
