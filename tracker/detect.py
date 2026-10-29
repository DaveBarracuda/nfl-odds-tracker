"""Orquestación de la detección de movimientos.

Cada snapshot se compara con dos referencias:
- el snapshot anterior, que da la velocidad del movimiento, y
- la línea de apertura, que dice si el movimiento es real o ya se deshizo.

Las reglas viven en `rules.py` y las primitivas de mercado en `market.py`."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, openings, rules
from .db import iso, parse_iso
from .market import (EPS, KIND_NAMES, OPPOSITE, PRIORITY, UNITS, Signal, book_name, book_value,
                     clamp, consensus, fmt_delta, fmt_magnitude, fmt_value, implied, is_discrete,
                     juice_cents, key_number_hit, market_consensus, move_side, short, side_label)

__all__ = [
    "EPS", "KIND_NAMES", "OPPOSITE", "PRIORITY", "UNITS", "Signal", "analyze", "book_name",
    "book_value", "clamp", "consensus", "fmt_delta", "fmt_magnitude", "fmt_value", "implied",
    "is_discrete",
    "juice_cents", "key_number_hit", "load_history", "load_openings", "load_splits",
    "market_consensus", "move_side", "short", "side_label", "splits_summary",
]

SPLIT_SOURCES = ("actionnetwork", "dknetwork")
SPLIT_SOURCE_NAMES = {"actionnetwork": "Action Network", "dknetwork": "DK Network"}


def load_history(conn: sqlite3.Connection, since: str, until_id: int | None = None):
    """snaps: id → datetime; history: (evento, mercado) → snapshot → casa → (valor, precio)."""
    query = "SELECT id, taken_at FROM snapshots WHERE error IS NULL AND taken_at >= ?"
    params: list = [since]
    if until_id is not None:
        query += " AND id <= ?"
        params.append(until_id)
    snaps = {r["id"]: parse_iso(r["taken_at"]) for r in conn.execute(query, params)}
    history: dict = defaultdict(lambda: defaultdict(dict))
    if not snaps:
        return snaps, history
    raw: dict = defaultdict(dict)
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


def load_openings(conn: sqlite3.Connection) -> dict:
    """(evento, mercado) → casa → Opening, incluida la apertura del consenso."""
    return openings.load(conn)


def load_splits(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """Splits más recientes hasta el snapshot: (evento, mercado) → fuente → lado → datos."""
    result: dict = defaultdict(dict)
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
            parts.append(
                f"{side_label(side, home, away)} {_pct(v.get('tickets'))} tickets / "
                f"{_pct(v.get('money'))} dinero"
            )
        if parts:
            return source, sides, f"Splits ({SPLIT_SOURCE_NAMES[source]}): " + " · ".join(parts)
    return None, None, None


def analyze(conn: sqlite3.Connection, snapshot_id: int) -> list[Signal]:
    """Señales de un snapshot. Solo lectura: las aperturas las registra el pipeline."""
    row = conn.execute("SELECT taken_at FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        return []
    now = parse_iso(row["taken_at"])
    window = timedelta(hours=config.THRESHOLDS["gradual_window_hours"])
    snaps, history = load_history(conn, iso(now - window), until_id=snapshot_id)
    events = {r["id"]: r for r in conn.execute("SELECT id, home, away, commence_time FROM events")}
    splits = load_splits(conn, snapshot_id)
    opens = openings.load(conn)
    signals = []
    for (event_id, market), by_snap in history.items():
        if snapshot_id in by_snap and event_id in events and market in config.THRESHOLDS:
            signals.extend(analyze_market(
                events[event_id], market, by_snap, snaps, snapshot_id,
                splits.get((event_id, market), {}),
                opens.get((event_id, market), {}).get(openings.CONSENSUS),
            ))
    return signals


def analyze_market(event, market: str, by_snap: dict, snaps: dict[int, datetime], sid: int,
                   splits: dict, opening=None) -> list[Signal]:
    """Aplica todas las reglas a un partido y mercado."""
    cons, outliers = {}, {}
    for snap_id in sorted(by_snap):
        cons[snap_id], outliers[snap_id] = market_consensus(market, by_snap[snap_id])
    signals = rules.outlier_signals(event, market, by_snap[sid], cons[sid], outliers[sid])
    summary = splits_summary(splits, market, event["home"], event["away"])
    ctx = rules.build(event, market, by_snap, snaps, sid, summary, opening, cons, outliers)
    if ctx is None:
        return signals

    primary = None
    for rule in (rules.info, rules.steam):
        signal = rule(ctx)
        if signal is not None:
            signals.append(signal)
            primary = signal.kind
            break
    for signal in (rules.reversion(ctx), rules.rlm(ctx, primary), rules.gradual(ctx, primary)):
        if signal is not None:
            signals.append(signal)
    if not any(s.kind != "noise" for s in signals):
        quiet = rules.noise(ctx)
        if quiet is not None:
            signals.append(quiet)
    return signals
