"""Línea de apertura: la primera línea observada de cada casa y del consenso.

Es la base contra la que se mide todo movimiento. Cada snapshot se compara
contra el anterior (velocidad) y contra la apertura (dirección real), para
distinguir un movimiento sostenido de uno que ya regresó a su punto de partida.

La apertura que se guarda es la primera que *vio el tracker*, no necesariamente
la primera que publicó la casa: por eso se registra también cuándo se observó.
Los extremos (`high`/`low`) se actualizan en cada snapshot y sobreviven al
borrado de historial, así el recorrido máximo no se pierde."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from . import config, market
from .db import get_meta, parse_iso, set_meta

CONSENSUS = "_consenso"
# Hasta donde se registraron aperturas: permite recuperar un snapshot que el
# pipeline no alcanzo a procesar en su momento.
WATERMARK = "openings_last_snapshot"
# Para estirar los extremos del consenso hace falta una alimentacion comparable
# a la de la apertura: con pocas casas el "maximo" puede ser un espejismo.
MIN_BOOK_SHARE = 0.6

STATE_LABELS = {
    "sostenido": "movimiento sostenido",
    "leve": "cambio leve",
    "retorno": "volvió a la apertura",
    "estable": "sin cambio desde la apertura",
}
MOVE_LABELS = {
    "extiende": "se aleja de la apertura",
    "corrige": "corrige hacia la apertura",
    "vuelve": "regresó a la apertura",
    "cruza": "cruzó la apertura",
}


@dataclass(frozen=True)
class Opening:
    value: float
    seen_at: datetime
    snapshot_id: int
    high: float
    low: float
    books: int


@dataclass(frozen=True)
class Drift:
    """Posición de la línea actual respecto a su apertura."""

    open_value: float
    net: float
    prev_net: float | None
    swing: float
    extreme: float
    at_open: bool
    state: str
    move: str | None


def _snapshot_values(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """(evento, mercado) → casa → (valor, precio) de un solo snapshot."""
    raw: dict = defaultdict(dict)
    for r in conn.execute(
        "SELECT event_id, book, market, outcome, point, price FROM lines WHERE snapshot_id = ?",
        (snapshot_id,),
    ):
        raw[(r[0], r[2], r[1])][r[3]] = (r[4], r[5])
    values: dict = defaultdict(dict)
    for (event_id, market_key, book), outcomes in raw.items():
        value = market.book_value(market_key, outcomes)
        if value is not None:
            values[(event_id, market_key)][book] = value
    return values


_UPSERT = """
INSERT INTO openings (event_id, market, book, value, price, snapshot_id, seen_at, high, low, books)
VALUES (:event_id, :market, :book, :value, :price, :snapshot_id, :seen_at, :value, :value, :books)
ON CONFLICT(event_id, market, book) DO UPDATE SET
    high = MAX(high, excluded.high),
    low = MIN(low, excluded.low)
WHERE (excluded.high > high OR excluded.low < low)
  AND excluded.books >= MAX(:floor, CAST(books * :share AS INTEGER))
"""


def record(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Guarda las aperturas que faltan y estira los extremos. Idempotente."""
    row = conn.execute("SELECT taken_at FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM openings").fetchone()[0]
    floor = config.THRESHOLDS["open_min_books"]
    for (event_id, market_key), books in _snapshot_values(conn, snapshot_id).items():
        if market_key not in config.THRESHOLDS:
            continue
        entries = [(book, value, price, 1, 0, 0.0) for book, (value, price) in books.items()]
        cons, _ = market.market_consensus(market_key, books)
        if cons is not None and len(books) >= floor:
            entries.append((CONSENSUS, cons, None, len(books), floor, MIN_BOOK_SHARE))
        for book, value, price, books_seen, book_floor, share in entries:
            conn.execute(_UPSERT, {
                "event_id": event_id, "market": market_key, "book": book, "value": value,
                "price": price, "snapshot_id": snapshot_id, "seen_at": row["taken_at"],
                "books": books_seen, "floor": book_floor, "share": share,
            })
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM openings").fetchone()[0] - before


def ensure(conn: sqlite3.Connection, snapshot_id: int) -> int:
    """Registra todo lo pendiente hasta `snapshot_id`, en orden cronologico.

    Avanza una marca de agua: un snapshot que el pipeline no alcanzo a procesar
    se recupera en la siguiente ejecucion en vez de perderse."""
    done = int(get_meta(conn, WATERMARK) or 0)
    pending = [r[0] for r in conn.execute(
        "SELECT id FROM snapshots WHERE error IS NULL AND id > ? AND id <= ? ORDER BY taken_at, id",
        (done, snapshot_id),
    )]
    created = sum(record(conn, i) for i in pending)
    if pending:
        set_meta(conn, WATERMARK, str(max(pending)))
        conn.commit()
    return created


def backfill(conn: sqlite3.Connection) -> int:
    """Reprocesa todo el historial guardado, del mas viejo al mas nuevo."""
    set_meta(conn, WATERMARK, "0")
    last = conn.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
    return ensure(conn, last) if last is not None else 0


def load(conn: sqlite3.Connection) -> dict:
    """(evento, mercado) → casa → Opening."""
    result: dict = defaultdict(dict)
    for r in conn.execute("SELECT * FROM openings"):
        result[(r["event_id"], r["market"])][r["book"]] = Opening(
            value=r["value"], seen_at=parse_iso(r["seen_at"]), snapshot_id=r["snapshot_id"],
            high=r["high"], low=r["low"], books=r["books"],
        )
    return result


def compare(market_key: str, opening: Opening, current: float, previous: float | None) -> Drift:
    """Compara la línea actual contra su apertura (y de paso contra la previa)."""
    th = config.THRESHOLDS[market_key]
    tol, eps = th["step"], market.EPS
    net = round(current - opening.value, 2)
    prev_net = None if previous is None else round(previous - opening.value, 2)
    up, down = opening.high - opening.value, opening.value - opening.low
    swing = round(max(up, down), 2)
    extreme = opening.high if up >= down else opening.low
    at_open = abs(net) < tol

    if at_open:
        state = "retorno" if swing >= th["reversion"] - eps else "estable"
    elif abs(net) >= th["move"] - eps:
        state = "sostenido"
    else:
        state = "leve"

    move = None
    if prev_net is not None and abs(net - prev_net) >= tol - eps:
        if at_open:
            move = "vuelve"
        elif prev_net * net < 0:
            move = "cruza"
        elif abs(net) > abs(prev_net):
            move = "extiende"
        else:
            move = "corrige"
    return Drift(opening.value, net, prev_net, swing, extreme, at_open, state, move)


def describe(market_key: str, drift: Drift, current: float, home: str) -> str:
    """Texto de una línea con la foto completa: apertura, actual y neto."""
    label = MOVE_LABELS[drift.move] if drift.move else STATE_LABELS[drift.state]
    net = ("sin cambio neto" if abs(drift.net) < market.EPS
           else market.fmt_delta(market_key, drift.net))
    return (f"Apertura {market.fmt_value(market_key, drift.open_value, home)} → ahora "
            f"{market.fmt_value(market_key, current, home)} ({net}, {label})")
