"""Decide si toca snapshot. El workflow corre cada 30 minutos (:07 y :37) y aquí se
toma snapshot si pasaron ≥ 55 min desde el último, o ≥ 25 min cuando un partido
arranca en ≤ 2 h. Se compara contra el último snapshot y no contra el reloj porque
los cron de GitHub Actions pueden retrasarse varios minutos."""
from datetime import datetime, timedelta

from . import config
from .db import parse_iso

RUN_MINUTES = (7, 37)


def should_snapshot(now: datetime, events, last) -> tuple[bool, str]:
    upcoming = [k for k in (parse_iso(e["commence_time"]) for e in events) if k > now]
    if not upcoming:
        return False, "sin partidos próximos"
    gap = None if last is None else now - parse_iso(last["taken_at"])
    if min(upcoming) - now <= timedelta(minutes=config.PREGAME_WINDOW_MIN):
        if gap is None or gap >= timedelta(minutes=config.PREGAME_GAP_MIN):
            return True, "pregame"
        return False, "snapshot reciente"
    if gap is None or gap >= timedelta(minutes=config.HOURLY_GAP_MIN):
        return True, "horario"
    return False, "no ha pasado una hora desde el último snapshot"


def next_slot(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    candidates = [base.replace(minute=m) + timedelta(hours=h) for h in (0, 1) for m in RUN_MINUTES]
    return min(c for c in candidates if c > now)
