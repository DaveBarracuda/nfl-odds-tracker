"""Primitivas de mercado: valor de cada casa, consenso y formato.

Valor normalizado de una casa: spread = puntos del local; total = puntos;
moneyline = probabilidad implícita sin vig del local (en %)."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from . import config

EPS = 1e-6
PRIORITY = {"info": 5, "reversion": 4, "steam": 3, "rlm": 2, "gradual": 1, "noise": 0}
KIND_NAMES = {
    "rlm": "RLM", "steam": "Vapor", "gradual": "Gradual",
    "info": "Información", "reversion": "Retorno", "noise": "Ruido",
}
UNITS = {"spreads": "pts", "totals": "pts", "h2h": "pp"}
OPPOSITE = {"home": "away", "away": "home", "over": "under", "under": "over"}


@dataclass
class Signal:
    kind: str
    event_id: str
    market: str
    strength: int
    from_value: float | None
    to_value: float | None
    summary: str
    details: dict = field(default_factory=dict)
    silenced: bool = False
    dedupe_key: str = ""


def clamp(value: float) -> int:
    return int(max(1, min(100, round(value))))


def implied(price: int) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def juice_cents(price: int) -> int:
    """Escala lineal de juice: -110 → -10, +105 → 5."""
    return price - 100 if price >= 100 else price + 100


def book_value(market: str, outcomes: dict) -> tuple[float, int | None] | None:
    """(valor, precio de referencia) de una casa. outcomes: lado → (point, price)."""
    if market == "spreads":
        if outcomes.get("home") and outcomes["home"][0] is not None:
            return float(outcomes["home"][0]), outcomes["home"][1]
        if outcomes.get("away") and outcomes["away"][0] is not None:
            return -float(outcomes["away"][0]), None
    elif market == "totals":
        side = outcomes.get("over") or outcomes.get("under")
        if side and side[0] is not None:
            return float(side[0]), outcomes["over"][1] if "over" in outcomes else None
    elif market == "h2h":
        if "home" in outcomes and "away" in outcomes:
            home, away = implied(outcomes["home"][1]), implied(outcomes["away"][1])
            return round(100 * home / (home + away), 2), outcomes["home"][1]
    return None


def is_discrete(market: str) -> bool:
    """Spread y total se mueven en líneas reales; moneyline es continuo."""
    return market != "h2h"


def consensus(books: dict, outlier: float, discrete: bool = False) -> tuple[float | None, set]:
    """Línea de consenso, excluyendo las casas que se alejan `outlier` o más de la mediana.

    Con `discrete` (spread y total) es la línea más repetida; en empate gana la que
    tienen más casas sharp y luego la más cercana a la mediana. Así el consenso
    siempre es una línea real (-3.5, no -3.75). En moneyline es la mediana."""
    values = [v for v, _ in books.values()]
    if not values:
        return None, set()
    median = statistics.median(values)
    outliers = set()
    if len(values) >= 4:
        outliers = {b for b, (v, _) in books.items() if abs(v - median) >= outlier - EPS}
    kept = {b: v for b, (v, _) in books.items() if b not in outliers} or {b: v for b, (v, _) in books.items()}
    median = statistics.median(kept.values())
    if not discrete:
        return round(median, 2), outliers
    counts: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    for book, value in kept.items():
        counts[value][0] += 1
        counts[value][1] += book in config.SHARP_BOOKS
    best = max(counts.items(), key=lambda item: (item[1][0], item[1][1], -abs(item[0] - median), -item[0]))
    return best[0], outliers


def market_consensus(market: str, books: dict) -> tuple[float | None, set]:
    """Consenso con el umbral de discrepancia y la escala propios del mercado."""
    return consensus(books, config.THRESHOLDS[market]["outlier"], discrete=is_discrete(market))


def short(team: str | None) -> str:
    return team.split()[-1] if team else ""


def book_name(key: str) -> str:
    return config.BOOK_NAMES.get(key, key)


def fmt_value(market: str, value: float | None, home: str) -> str:
    if value is None:
        return "—"
    if market == "spreads":
        return f"{short(home)} {'PK' if abs(value) < EPS else f'{value:+g}'}"
    if market == "totals":
        return f"{value:g}"
    return f"{short(home)} {value:.1f}%"


def fmt_delta(market: str, delta: float) -> str:
    return f"{delta:+.1f} pp" if market == "h2h" else f"{delta:+g} pts"


def books_phrase(count: int) -> str:
    """'1 casa' / '3 casas', para que los textos concuerden en número."""
    return f"{count} casa" if count == 1 else f"{count} casas"


def fmt_magnitude(market: str, value: float) -> str:
    """Tamaño del recorrido, sin signo: la dirección se dice aparte."""
    return f"{abs(value):.1f} pp" if market == "h2h" else f"{abs(value):g} pts"


def move_side(market: str, delta: float) -> str:
    """Lado hacia el que se movió el mercado."""
    if market == "totals":
        return "over" if delta > 0 else "under"
    if market == "spreads":
        return "home" if delta < 0 else "away"
    return "home" if delta > 0 else "away"


def side_label(side: str, home: str, away: str) -> str:
    return {"home": short(home), "away": short(away), "over": "Over", "under": "Under"}[side]


def key_number_hit(prev: float, cur: float) -> int | None:
    lo, hi = sorted((abs(prev), abs(cur)))
    if prev * cur < 0:
        lo = 0.0
    if hi - lo < EPS:
        return None
    for key in config.THRESHOLDS["key_numbers"]:
        if lo - EPS <= key <= hi + EPS:
            return key
    return None
