"""App web local: sirve el panel y expone el estado del bot.

Run:  python bot/server.py  and open http://127.0.0.1:8765
"""

import asyncio
import csv
import io
import json
import sys
import time
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bot import Engine
from config import ROOT

for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HOST, PORT = "127.0.0.1", 8765
engine = Engine()


@asynccontextmanager
async def lifespan(_app):
    await engine.start()
    yield
    engine.running = False
    engine.snapshot_equity()
    await engine.stop()


app = FastAPI(title="Bot de arbitraje · Binance demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    return engine.state()


@app.get("/api/trades")
def trades(limit: int = 500, mode: str | None = None):
    return engine.storage.trades(limit=limit, mode=mode)


@app.get("/api/equity")
def equity(hours: float = 24 * 30):
    return engine.storage.equity(since=time.time() - hours * 3600)


@app.get("/api/market_history")
def market_history(hours: float = 24):
    return {
        "minutes": engine.storage.market_minutes(since=time.time() - hours * 3600),
        "seconds": list(engine.samples),
    }


@app.post("/api/bot/start")
def start_bot():
    try:
        engine.start_bot()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"running": True}


@app.post("/api/bot/stop")
def stop_bot():
    engine.stop_bot()
    return {"running": False}


@app.post("/api/settings")
async def update_settings(new: dict = Body(...)):
    try:
        return await engine.update_settings(new)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/account/refresh")
async def refresh_account():
    try:
        await engine.refresh_account()
        engine.snapshot_equity()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)) from exc
    return engine.state()["account"]


@app.get("/api/export/trades.csv")
def export_trades():
    rows = engine.storage.trades(limit=1_000_000)
    buf = io.StringIO()
    fields = ["id", "ts", "mode", "status", "route", "source", "amount_in", "expected_out", "realized_out",
              "theo_bps", "expected_bps", "realized_bps", "pnl_usdt", "duration_ms", "note", "legs"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        r = dict(r)
        r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        r["legs"] = json.dumps(r["legs"], ensure_ascii=False)
        writer.writerow(r)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trades_arbitraje.csv"})


@app.get("/api/export/equity.csv")
def export_equity():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["fecha", "total_usdt", "balances"])
    for r in engine.storage.equity():
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["total_usdt"],
                         json.dumps(r["balances"])])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=evolucion_cuenta.csv"})


@app.websocket("/ws")
async def ws_state(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(engine.state(), default=str))
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not args.no_browser:
        import threading
        threading.Timer(2.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
