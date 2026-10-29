"""Configuración: variables de .env y umbrales de detección."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SPORT = "americanfootball_nfl"
# Hasta 10 casas cuentan como 1 región: 3 créditos por snapshot con 3 mercados.
BOOKMAKERS = _list(
    "BOOKMAKERS",
    "pinnacle,lowvig,betonlineag,draftkings,fanduel,betmgm,espnbet,betrivers,hardrockbet,bovada",
)
SHARP_BOOKS = set(_list("SHARP_BOOKS", "pinnacle,lowvig,betonlineag"))
MARKETS = _list("MARKETS", "h2h,spreads,totals")

BOOK_NAMES = {
    "pinnacle": "Pinnacle",
    "lowvig": "LowVig",
    "betonlineag": "BetOnline",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "espnbet": "ESPN Bet",
    "betrivers": "BetRivers",
    "hardrockbet": "Hard Rock",
    "bovada": "Bovada",
    "williamhill_us": "Caesars",
    "circasports": "Circa",
    "fanatics": "Fanatics",
}
MARKET_NAMES = {"spreads": "Spread", "totals": "Total", "h2h": "Moneyline"}

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/Mexico_City"))
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "7"))
PREGAME_WINDOW_MIN = int(os.environ.get("PREGAME_WINDOW_MIN", "120"))
# Minutos desde el último snapshot para tomar otro. Se mide contra el último snapshot
# y no contra el reloj para tolerar los retrasos del cron de GitHub Actions.
HOURLY_GAP_MIN = int(os.environ.get("HOURLY_GAP_MIN", "55"))
PREGAME_GAP_MIN = int(os.environ.get("PREGAME_GAP_MIN", "25"))
# Ventana de cierre: pegado al kickoff los snapshots se juntan para capturar la línea
# de cierre, que es contra la que se mide el CLV.
CLOSING_WINDOW_MIN = int(os.environ.get("CLOSING_WINDOW_MIN", "30"))
CLOSING_GAP_MIN = int(os.environ.get("CLOSING_GAP_MIN", "9"))
# Hueco a partir del cual se avisa que la cadena de ejecuciones se cortó.
STALE_GAP_MIN = int(os.environ.get("STALE_GAP_MIN", "150"))
# Días de líneas y splits completos. La detección usa 72 h, así que 4 días dan margen.
# No subirlo sin medir: con ~32 snapshots al día, cada día de retención completa pesa
# ~7 MB, y si tracker.db pasa de 100 MB GitHub rechaza el push y la cadena muere.
# El dashboard sigue graficando 7 días: más allá de la ventana completa usa el archivo
# diario, así que la serie queda densa en lo reciente y dispersa en lo viejo.
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "4"))
# Días de archivo (toda la temporada): pasado KEEP_DAYS se conservan apertura, cierre y un
# snapshot diario por partido; alertas y eventos se conservan completos.
ARCHIVE_DAYS = int(os.environ.get("ARCHIVE_DAYS", "200"))
LOW_CREDITS_WARNING = int(os.environ.get("LOW_CREDITS_WARNING", "100"))
# Horas tras las que un partido de la base se considera fantasma: `tracker run` refresca
# el calendario en cada pasada, así que algo sin refrescar ya no lo devuelve la API.
EVENT_FRESH_HOURS = int(os.environ.get("EVENT_FRESH_HOURS", "3"))

DB_PATH = Path(os.environ.get("DB_PATH", ROOT / "data" / "tracker.db"))
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"
# Copias que descarga `python -m tracker sync` desde la rama state de GitHub.
REMOTE_DASHBOARD_PATH = ROOT / "dashboard" / "remote.html"
REMOTE_DB_PATH = ROOT / "data" / "remote.db"
LOG_PATH = ROOT / "logs" / "tracker.log"

# Umbrales por mercado. Spread y total en puntos; moneyline en puntos
# porcentuales de probabilidad implícita sin vig del local.
THRESHOLDS = {
    "spreads": {"move": 0.5, "info": 1.5, "gradual_total": 1.0, "outlier": 1.5, "step": 0.25,
                "reversion": 1.0},
    "totals": {"move": 0.5, "info": 2.0, "gradual_total": 1.5, "outlier": 2.0, "step": 0.25,
               "reversion": 1.5},
    "h2h": {"move": 2.0, "info": 6.0, "gradual_total": 4.0, "outlier": 5.0, "step": 1.0,
            "reversion": 4.0},
    # Vapor: al menos N casas se mueven en la misma dirección entre dos snapshots cercanos.
    "steam_min_books": 3,
    "steam_max_opposite": 1,
    "steam_max_gap_min": 75,
    # Información: movimiento grande del consenso con la mayoría de las casas.
    "info_min_book_share": 0.7,
    # RLM: la línea se mueve contra el lado con al menos este % de tickets.
    "rlm_min_tickets": 60,
    # Gradual: pasos pequeños en la misma dirección dentro de la ventana.
    "gradual_window_hours": 72,
    "gradual_min_steps": 3,
    "gradual_min_hours": 3,
    # Apertura: casas mínimas para fijar la apertura del consenso. "reversion" (por
    # mercado) es el recorrido mínimo de ida y vuelta para avisar que la línea
    # regresó a su punto de partida.
    "open_min_books": 3,
    # Ruido: cambios de juice sin cambio de línea a partir de estos centavos.
    "noise_juice_cents": 10,
    "key_numbers": [3, 7, 10, 6, 4, 14],
}
