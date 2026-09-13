"""Clasificación de movimientos de línea.

Para cada partido y mercado se compara el snapshot actual contra el anterior:
- Información: el consenso salta mucho y casi todas las casas se mueven juntas.
- Vapor: 3+ casas se mueven en la misma dirección entre snapshots cercanos.
- RLM: el consenso se mueve contra el lado que tiene la mayoría de los tickets.
- Gradual: pasos pequeños y consecutivos en la misma dirección durante horas.
- Ruido: movimientos sin confirmación (1-2 casas, direcciones mixtas, solo juice)
  y casas fuera de consenso. Se guardan pero no se envían a Telegram.

Valor de cada casa: spread = puntos del local; total = puntos; moneyline =
probabilidad implícita sin vig del local (en %)."""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from .db import iso, parse_iso

EPS = 1e-6
PRIORITY = {"info": 4, "steam": 3, "rlm": 2, "gradual": 1, "noise": 0}
KIND_NAMES = {"rlm": "RLM", "steam": "Vapor", "gradual": "Gradual", "info": "Información", "noise": "Ruido"}
UNITS = {"spreads": "pts", "totals": "pts", "h2h": "pp"}
OPPOSITE = {"home": "away", "away": "home", "over": "under", "under": "over"}
SPLIT_SOURCES = ("actionnetwork", "dknetwork")
SPLIT_SOURCE_NAMES = {"actionnetwork": "Action Network", "dknetwork": "DK Network"}


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
    counts = defaultdict(lambda: [0, 0])
    for book, value in kept.items():
        counts[value][0] += 1
        counts[value][1] += book in config.SHARP_BOOKS
    best = max(counts.items(), key=lambda item: (item[1][0], item[1][1], -abs(item[0] - median), -item[0]))
    return best[0], outliers


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


def load_history(conn: sqlite3.Connection, since: str, until_id: int | None = None):
    """snaps: id → datetime; history: (evento, mercado) → snapshot → casa → (valor, precio)."""
    query = "SELECT id, taken_at FROM snapshots WHERE error IS NULL AND taken_at >= ?"
    params: list = [since]
    if until_id is not None:
        query += " AND id <= ?"
        params.append(until_id)
    snaps = {r["id"]: parse_iso(r["taken_at"]) for r in conn.execute(query, params)}
    history = defaultdict(lambda: defaultdict(dict))
    if not snaps:
        return snaps, history
    raw = defaultdict(dict)
    for r in conn.execute(
        "SELECT snapshot_id, event_id, book, market, outcome, point, price FROM lines "
        "WHERE snapshot_id BETWEEN ? AND ?",
        (min(snaps), max(snaps)),
    ):
        if r[0] in snaps:
            raw[(r[1], r[3], r[0], r[2])][r[4]] = (r[5], r[6])
    for (event_id, market, snapshot_id, book), outcomes in raw.items():
        value = book_value(market, outcomes)
        if value is not None:
            history[(event_id, market)][snapshot_id][book] = value
    return snaps, history


def load_splits(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """Splits más recientes hasta el snapshot: (evento, mercado) → fuente → lado → datos."""
    result = defaultdict(dict)
    rows = conn.execute(
        """SELECT s.* FROM splits s
           JOIN (SELECT event_id, source, market, MAX(snapshot_id) AS sid FROM splits
                 WHERE snapshot_id <= ? GROUP BY event_id, source, market) latest
             ON s.event_id = latest.event_id AND s.source = latest.source
            AND s.market = latest.market AND s.snapshot_id = latest.sid""",
        (snapshot_id,),
    )
    for r in rows:
        result[(r["event_id"], r["market"])].setdefault(r["source"], {})[r["side"]] = {
            "tickets": r["tickets_pct"], "money": r["money_pct"], "line": r["line"],
        }
    return result


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def splits_summary(data: dict, market: str, home: str, away: str):
    for source in SPLIT_SOURCES:
        sides = data.get(source)
        if not sides:
            continue
        parts = []
        for side in (("over", "under") if market == "totals" else ("away", "home")):
            v = sides.get(side) or {}
            if v.get("tickets") is None and v.get("money") is None:
                continue
            parts.append(f"{side_label(side, home, away)} {_pct(v.get('tickets'))} tickets / {_pct(v.get('money'))} dinero")
        if parts:
            return source, sides, f"Splits ({SPLIT_SOURCE_NAMES[source]}): " + " · ".join(parts)
    return None, None, None


def analyze(conn: sqlite3.Connection, snapshot_id: int) -> list[Signal]:
    row = conn.execute("SELECT taken_at FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        return []
    now = parse_iso(row["taken_at"])
    window = timedelta(hours=config.THRESHOLDS["gradual_window_hours"])
    snaps, history = load_history(conn, iso(now - window), until_id=snapshot_id)
    events = {r["id"]: r for r in conn.execute("SELECT id, home, away, commence_time FROM events")}
    splits = load_splits(conn, snapshot_id)
    signals = []
    for (event_id, market), by_snap in history.items():
        if snapshot_id in by_snap and event_id in events and market in config.THRESHOLDS:
            signals.extend(analyze_market(
                events[event_id], market, by_snap, snaps, snapshot_id, splits.get((event_id, market), {}),
            ))
    return signals


def analyze_market(event, market: str, by_snap: dict, snaps: dict[int, datetime], sid: int, splits: dict) -> list[Signal]:
    T = config.THRESHOLDS
    th = T[market]
    eid, home, away = event["id"], event["home"], event["away"]

    def fmt(value):
        return fmt_value(market, value, home)

    ids = sorted(by_snap)
    cons, outliers = {}, {}
    for s in ids:
        cons[s], outliers[s] = consensus(by_snap[s], th["outlier"], discrete=market != "h2h")
    cur = by_snap[sid]
    split_source, split_data, split_text = splits_summary(splits, market, home, away)
    signals: list[Signal] = []

    for book in sorted(outliers[sid]):
        value = cur[book][0]
        signals.append(Signal(
            "noise", eid, market, 15, cons[sid], value,
            f"{book_name(book)} fuera de consenso: {fmt(value)} contra {fmt(cons[sid])}. "
            "Posible error del feed o movimiento aislado; verificar antes de actuar.",
            {"reason": "discrepancia", "book": book, "unit": UNITS[market]},
            silenced=True, dedupe_key=f"outlier|{eid}|{market}|{book}|{value}",
        ))

    earlier = [s for s in ids if s < sid]
    if not earlier:
        return signals
    pid = earlier[-1]
    prev = by_snap[pid]
    gap = (snaps[sid] - snaps[pid]).total_seconds() / 60
    excluded = outliers[sid] | outliers[pid]
    deltas = {b: cur[b][0] - prev[b][0] for b in cur if b in prev and b not in excluded}
    up = sorted(b for b, d in deltas.items() if d >= th["move"] - EPS)
    down = sorted(b for b, d in deltas.items() if d <= -th["move"] + EPS)
    dom, opp, sign = (up, down, 1) if len(up) >= len(down) else (down, up, -1)
    n_books = len(deltas)
    cons_delta = round(cons[sid] - cons[pid], 2)
    fast = gap <= T["steam_max_gap_min"]

    sharp_now = [b for b in dom if b in config.SHARP_BOOKS]
    sharp_first = []
    if dom and len(earlier) >= 2:
        before = by_snap[earlier[-2]]
        sharp_first = sorted(
            b for b in config.SHARP_BOOKS
            if b in prev and b in before and sign * (prev[b][0] - before[b][0]) >= th["move"] - EPS
        )
    base = {
        "unit": UNITS[market], "gap_min": round(gap), "books": n_books,
        "consensus_from": cons[pid], "consensus_to": cons[sid], "consensus_delta": cons_delta,
        "movers": [{"book": b, "from": prev[b][0], "to": cur[b][0]} for b in dom],
        "sharp_movers": sharp_now, "sharp_first": sharp_first,
        "splits_source": split_source, "splits_text": split_text,
    }
    primary = None

    if (fast and n_books >= 3 and abs(cons_delta) >= th["info"] - EPS
            and len(dom) / n_books >= T["info_min_book_share"]):
        primary = "info"
        key = key_number_hit(cons[pid], cons[sid]) if market == "spreads" else None
        strength = 70 + 20 * (abs(cons_delta) / th["info"] - 1) + (10 if key else 0) + (5 if sharp_now else 0)
        text = (f"Salto de {fmt_delta(market, cons_delta)} en {len(dom)} de {n_books} casas en {round(gap)} min "
                f"({fmt(cons[pid])} → {fmt(cons[sid])})")
        if key:
            text += f", pasando por el número clave {key}"
        text += ". Patrón de noticia: revisar lesiones, alineaciones o clima."
        signals.append(Signal("info", eid, market, clamp(strength), cons[pid], cons[sid], text,
                              {**base, "key_number": key}, dedupe_key=f"info|{eid}|{market}|{sid}"))

    elif fast and len(dom) >= T["steam_min_books"] and len(opp) <= T["steam_max_opposite"]:
        primary = "steam"
        f_val = statistics.median(prev[b][0] for b in dom)
        t_val = statistics.median(cur[b][0] for b in dom)
        key = key_number_hit(f_val, t_val) if market == "spreads" else None
        size = abs(t_val - f_val) / th["move"]
        strength = (50 + 8 * (len(dom) - T["steam_min_books"])
                    + (15 if sharp_first else 10 if sharp_now else 0)
                    + 10 * min(size - 1, 2) + (10 if key else 0))
        text = f"{len(dom)} de {n_books} casas movieron {fmt(f_val)} → {fmt(t_val)} en {round(gap)} min"
        if sharp_first:
            verb = "se movió" if len(sharp_first) == 1 else "se movieron"
            text += f"; {', '.join(map(book_name, sharp_first))} {verb} primero"
        elif sharp_now:
            text += f"; incluye {', '.join(map(book_name, sharp_now))}"
        if key:
            text += f"; número clave {key}"
        signals.append(Signal("steam", eid, market, clamp(strength), f_val, t_val, text + ".",
                              {**base, "key_number": key}, dedupe_key=f"steam|{eid}|{market}|{sid}"))

    if abs(cons_delta) >= th["move"] - EPS and split_data:
        side = move_side(market, cons_delta)
        other = OPPOSITE[side]
        t_side = (split_data.get(side) or {}).get("tickets")
        t_other = (split_data.get(other) or {}).get("tickets")
        m_side = (split_data.get(side) or {}).get("money")
        if t_other is None and t_side is not None:
            t_other = 100 - t_side
        if t_other is not None and t_other >= T["rlm_min_tickets"]:
            divergence = (m_side - t_side) if m_side is not None and t_side is not None else 0
            strength = (45 + (t_other - T["rlm_min_tickets"]) + 0.5 * max(divergence, 0)
                        + 5 * min(abs(cons_delta) / th["move"], 3) + (10 if primary else 0))
            text = (f"{t_other:.0f}% de los tickets van con {side_label(other, home, away)}, pero la línea se movió "
                    f"hacia {side_label(side, home, away)} ({fmt(cons[pid])} → {fmt(cons[sid])}).")
            if m_side is not None:
                text += f" Dinero en {side_label(side, home, away)}: {m_side:.0f}%."
            signals.append(Signal(
                "rlm", eid, market, clamp(strength), cons[pid], cons[sid], text,
                {**base, "move_side": side, "tickets_public": t_other,
                 "tickets_move_side": t_side, "money_move_side": m_side},
                dedupe_key=f"rlm|{eid}|{market}|{cons[pid]}|{cons[sid]}",
            ))

    if primary != "info" and abs(cons_delta) >= th["step"] - EPS:
        levels: list[tuple[int, float]] = []
        for s in ids:
            if cons[s] is not None and (not levels or abs(cons[s] - levels[-1][1]) >= th["step"] - EPS):
                levels.append((s, cons[s]))
        if levels and levels[-1][0] == sid:
            trail = 0
            for i in range(len(levels) - 1, 0, -1):
                step = levels[i][1] - levels[i - 1][1]
                if step * cons_delta > 0 and abs(step) < th["info"] - EPS:
                    trail += 1
                else:
                    break
            if trail >= T["gradual_min_steps"]:
                start_sid, start_val = levels[-trail - 1]
                total = round(cons[sid] - start_val, 2)
                hours = (snaps[sid] - snaps[start_sid]).total_seconds() / 3600
                if abs(total) >= th["gradual_total"] - EPS and hours >= T["gradual_min_hours"]:
                    strength = 35 + 5 * trail + 10 * min(abs(total) / th["gradual_total"], 3)
                    side = move_side(market, total)
                    text = (f"{trail} ajustes seguidos hacia {side_label(side, home, away)} en {hours:.0f} h: "
                            f"{fmt(start_val)} → {fmt(cons[sid])} ({fmt_delta(market, total)}).")
                    signals.append(Signal(
                        "gradual", eid, market, clamp(strength), start_val, cons[sid], text,
                        {**base, "steps": trail, "hours": round(hours, 1)},
                        dedupe_key=f"gradual|{eid}|{market}|{start_val}|{cons[sid]}",
                    ))

    if not any(s.kind != "noise" for s in signals):
        juice = []
        if market != "h2h":
            juice = sorted(
                b for b, d in deltas.items()
                if abs(d) < EPS and cur[b][1] is not None and prev[b][1] is not None
                and abs(juice_cents(cur[b][1]) - juice_cents(prev[b][1])) >= T["noise_juice_cents"]
            )
        text, reason = None, None
        if up and down:
            text, reason = f"Movimientos en direcciones opuestas: {len(up)} casas suben y {len(down)} bajan.", "mixto"
        elif dom and not fast:
            text, reason = (f"{len(dom)} casas movieron la línea tras {round(gap)} min sin snapshots; "
                            "no se puede medir la velocidad."), "hueco"
        elif dom:
            names = ", ".join(map(book_name, dom))
            text = f"Solo {names} {'movió' if len(dom) == 1 else 'movieron'} la línea; el mercado no confirma."
            if sharp_now:
                text += " Hay una casa sharp: posible inicio de vapor."
            reason = "sin_confirmacion"
        elif len(juice) >= 2:
            text, reason = f"Cambio de juice en {len(juice)} casas sin mover la línea.", "juice"
        if text:
            signals.append(Signal(
                "noise", eid, market, clamp(10 + 5 * len(dom) + 2 * len(juice)), cons[pid], cons[sid], text,
                {**base, "reason": reason, "juice_books": juice},
                silenced=True, dedupe_key=f"noise|{eid}|{market}|{sid}",
            ))
    return signals
