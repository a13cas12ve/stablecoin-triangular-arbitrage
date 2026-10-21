"""Cliente REST mínimo para Binance Spot (entorno demo)."""

import hashlib
import hmac
import math
import time
import uuid
import urllib.parse
from decimal import Decimal

import requests


CLIENT_PREFIX = "arb"   # marca de origen: las órdenes de esta app llevan clientOrderId "arb-xxxx" en Binance


def new_client_id():
    return f"{CLIENT_PREFIX}-{uuid.uuid4().hex[:20]}"


class BinanceError(Exception):
    def __init__(self, status, code, msg):
        super().__init__(f"HTTP {status} · código {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


def floor_to(value, step):
    """Redondea hacia abajo a un múltiplo de step, tolerando ruido de coma flotante."""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


def fmt(value, step):
    """Texto decimal con los decimales que permite step (sin notación científica)."""
    decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)
    return f"{floor_to(value, step):.{decimals}f}"


class BinanceREST:
    def __init__(self, api_key, api_secret, base_url):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-MBX-APIKEY"] = api_key
        self.time_offset_ms = 0
        self.filters = {}  # symbol -> {step, tick, min_notional, quote_precision}

    def _request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            # Margen de 3 s hacia atrás: Binance rechaza marcas >1 s POR DELANTE de su reloj, pero admite hasta
            # recvWindow (10 s) por detrás. La medición del desfase tiene un error de ~medio RTT.
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms - 3000
            params["recvWindow"] = 10_000
            query = urllib.parse.urlencode(params)
            signature = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={signature}"
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        response = self.session.request(method, url, timeout=15)
        try:
            data = response.json()
        except ValueError:
            data = {"code": -1, "msg": response.text[:200]}
        if response.status_code >= 400:
            if signed and data.get("code") == -1021:
                self.sync_time()  # Reloj desincronizado: se corrige para la próxima llamada.
            raise BinanceError(response.status_code, data.get("code"), data.get("msg"))
        return data

    def sync_time(self):
        """Desfase servidor − local. Se toman 3 mediciones y se usa la de menor latencia: la primera, con la
        conexión en frío, puede tardar segundos y el punto medio del viaje deja de ser fiable (error de varios s)."""
        best = None
        for _ in range(3):
            before = time.time() * 1000
            server = self._request("GET", "/api/v3/time")["serverTime"]
            after = time.time() * 1000
            if best is None or after - before < best[0]:
                best = (after - before, int(server - (before + after) / 2))
        self.time_offset_ms = best[1]
        return self.time_offset_ms

    def discover(self, assets):
        """Catálogo de pares spot cuyo base y quote están en `assets` (cualquier estado) y sus filtros.

        Devuelve {símbolo: {"base", "quote", "status"}}. Los filtros se guardan en self.filters.
        """
        data = self._request("GET", "/api/v3/exchangeInfo", {"permissions": "SPOT"})
        assets = set(assets)
        catalog = {}
        for s in data["symbols"]:
            if s["baseAsset"] not in assets or s["quoteAsset"] not in assets:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            catalog[s["symbol"]] = {"base": s["baseAsset"], "quote": s["quoteAsset"], "status": s["status"]}
            self.filters[s["symbol"]] = {
                "status": s["status"],
                "step": float(f["LOT_SIZE"]["stepSize"]),
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "min_notional": float(notional.get("minNotional", 0)),
                # quoteOrderQty: céntimos para stablecoins; 8 decimales si el quote fuese BTC.
                "quote_step": 1e-8 if s["quoteAsset"] == "BTC" else 0.01,
            }
        return catalog

    def book_ticker(self, symbol):
        """Mejor bid y ask actuales de un símbolo (REST), para valorar monedas fuera del universo."""
        d = self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return float(d["bidPrice"]), float(d["askPrice"])

    def depth(self, symbol, limit=20):
        return self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    def account(self):
        return self._request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    def taker_fee(self, symbol):
        data = self._request("GET", "/api/v3/account/commission", {"symbol": symbol}, signed=True)
        return (float(data["standardCommission"]["taker"])
                + float(data["specialCommission"]["taker"])
                + float(data["taxCommission"]["taker"]))

    def market_order(self, symbol, side, amount, free_balance=None):
        """Orden a mercado que gasta `amount` del activo de entrada.

        SELL: amount está en el activo base (cantidad a vender, redondeada al stepSize).
        BUY: amount está en el activo quote (se usa quoteOrderQty).
        """
        f = self.filters[symbol]
        params = {"symbol": symbol, "side": side, "type": "MARKET", "newOrderRespType": "FULL",
                  "newClientOrderId": new_client_id()}
        if side == "SELL":
            qty = floor_to(amount, f["step"])
            # Si hay polvo previo suficiente, se redondea hacia arriba para no acumularlo.
            if free_balance is not None and free_balance >= qty + f["step"] - 1e-12 and amount - qty > 1e-12:
                qty += f["step"]
            params["quantity"] = fmt(qty, f["step"])
        else:
            params["quoteOrderQty"] = fmt(amount, f["quote_step"])
        return self._request("POST", "/api/v3/order", params, signed=True)
