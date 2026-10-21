"""Configuración del bot: credenciales (.env) y parámetros editables (data/settings.json)."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Directorio de datos locales (ajustes y SQLite); se puede redirigir con ARB_BOT_DATA_DIR (p. ej. en los tests).
DATA_DIR = Path(os.environ.get("ARB_BOT_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "bot.db"

# Activos que el bot puede usar (código de Binance -> descripción). Los mercados no se fijan aquí: al arrancar se
# descubren en exchangeInfo todos los pares activos (TRADING) entre los activos elegidos en los ajustes.
CANDIDATE_ASSETS = {
    "USDT": "Tether",
    "USDC": "USD Coin",
    "USD": "Dólar estadounidense",
    "BTC": "Bitcoin (puente del estudio)",
    "USD1": "World Liberty Financial USD",
    "USDS": "Sky Dollar (antiguo DAI)",
    "DAI": "DAI (sustituido por USDS)",
    "USDE": "Ethena USDe",
    "RLUSD": "Ripple USD",
    "PYUSD": "PayPal USD",
    "USDG": "Global Dollar",
    "GHO": "Aave GHO",
    "BUSD": "Binance USD (retirada)",
    "FDUSD": "First Digital USD (puente opcional)",
    "U": "United Stables U (puente opcional)",
}
ASSETS = list(CANDIDATE_ASSETS)
ASSET_ORDER = ASSETS                 # orden de presentación
DISPLAY = {"USDE": "USDe"}           # nombres con mayúsculas distintas a Binance

DEFAULT_SETTINGS = {
    # "paper": solo detecta y registra oportunidades; "live": envía órdenes a la cuenta demo.
    "mode": "paper",
    "trade_size_usd": 100.0,        # Tamaño por ciclo, en USD equivalentes
    "min_profit_bps": 2.0,          # Beneficio neto mínimo ejecutable (tras comisiones y profundidad)
    "sources": ["USDT", "USDC"],    # Activos desde los que puede arrancar un ciclo
    "cooldown_s": 5.0,              # Espera mínima entre ciclos
    "max_trades_per_hour": 60,
    "max_book_age_ms": 2000,        # Antigüedad máxima del stream de libros para operar
    "max_session_loss_pct": 1.0,    # Para el bot si la cuenta cae este % desde que se arrancó
    "max_consecutive_failures": 3,
    "auto_unwind": True,            # Si falla una pata, vuelve al activo de origen
    # Universo de activos: los cuatro del estudio + las stablecoins añadidas. FDUSD y U, desactivadas.
    "assets": ["USDT", "USDC", "USD", "BTC", "USD1", "USDS", "DAI", "USDE", "RLUSD", "PYUSD", "USDG", "GHO", "BUSD"],
    "max_legs": 4,                  # Patas máximas por ciclo (el número de rutas crece muy rápido con esto)
}


def load_env():
    # Credenciales: .env en esta carpeta o en la raíz del repositorio (nunca se versiona; ver .env.example).
    env_path = next((p for p in (ROOT / ".env", ROOT.parent / ".env") if p.exists()), ROOT / ".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    return {
        "api_key": os.environ.get("BINANCE_API_KEY", ""),
        "api_secret": os.environ.get("BINANCE_API_SECRET", ""),
        "base_url": os.environ.get("BINANCE_BASE_URL", "https://demo-api.binance.com"),
        "ws_url": os.environ.get("BINANCE_WS_URL", "wss://demo-stream.binance.com"),
    }


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return settings


def validate_settings(new):
    """Devuelve una copia saneada de los ajustes o lanza ValueError."""
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in new.items() if k in DEFAULT_SETTINGS})
    if s["mode"] not in ("paper", "live"):
        raise ValueError("El modo debe ser 'paper' o 'live'.")
    s["trade_size_usd"] = float(s["trade_size_usd"])
    if not 6 <= s["trade_size_usd"] <= 100_000:
        raise ValueError("El tamaño por ciclo debe estar entre 6 y 100.000 USD.")
    s["min_profit_bps"] = float(s["min_profit_bps"])
    s["cooldown_s"] = max(0.5, float(s["cooldown_s"]))
    s["max_trades_per_hour"] = max(1, int(s["max_trades_per_hour"]))
    s["max_book_age_ms"] = max(200, int(s["max_book_age_ms"]))
    s["max_session_loss_pct"] = max(0.01, float(s["max_session_loss_pct"]))
    s["max_consecutive_failures"] = max(1, int(s["max_consecutive_failures"]))
    s["auto_unwind"] = bool(s["auto_unwind"])
    s["assets"] = [a for a in ASSETS if a in s["assets"]]
    if "USDT" not in s["assets"]:
        raise ValueError("USDT tiene que estar en el universo: es la moneda de valoración.")
    s["max_legs"] = int(s["max_legs"])
    if not 2 <= s["max_legs"] <= 5:
        raise ValueError("Las patas por ciclo deben estar entre 2 y 5.")
    s["sources"] = [a for a in s["sources"] if a in s["assets"]]
    if not s["sources"]:
        raise ValueError("Selecciona al menos un activo de origen que esté en el universo.")
    return s


def save_settings(settings):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
