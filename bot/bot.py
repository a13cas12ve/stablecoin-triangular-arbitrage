"""Motor del bot: libros en vivo, evaluación de ciclos, ejecución y registro de la cuenta."""

import asyncio
import collections
import json
import time

import websockets

from arbitrage import Universe, mid, simulate_cycle, stable_pegs, theoretical_bps, usd_prices
from binance_api import BinanceError, BinanceREST
from config import ASSETS, CANDIDATE_ASSETS, DISPLAY, load_env, load_settings, save_settings, validate_settings
from storage import Storage

EVAL_INTERVAL_S = 0.25
ACCOUNT_INTERVAL_S = 5
EQUITY_SNAPSHOT_S = 60
RESYNC_AFTER_S = 30


class Engine:
    def __init__(self):
        self.env = load_env()
        self.settings = validate_settings(load_settings())
        self.api = BinanceREST(self.env["api_key"], self.env["api_secret"], self.env["base_url"])
        self.storage = Storage()
        self.books = {}
        self.fees = {}
        self.catalog = {}                  # todos los pares entre activos candidatos (cualquier estado)
        self.extra_prices = {}             # precio en USDT de monedas en cartera fuera del universo (p. ej. FDUSD)
        self.universe = None               # mercados activos entre los activos elegidos y sus ciclos
        self._ws_restart = None
        self.ws_connected = False
        self.last_ws_msg = 0.0
        self.api_ok = False
        self.ready = False
        self.init_error = None
        self.account = {"balances": {}, "total_usdt": None, "updated": None, "can_trade": None}
        self.running = False
        self.executing = False
        self.session_start = None          # {"ts", "equity"} al pulsar Iniciar
        self.last_trade_ts = 0.0
        self.trade_times = collections.deque()
        self.consecutive_failures = 0
        self.last_trade_id = 0
        self.last_eval = {"routes": [], "best": None, "pegs": {}, "market_ok": False}
        self.samples = collections.deque(maxlen=3600)   # 1 muestra/s, última hora
        self.minute_acc = None
        self.events = collections.deque(maxlen=200)
        self.last_equity_snapshot = 0.0
        self._tasks = []
        for e in reversed(self.storage.events(100)):
            self.events.append(e)
        last = self.storage.trades(limit=1)
        self.last_trade_id = last[0]["id"] if last else 0

    # ---------------------------------------------------------------- utilidades
    def log(self, level, msg):
        event = {"ts": time.time(), "level": level, "msg": msg}
        self.events.append(event)
        self.storage.add_event(level, msg)
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {level.upper():5} {msg}", flush=True)
        except (UnicodeError, OSError):
            pass  # La consola de Windows (cp1252) no admite «→»; el registro ya está en BD y en el panel.

    def prices(self):
        return usd_prices(self.books, self.universe) if self.universe else {}

    # ---------------------------------------------------------------- arranque
    async def start(self):
        self._ws_restart = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._init_loop()),
            asyncio.create_task(self._ws_loop()),
            asyncio.create_task(self._resync_loop()),
            asyncio.create_task(self._eval_loop()),
            asyncio.create_task(self._account_loop()),
            asyncio.create_task(self._sample_loop()),
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _init_loop(self):
        delay = 2
        while not self.ready:
            try:
                offset = await asyncio.to_thread(self.api.sync_time)
                self.catalog = await asyncio.to_thread(self.api.discover, ASSETS)
                await self._build_universe()
                await self.refresh_account()
                self.ready, self.init_error = True, None
                self.log("info", f"Conectado a Binance demo (desfase de reloj {offset} ms).")
            except Exception as exc:  # noqa: BLE001 - se reintenta con cualquier fallo de red/API
                self.init_error = str(exc)
                self.log("error", f"No se pudo inicializar la API: {exc}. Reintento en {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                while True:  # Resincroniza reloj cada 10 min
                    await asyncio.sleep(600)
                    try:
                        await asyncio.to_thread(self.api.sync_time)
                    except Exception:  # noqa: BLE001
                        pass

    # ---------------------------------------------------------------- universo de mercados
    async def _build_universe(self):
        """Mercados TRADING entre los activos elegidos, comisiones y ciclos. Reinicia el stream si cambian."""
        chosen = set(self.settings["assets"])
        markets = {sym: (c["base"], c["quote"]) for sym, c in self.catalog.items()
                   if c["status"] == "TRADING" and c["base"] in chosen and c["quote"] in chosen}
        for sym in markets:
            if sym not in self.fees:
                self.fees[sym] = await asyncio.to_thread(self.api.taker_fee, sym)
        universe = Universe(markets, self.settings["max_legs"])
        old = set(self.universe.markets) if self.universe else set()
        self.universe = universe
        for sym in list(self.books):
            if sym not in markets:
                del self.books[sym]
        if set(markets) != old and self._ws_restart is not None:
            self._ws_restart.set()
        idle = [a for a in self.settings["assets"] if a not in universe.assets]
        triangular = universe.routes_by_asset()
        isolated = [a for a in universe.assets if not triangular[a]]
        fees = ", ".join(f"{s} {self.fees[s] * 100:.3f}%" for s in markets)
        self.log("info", f"Universo: {len(markets)} mercados activos, {len(universe.assets)} activos, "
                         f"{len(universe.cycles)} rutas (hasta {universe.max_legs} patas). Comisiones taker: {fees}")
        if idle:
            self.log("warn", "Sin mercados activos en Binance entre los activos elegidos: "
                             + ", ".join(DISPLAY.get(a, a) for a in idle))
        if isolated:
            self.log("warn", "Con un solo mercado activo, solo hay ida y vuelta (sin rutas de 3+ patas): "
                             + ", ".join(DISPLAY.get(a, a) for a in isolated))

    def coverage(self):
        """Estado de cada activo candidato para el panel."""
        routes = self.universe.routes_by_asset() if self.universe else {}
        rows = []
        for a, desc in CANDIDATE_ASSETS.items():
            mk = [(sym, c["status"], c["quote"] if c["base"] == a else c["base"])
                  for sym, c in sorted(self.catalog.items()) if a in (c["base"], c["quote"])]
            active = [m for m in mk if m[1] == "TRADING"]
            enabled = a in self.settings["assets"]
            in_universe = bool(self.universe and a in self.universe.assets)
            status = ("activa" if routes.get(a) else
                      "solo ida y vuelta (1 mercado)" if in_universe else
                      "desactivada" if not enabled and active else
                      "en pausa en Binance" if mk else "no cotiza en Binance")
            rows.append({"asset": a, "name": DISPLAY.get(a, a), "desc": desc, "enabled": enabled, "status": status,
                         "routes": routes.get(a, 0),
                         "markets": [{"symbol": sym, "status": st, "vs": DISPLAY.get(o, o)} for sym, st, o in mk]})
        return rows

    # ---------------------------------------------------------------- mercado
    def _set_book(self, symbol, bids, asks, update_id):
        self.books[symbol] = {
            "bids": [(float(p), float(q)) for p, q in bids],
            "asks": [(float(p), float(q)) for p, q in asks],
            "ts": time.time(),
            "update_id": update_id,
        }

    async def _ws_loop(self):
        delay = 1
        while True:
            while self.universe is None:
                await asyncio.sleep(0.5)
            self._ws_restart.clear()
            markets = self.universe.markets
            streams = "/".join(f"{s.lower()}@depth20@100ms" for s in markets)
            url = f"{self.env['ws_url']}/stream?streams={streams}"
            restart = False
            try:
                # close_timeout corto: al cambiar el universo no se esperan 10 s al cierre ordenado del stream viejo.
                async with websockets.connect(url, open_timeout=15, ping_interval=20, close_timeout=1,
                                              max_size=2**22) as ws:
                    self.ws_connected, delay = True, 1
                    self.log("info", f"Stream de libros de órdenes conectado (depth20 @100ms, {len(markets)} mercados)")
                    async for raw in ws:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        symbol = msg.get("stream", "").split("@")[0].upper()
                        if symbol in markets and "bids" in data:
                            self._set_book(symbol, data["bids"], data["asks"], data.get("lastUpdateId"))
                            self.last_ws_msg = time.time()
                        if self._ws_restart.is_set():
                            restart = True
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log("warn", f"Stream desconectado ({exc}). Reconectando en {delay}s")
            self.ws_connected = False
            if restart:
                continue  # el universo ha cambiado: reconectar ya con los nuevos mercados
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _resync_loop(self):
        """Los mercados poco líquidos solo envían cambios: si llevan tiempo quietos se refrescan por REST."""
        while True:
            for symbol in list(self.universe.markets) if self.universe else []:
                book = self.books.get(symbol)
                if book is None or time.time() - book["ts"] > RESYNC_AFTER_S:
                    try:
                        d = await asyncio.to_thread(self.api.depth, symbol, 20)
                        current = self.books.get(symbol)
                        if (self.universe and symbol in self.universe.markets
                                and (current is None or time.time() - current["ts"] > RESYNC_AFTER_S)):
                            self._set_book(symbol, d["bids"], d["asks"], d.get("lastUpdateId"))
                    except Exception as exc:  # noqa: BLE001
                        self.log("warn", f"No se pudo refrescar {symbol} por REST: {exc}")
            await asyncio.sleep(10)

    # ---------------------------------------------------------------- cuenta
    async def refresh_account(self):
        data = await asyncio.to_thread(self.api.account)
        balances = {}
        for b in data.get("balances", []):
            total = float(b["free"]) + float(b["locked"])
            if total > 0:
                balances[b["asset"]] = {"free": float(b["free"]), "locked": float(b["locked"])}
        self.account.update(balances=balances, updated=time.time(), can_trade=data.get("canTrade"))
        self.api_ok = True
        await self._price_extras()
        self._value_account()
        return self.account

    async def _price_extras(self):
        """Valora las monedas en cartera que no están en el universo (FDUSD, U, …) con el precio medio por REST.

        Sin esto, esas monedas se valoraban a 0 y el valor de la cuenta salía por debajo del real.
        """
        prices = self.prices()
        extras = {}
        for asset, b in self.account["balances"].items():
            if asset in prices or b["free"] + b["locked"] <= 1e-9:
                continue
            for sym, c in self.catalog.items():
                if c["status"] != "TRADING" or asset not in (c["base"], c["quote"]):
                    continue
                other = c["quote"] if c["base"] == asset else c["base"]
                if other not in prices:
                    continue
                try:
                    bid, ask = await asyncio.to_thread(self.api.book_ticker, sym)
                except Exception:  # noqa: BLE001 - se reintenta en la próxima lectura
                    continue
                m = (bid + ask) / 2
                extras[asset] = prices[other] * (m if c["base"] == asset else 1 / m)
                break
        self.extra_prices = extras

    def all_prices(self):
        return {**self.prices(), **self.extra_prices}

    def _value_account(self):
        prices = self.all_prices()
        if not self.universe or any(a not in prices for a in self.universe.assets):
            return
        total, unvalued = 0.0, []
        for asset, b in self.account["balances"].items():
            qty = b["free"] + b["locked"]
            if asset in prices:
                total += qty * prices[asset]
            elif qty > 1e-6:
                unvalued.append(asset)   # no se suma como 0 en silencio: se avisa en el panel
        self.account["total_usdt"] = total
        self.account["unvalued"] = unvalued

    def snapshot_equity(self):
        self._value_account()
        if self.account["total_usdt"] is None:
            return
        prices = self.all_prices()
        balances = {a: {"amount": b["free"] + b["locked"], "usdt": (b["free"] + b["locked"]) * prices.get(a, 0.0)}
                    for a, b in self.account["balances"].items()}
        self.storage.add_equity(self.account["total_usdt"], balances)
        self.last_equity_snapshot = time.time()

    async def _account_loop(self):
        while True:
            await asyncio.sleep(ACCOUNT_INTERVAL_S)
            if not self.ready or self.executing:
                continue
            try:
                await self.refresh_account()
            except Exception as exc:  # noqa: BLE001
                self.api_ok = False
                self.log("warn", f"No se pudo leer la cuenta: {exc}")
                continue
            if time.time() - self.last_equity_snapshot >= EQUITY_SNAPSHOT_S:
                self.snapshot_equity()
            self._check_session_loss()

    def _check_session_loss(self):
        if not (self.running and self.session_start and self.account["total_usdt"]):
            return
        if self.settings["mode"] != "live":
            return
        start = self.session_start["equity"]
        loss_pct = (start - self.account["total_usdt"]) / start * 100
        if loss_pct >= self.settings["max_session_loss_pct"]:
            self.running = False
            self.log("error", f"Bot detenido: la cuenta ha caído {loss_pct:.2f}% desde el inicio de la sesión "
                              f"(límite {self.settings['max_session_loss_pct']}%).")

    # ---------------------------------------------------------------- evaluación
    def evaluate(self):
        now = time.time()
        u = self.universe
        prices = self.prices()
        mids = {s: mid(self.books.get(s)) for s in u.markets} if u else {}
        ws_age_ms = (now - self.last_ws_msg) * 1000 if self.last_ws_msg else None
        market_ok = bool(u and self.ready and self.ws_connected and ws_age_ms is not None and "USDT" in prices
                         and ws_age_ms <= self.settings["max_book_age_ms"])
        routes, best = [], None
        if u and self.ready:
            live = self.settings["mode"] == "live"
            balances = self.account["balances"]
            for cycle in u.cycles:
                source = cycle[0]
                missing = next((s for s in (u.edge_market(a, b)[0] for a, b in zip(cycle, cycle[1:]))
                                if not mids.get(s)), None)
                if missing or any(a not in prices for a in cycle):
                    routes.append({"route": " → ".join(cycle), "cycle": cycle, "source": source,
                                   "legs_n": len(cycle) - 1, "theo_bps": None, "size": None, "eligible": False,
                                   "ok": False, "exec_bps": None, "value_bps": None,
                                   "reason": f"{missing or 'precio'}: sin libro todavía", "sim": None})
                    continue
                size = self.settings["trade_size_usd"] / prices[source]
                eligible = source in self.settings["sources"]
                if live and eligible:
                    free = balances.get(source, {}).get("free", 0.0)
                    if free * prices[source] < 6:
                        eligible = False
                    else:
                        size = min(size, free * 0.999)
                sim = simulate_cycle(cycle, size, self.books, self.api.filters, self.fees, prices, u)
                row = {
                    "route": " → ".join(cycle), "cycle": cycle, "source": source, "legs_n": len(cycle) - 1,
                    "theo_bps": theoretical_bps(cycle, mids, self.fees, u), "size": size,
                    "eligible": eligible, "ok": sim["ok"],
                    "exec_bps": sim.get("exec_bps"), "value_bps": sim.get("value_bps"),
                    "reason": sim.get("reason"), "sim": sim,
                }
                routes.append(row)
                if eligible and sim["ok"] and (best is None or sim["value_bps"] > best["value_bps"]):
                    best = row
        routes.sort(key=lambda r: (r["ok"], r["value_bps"] if r["ok"] else -1e9), reverse=True)
        self.last_eval = {"routes": routes, "best": best, "pegs": stable_pegs(mids, prices, u) if u else {},
                          "market_ok": market_ok,
                          "ws_age_ms": ws_age_ms, "prices": prices, "mids": mids}
        return self.last_eval

    async def _eval_loop(self):
        while True:
            await asyncio.sleep(EVAL_INTERVAL_S)
            try:
                ev = self.evaluate()
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"Error evaluando rutas: {exc}")
                continue
            if self.running and not self.executing and self._may_trade(ev):
                self.executing = True
                asyncio.create_task(self._execute(ev["best"]))

    def _may_trade(self, ev):
        best = ev["best"]
        if not (ev["market_ok"] and best and best["value_bps"] >= self.settings["min_profit_bps"]):
            return False
        now = time.time()
        if now - self.last_trade_ts < self.settings["cooldown_s"]:
            return False
        while self.trade_times and now - self.trade_times[0] > 3600:
            self.trade_times.popleft()
        if len(self.trade_times) >= self.settings["max_trades_per_hour"]:
            return False
        if self.settings["mode"] == "live" and not self.account.get("can_trade"):
            return False
        return True

    # ---------------------------------------------------------------- ejecución
    async def _execute(self, opp):
        try:
            if self.settings["mode"] == "paper":
                self._record_paper(opp)
            else:
                await self._execute_live(opp)
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Error inesperado ejecutando {opp['route']}: {exc}")
        finally:
            self.last_trade_ts = time.time()
            self.trade_times.append(self.last_trade_ts)
            self.executing = False

    def _record_paper(self, opp):
        sim = opp["sim"]
        trade = {
            "ts": time.time(), "mode": "paper", "status": "completado", "route": opp["route"],
            "source": opp["source"], "amount_in": sim["start_amount"], "expected_out": sim["end_amount"],
            "realized_out": sim["end_amount"], "theo_bps": opp["theo_bps"], "expected_bps": sim["value_bps"],
            "realized_bps": sim["value_bps"], "pnl_usdt": sim["pnl_usdt"], "duration_ms": 0.0,
            "legs": [{**leg, "expected_received": leg["received"]} for leg in sim["legs"]],
            "note": "Simulado contra el libro (sin enviar órdenes)",
        }
        self.last_trade_id = self.storage.add_trade(trade)
        self.log("trade", f"[SIMULADO] {opp['route']}: {sim['value_bps']:+.2f} bps "
                          f"({sim['pnl_usdt']:+.4f} USDT sobre {sim['start_amount']:.2f} {opp['source']})")

    async def _execute_live(self, opp):
        sim, cycle, source = opp["sim"], opp["cycle"], opp["source"]
        prices = dict(self.last_eval["prices"])
        t0 = time.perf_counter()
        u = self.universe
        deltas = {a: 0.0 for a in u.assets}
        legs, amount, asset = [], sim["start_amount"], source
        status, note = "completado", ""
        self.log("info", f"Ejecutando {opp['route']} con {amount:.2f} {source} "
                         f"(esperado {sim['value_bps']:+.2f} bps)")
        for i, (frm, to) in enumerate(zip(cycle, cycle[1:])):
            symbol, side = u.edge_market(frm, to)
            try:
                leg = await self._send_leg(symbol, side, frm, to, amount, deltas)
            except Exception as exc:  # noqa: BLE001 - cualquier fallo debe quedar registrado, no solo los de Binance
                if isinstance(exc, BinanceError):
                    note = f"Pata {i + 1} ({symbol} {side}) rechazada: {exc.msg}"
                else:
                    # Error de red o de lectura de la respuesta: la orden pudo llegar a ejecutarse sin confirmarse.
                    note = (f"Pata {i + 1} ({symbol} {side}) con estado incierto ({type(exc).__name__}: {exc}); "
                            f"revisa las órdenes en Binance")
                status = "fallido"
                self.log("error", note)
                if asset != source and self.settings["auto_unwind"]:
                    await self._unwind(asset, amount, source, deltas, legs)
                break
            leg["expected_received"] = sim["legs"][i]["received"]
            leg["expected_avg_price"] = sim["legs"][i]["avg_price"]
            legs.append(leg)
            amount, asset = leg["received"], to
        duration = (time.perf_counter() - t0) * 1000
        start_value = sim["start_amount"] * prices.get(source, 1.0)
        pnl = sum(d * prices.get(a, 0.0) for a, d in deltas.items())
        realized_bps = pnl / start_value * 10_000 if start_value else 0.0
        trade = {
            "ts": time.time(), "mode": "live", "status": status, "route": opp["route"], "source": source,
            "amount_in": sim["start_amount"], "expected_out": sim["end_amount"],
            "realized_out": amount if status == "completado" else None,
            "theo_bps": opp["theo_bps"], "expected_bps": sim["value_bps"], "realized_bps": realized_bps,
            "pnl_usdt": pnl, "duration_ms": duration, "legs": legs, "note": note,
        }
        self.last_trade_id = self.storage.add_trade(trade)
        if status == "completado":
            self.consecutive_failures = 0
            self.log("trade", f"{opp['route']}: realizado {realized_bps:+.2f} bps ({pnl:+.4f} USDT) · "
                              f"esperado {sim['value_bps']:+.2f} bps · {duration:.0f} ms")
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.settings["max_consecutive_failures"]:
                self.running = False
                self.log("error", f"Bot detenido tras {self.consecutive_failures} fallos consecutivos.")
        try:
            await self.refresh_account()
            self.snapshot_equity()
            self._check_session_loss()
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"No se pudo refrescar la cuenta tras el trade: {exc}")

    async def _send_leg(self, symbol, side, frm, to, amount, deltas):
        free = self.account["balances"].get(frm, {}).get("free")
        resp = await asyncio.to_thread(self.api.market_order, symbol, side, amount, free)
        base, quote = self.universe.markets[symbol]
        qty, quote_qty = float(resp["executedQty"]), float(resp["cummulativeQuoteQty"])
        sign = 1 if side == "BUY" else -1
        deltas[base] += sign * qty
        deltas[quote] -= sign * quote_qty
        commissions = collections.defaultdict(float)
        for fill in resp.get("fills", []):
            commissions[fill["commissionAsset"]] += float(fill["commission"])
        for a, c in commissions.items():
            if a in deltas:
                deltas[a] -= c
        received = (qty if side == "BUY" else quote_qty) - commissions.get(to, 0.0)
        spent = quote_qty if side == "BUY" else qty
        # Saldo local aproximado hasta la próxima lectura de la cuenta.
        bal = self.account["balances"]
        bal.setdefault(frm, {"free": 0.0, "locked": 0.0})["free"] -= spent
        bal.setdefault(to, {"free": 0.0, "locked": 0.0})["free"] += received
        return {
            "symbol": symbol, "side": side, "from": frm, "to": to, "order_id": resp.get("orderId"),
            "status": resp.get("status"), "spent": spent, "received": received,
            "avg_price": quote_qty / qty if qty else None, "qty": qty, "quote_qty": quote_qty,
            "fees": dict(commissions), "fills": len(resp.get("fills", [])),
        }

    async def _unwind(self, asset, amount, source, deltas, legs):
        symbol, side = self.universe.edge_market(asset, source)
        try:
            leg = await self._send_leg(symbol, side, asset, source, amount, deltas)
            leg["unwind"] = True
            legs.append(leg)
            self.log("warn", f"Posición deshecha: {amount:.6f} {asset} → {leg['received']:.4f} {source}")
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"No se pudo deshacer la posición en {asset}: {getattr(exc, 'msg', exc)}. Revisa la cuenta.")

    # ---------------------------------------------------------------- control
    def start_bot(self):
        if not self.ready:
            raise RuntimeError("La conexión con Binance aún no está lista.")
        self._value_account()
        if self.account["total_usdt"] is None:
            raise RuntimeError("Aún no se puede valorar la cuenta (faltan precios de mercado). Reintenta en unos segundos.")
        self.running = True
        self.consecutive_failures = 0
        self.session_start = {"ts": time.time(), "equity": self.account["total_usdt"]}
        mode = "ÓRDENES REALES EN DEMO" if self.settings["mode"] == "live" else "simulación (sin órdenes)"
        self.log("info", f"Bot iniciado en modo {mode}. Umbral {self.settings['min_profit_bps']} bps, "
                         f"tamaño {self.settings['trade_size_usd']} USD")

    def stop_bot(self):
        self.running = False
        self.log("info", "Bot detenido por el usuario.")

    async def update_settings(self, new):
        merged = dict(self.settings)
        merged.update(new)
        settings = validate_settings(merged)
        changed = [k for k in settings if settings[k] != self.settings.get(k)]
        if self.running and {"mode", "assets", "max_legs"} & set(changed):
            raise ValueError("Detén el bot antes de cambiar el modo, el universo de activos o las patas máximas.")
        self.settings = settings
        save_settings(settings)
        if changed:
            self.log("info", "Ajustes actualizados: " + ", ".join(f"{k}={settings[k]}" for k in changed))
        if self.ready and {"assets", "max_legs"} & set(changed):
            await self._build_universe()
        return settings

    # ---------------------------------------------------------------- muestras para gráficos
    async def _sample_loop(self):
        while True:
            await asyncio.sleep(1)
            ev = self.last_eval
            ok = [r for r in ev["routes"] if r["ok"]]
            if not ok:
                continue
            best_exec = max(ok, key=lambda r: r["value_bps"])
            theo = [r["theo_bps"] for r in ev["routes"] if r["theo_bps"] is not None]
            sample = {
                "t": time.time(),
                "peg": {k: v["peg_bps"] for k, v in ev["pegs"].items()},
                "gaps": {k: v["gap_bps"] for k, v in ev["pegs"].items() if "gap_bps" in v},
                "best_exec": best_exec["value_bps"], "best_route": best_exec["route"],
                "best_theo": max(theo) if theo else None,
            }
            self.samples.append(sample)
            self._accumulate_minute(sample)

    def _accumulate_minute(self, s):
        minute = int(s["t"] // 60) * 60
        acc = self.minute_acc
        if acc and acc["ts"] != minute:
            self.storage.add_market_minute(acc["ts"], {
                "peg": {k: v / acc["n_peg"][k] for k, v in acc["peg"].items()},
                "gaps": {k: v / acc["n_gap"][k] for k, v in acc["gaps"].items()},
                "best_exec": acc["best_exec"], "best_theo": acc["best_theo"],
                "secs_above": acc["secs_above"], "best_route": acc["best_route"],
            })
            acc = None
        if acc is None:
            acc = {"ts": minute, "n": 0, "gaps": collections.defaultdict(float), "peg": collections.defaultdict(float),
                   "n_gap": collections.Counter(), "n_peg": collections.Counter(), "best_exec": -1e9,
                   "best_theo": -1e9, "secs_above": 0, "best_route": None}
        acc["n"] += 1
        for k, v in s["gaps"].items():
            acc["gaps"][k] += v
            acc["n_gap"][k] += 1
        for k, v in s["peg"].items():
            acc["peg"][k] += v
            acc["n_peg"][k] += 1
        if s["best_exec"] > acc["best_exec"]:
            acc["best_exec"], acc["best_route"] = s["best_exec"], s["best_route"]
        if s["best_theo"] is not None:
            acc["best_theo"] = max(acc["best_theo"], s["best_theo"])
        if s["best_exec"] >= self.settings["min_profit_bps"]:
            acc["secs_above"] += 1
        self.minute_acc = acc

    # ---------------------------------------------------------------- estado para la UI
    def state(self):
        now = time.time()
        ev = self.last_eval
        prices = {**(ev.get("prices") or self.prices()), **self.extra_prices}
        market = {}
        for symbol, (base, quote) in (self.universe.markets.items() if self.universe else []):
            book = self.books.get(symbol)
            if not book or not book["bids"] or not book["asks"]:
                market[symbol] = {"base": base, "quote": quote, "ready": False}
                continue
            bid, ask = book["bids"][0], book["asks"][0]
            m = (bid[0] + ask[0]) / 2
            market[symbol] = {
                "base": base, "quote": quote, "ready": True, "bid": bid[0], "bid_qty": bid[1],
                "ask": ask[0], "ask_qty": ask[1], "mid": m, "spread_bps": (ask[0] - bid[0]) / m * 10_000,
                "age_ms": (now - book["ts"]) * 1000, "bids": book["bids"][:8], "asks": book["asks"][:8],
                "fee": self.fees.get(symbol),
            }
        balances = []
        for asset, b in sorted(self.account["balances"].items()):
            total = b["free"] + b["locked"]
            balances.append({"asset": asset, "free": b["free"], "locked": b["locked"], "total": total,
                             "usdt": total * prices[asset] if asset in prices else None})
        first = self.storage.first_equity()
        equity = self.account["total_usdt"]
        session = None
        if self.session_start and self.session_start["equity"] and equity:
            session = {"ts": self.session_start["ts"], "start": self.session_start["equity"],
                       "pnl": equity - self.session_start["equity"]}
        routes = [{k: v for k, v in r.items() if k not in ("sim", "cycle")} for r in ev["routes"]]
        best = ev.get("best")
        return {
            "now": now,
            "status": {
                "ready": self.ready, "init_error": self.init_error, "running": self.running,
                "executing": self.executing, "mode": self.settings["mode"], "ws_connected": self.ws_connected,
                "ws_age_ms": ev.get("ws_age_ms"), "market_ok": ev.get("market_ok"), "api_ok": self.api_ok,
                "can_trade": self.account.get("can_trade"), "base_url": self.env["base_url"],
            },
            "settings": self.settings,
            "market": market,
            "pegs": ev.get("pegs", {}),
            "routes": routes,
            "universe": {"assets": self.universe.assets if self.universe else [],
                         "cycles": len(self.universe.cycles) if self.universe else 0,
                         "coverage": self.coverage(), "display": DISPLAY},
            "best": {k: v for k, v in best.items() if k not in ("sim", "cycle")} if best else None,
            "account": {"balances": balances, "total_usdt": equity, "updated": self.account["updated"],
                        "unvalued": self.account.get("unvalued", [])},
            "kpis": {
                "equity": equity,
                "first_equity": first["total_usdt"] if first else None,
                "first_ts": first["ts"] if first else None,
                "session": session,
                "live": self.storage.trade_stats("live"),
                "paper": self.storage.trade_stats("paper"),
            },
            "sample": self.samples[-1] if self.samples else None,
            "last_trade_id": self.last_trade_id,
            "events": list(self.events)[-40:][::-1],
        }
