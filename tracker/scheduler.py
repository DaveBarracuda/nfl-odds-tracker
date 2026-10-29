"""Decide cuándo toca snapshot y cuándo toca el siguiente.

El job del workflow vive ~5.5 h y duerme exactamente hasta el instante que marca
`due_at`, así que la cadencia la fija este módulo y no el reloj del cron (el cron de
GitHub Actions se retrasa horas: medido, huecos de 124 a 444 min).

La cadencia se aprieta conforme se acerca el kickoff, porque lo que vale para el
modelo de picks es la línea de cierre:

    más de 2 h antes   → cada 55 min   (horario)
    2 h antes o menos  → cada 25 min   (pregame)
    30 min antes o menos → cada 9 min   (cierre)
"""
from datetime import datetime, timedelta

from . import config
from .db import parse_iso

# (nombre, minutos antes del kickoff, espaciado mínimo en minutos). De la ventana
# más estrecha a la más amplia: gana la primera que contenga al kickoff.
TIERS = (
    ("cierre", config.CLOSING_WINDOW_MIN, config.CLOSING_GAP_MIN),
    ("pregame", config.PREGAME_WINDOW_MIN, config.PREGAME_GAP_MIN),
)


def _tier(to_kickoff: timedelta) -> tuple[int, str]:
    """Espaciado exigido y nombre del nivel según lo que falte para el kickoff."""
    for name, window, gap in TIERS:
        if to_kickoff <= timedelta(minutes=window):
            return gap, name
    return config.HOURLY_GAP_MIN, "horario"


def should_snapshot(now: datetime, events, last) -> tuple[bool, str]:
    """Si toca snapshot en este instante. Se mide contra el último snapshot y no
    contra el reloj, para tolerar un arranque tardío de la corrida."""
    upcoming = [k for k in (parse_iso(e["commence_time"]) for e in events) if k > now]
    if not upcoming:
        return False, "sin partidos próximos"
    gap_min, reason = _tier(min(upcoming) - now)
    if last is None:
        return True, reason
    waited = (now - parse_iso(last["taken_at"])).total_seconds() / 60
    if waited >= gap_min:
        return True, reason
    return False, f"faltan {gap_min - waited:.0f} min ({reason})"


def due_at(now: datetime, events, last, horizon_min: int | None = None) -> datetime | None:
    """Instante en que toca el próximo snapshot, o None si no toca en el horizonte.

    Busca minuto a minuto preguntándole a `should_snapshot`, así el loop del workflow
    y la decisión real nunca pueden discrepar, y se anticipa el cambio de nivel: a
    2 h 10 del kickoff el siguiente snapshot toca al entrar a la ventana pregame, no
    al cumplirse la hora."""
    if should_snapshot(now, events, last)[0]:
        return now
    horizon = config.HOURLY_GAP_MIN + 1 if horizon_min is None else horizon_min
    start = now.replace(second=0, microsecond=0)
    for minute in range(1, horizon + 1):
        moment = start + timedelta(minutes=minute)
        if should_snapshot(moment, events, last)[0]:
            return moment
    return None


def wait_seconds(now: datetime, events, last, known_schedule: bool = True) -> int:
    """Segundos que el loop del workflow debe dormir antes del próximo snapshot.

    `known_schedule` es False cuando no se conoce el calendario (base recién creada):
    entonces hay que correr ya para traerlo, no dormir el ciclo completo."""
    if not known_schedule:
        return 0
    due = due_at(now, events, last)
    if due is None:
        return config.HOURLY_GAP_MIN * 60
    return max(0, round((due - now).total_seconds()))
