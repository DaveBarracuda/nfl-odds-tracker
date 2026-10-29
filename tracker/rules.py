"""Reglas de clasificación. Una función por tipo de movimiento.

Cada regla recibe el contexto de un partido y mercado (`MarketCtx`), que trae
las dos comparaciones: contra el snapshot anterior (velocidad) y contra la
apertura (dirección real)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from . import config, openings
from .market import (EPS, OPPOSITE, UNITS, Signal, book_name, clamp, fmt_delta, fmt_magnitude,
                     fmt_value, juice_cents, key_number_hit, move_side, side_label)
from .openings import MIN_BOOK_SHARE, Drift, Opening

EXTENDS_BONUS = 5
PULLBACK_PENALTY = -5


@dataclass(frozen=True)
class MarketCtx:
    """Todo lo que una regla necesita saber de un partido y mercado."""

    event: object
    market: str
    sid: int
    pid: int
    ids: list[int]
    by_snap: dict
    snaps: dict
    cons: dict
    outliers: dict
    opening: Opening | None
    drift: Drift | None
    split_source: str | None
    split_data: dict | None
    split_text: str | None
    deltas: dict
    up: list[str]
    down: list[str]
    dom: list[str]
    opp: list[str]
    n_books: int
    cons_delta: float
    gap: float
    fast: bool
    sharp_now: list[str]
    sharp_first: list[str]
    base: dict

    @property
    def th(self) -> dict:
        return config.THRESHOLDS[self.market]

    @property
    def eid(self) -> str:
        return self.event["id"]

    @property
    def home(self) -> str:
        return self.event["home"]

    @property
    def away(self) -> str:
        return self.event["away"]

    def fmt(self, value: float | None) -> str:
        return fmt_value(self.market, value, self.home)

    def label(self, side: str) -> str:
        return side_label(side, self.home, self.away)

    @property
    def drift_bonus(self) -> int:
        """Un movimiento que se aleja de la apertura pesa más que uno que la deshace."""
        if not self.drift or not self.drift.move:
            return 0
        if self.drift.move == "extiende":
            return EXTENDS_BONUS
        return PULLBACK_PENALTY if self.drift.move in ("corrige", "vuelve") else 0


def outlier_signals(event, market: str, books: dict, cons: float | None, outliers: set) -> list[Signal]:
    """Casas que se alejan tanto del resto que se excluyen del consenso."""
    signals = []
    for book in sorted(outliers):
        value = books[book][0]
        signals.append(Signal(
            "noise", event["id"], market, 15, cons, value,
            f"{book_name(book)} fuera de consenso: {fmt_value(market, value, event['home'])} contra "
            f"{fmt_value(market, cons, event['home'])}. "
            "Posible error del feed o movimiento aislado; verificar antes de actuar.",
            {"reason": "discrepancia", "book": book, "unit": UNITS[market]},
            silenced=True, dedupe_key=f"outlier|{event['id']}|{market}|{book}|{value}",
        ))
    return signals


def _sharp_leaders(market: str, by_snap: dict, earlier: list[int], prev: dict,
                   dom: list[str], sign: int) -> list[str]:
    """Casas sharp que ya se habían movido en esa dirección un snapshot antes."""
    if not dom or len(earlier) < 2:
        return []
    before = by_snap[earlier[-2]]
    move = config.THRESHOLDS[market]["move"]
    return sorted(
        b for b in config.SHARP_BOOKS
        if b in prev and b in before and sign * (prev[b][0] - before[b][0]) >= move - EPS
    )


def _base_details(event, market: str, cons_from: float, cons_to: float, prev: dict, cur: dict,
                  dom: list[str], deltas: dict, gap: float, sharp_now: list[str],
                  sharp_first: list[str], splits: tuple, drift, opening) -> dict:
    """Campos comunes a todas las señales: el movimiento y su posición vs la apertura."""
    source, _, text = splits
    base = {
        "unit": UNITS[market], "gap_min": round(gap), "books": len(deltas),
        "consensus_from": cons_from, "consensus_to": cons_to,
        "consensus_delta": round(cons_to - cons_from, 2),
        "movers": [{"book": b, "from": prev[b][0], "to": cur[b][0]} for b in dom],
        "sharp_movers": sharp_now, "sharp_first": sharp_first,
        "splits_source": source, "splits_text": text,
    }
    if drift is not None and opening is not None:
        base.update({
            "open_value": drift.open_value, "open_net": drift.net, "open_swing": drift.swing,
            "open_extreme": drift.extreme, "open_state": drift.state, "open_move": drift.move,
            "open_seen_at": opening.seen_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open_books": opening.books,
            "open_text": openings.describe(market, drift, cons_to, event["home"]),
        })
    return base


def build(event, market: str, by_snap: dict, snaps: dict[int, datetime], sid: int,
          splits: tuple, opening: Opening | None, cons: dict, outliers: dict) -> MarketCtx | None:
    """Arma el contexto del snapshot actual; None si todavía no hay con qué comparar."""
    th = config.THRESHOLDS[market]
    ids = sorted(by_snap)
    earlier = [s for s in ids if s < sid]
    if not earlier:
        return None
    pid = earlier[-1]
    cur, prev = by_snap[sid], by_snap[pid]
    excluded = outliers[sid] | outliers[pid]
    deltas = {b: cur[b][0] - prev[b][0] for b in cur if b in prev and b not in excluded}
    up = sorted(b for b, d in deltas.items() if d >= th["move"] - EPS)
    down = sorted(b for b, d in deltas.items() if d <= -th["move"] + EPS)
    dom, opp, sign = (up, down, 1) if len(up) >= len(down) else (down, up, -1)
    sharp_now = [b for b in dom if b in config.SHARP_BOOKS]
    sharp_first = _sharp_leaders(market, by_snap, earlier, prev, dom, sign)
    drift = None
    if opening is not None and cons[sid] is not None:
        drift = openings.compare(market, opening, cons[sid], cons[pid])
    gap = (snaps[sid] - snaps[pid]).total_seconds() / 60
    source, data, text = splits
    base = _base_details(event, market, cons[pid], cons[sid], prev, cur, dom, deltas,
                         gap, sharp_now, sharp_first, splits, drift, opening)
    return MarketCtx(
        event=event, market=market, sid=sid, pid=pid, ids=ids, by_snap=by_snap, snaps=snaps,
        cons=cons, outliers=outliers, opening=opening, drift=drift,
        split_source=source, split_data=data, split_text=text,
        deltas=deltas, up=up, down=down, dom=dom, opp=opp, n_books=len(deltas),
        cons_delta=round(cons[sid] - cons[pid], 2), gap=gap,
        fast=gap <= config.THRESHOLDS["steam_max_gap_min"],
        sharp_now=sharp_now, sharp_first=sharp_first, base=base,
    )


def info(ctx: MarketCtx) -> Signal | None:
    """Salto grande y casi unánime: patrón de noticia."""
    T, th = config.THRESHOLDS, ctx.th
    if not (ctx.fast and ctx.n_books >= 3 and abs(ctx.cons_delta) >= th["info"] - EPS
            and len(ctx.dom) / ctx.n_books >= T["info_min_book_share"]):
        return None
    cons_from, cons_to = ctx.cons[ctx.pid], ctx.cons[ctx.sid]
    key = key_number_hit(cons_from, cons_to) if ctx.market == "spreads" else None
    strength = (70 + 20 * (abs(ctx.cons_delta) / th["info"] - 1) + (10 if key else 0)
                + (5 if ctx.sharp_now else 0) + ctx.drift_bonus)
    text = (f"Salto de {fmt_delta(ctx.market, ctx.cons_delta)} en {len(ctx.dom)} de {ctx.n_books} casas "
            f"en {round(ctx.gap)} min ({ctx.fmt(cons_from)} → {ctx.fmt(cons_to)})")
    if key:
        text += f", pasando por el número clave {key}"
    text += ". Patrón de noticia: revisar lesiones, alineaciones o clima."
    return Signal("info", ctx.eid, ctx.market, clamp(strength), cons_from, cons_to,
                  text + _drift_note(ctx), {**ctx.base, "key_number": key},
                  dedupe_key=f"info|{ctx.eid}|{ctx.market}|{ctx.sid}")


def steam(ctx: MarketCtx) -> Signal | None:
    """Varias casas moviéndose juntas y rápido."""
    T, th = config.THRESHOLDS, ctx.th
    if not (ctx.fast and len(ctx.dom) >= T["steam_min_books"] and len(ctx.opp) <= T["steam_max_opposite"]):
        return None
    prev, cur = ctx.by_snap[ctx.pid], ctx.by_snap[ctx.sid]
    f_val = statistics.median(prev[b][0] for b in ctx.dom)
    t_val = statistics.median(cur[b][0] for b in ctx.dom)
    key = key_number_hit(f_val, t_val) if ctx.market == "spreads" else None
    size = abs(t_val - f_val) / th["move"]
    strength = (50 + 8 * (len(ctx.dom) - T["steam_min_books"])
                + (15 if ctx.sharp_first else 10 if ctx.sharp_now else 0)
                + 10 * min(size - 1, 2) + (10 if key else 0) + ctx.drift_bonus)
    text = (f"{len(ctx.dom)} de {ctx.n_books} casas movieron {ctx.fmt(f_val)} → {ctx.fmt(t_val)} "
            f"en {round(ctx.gap)} min")
    if ctx.sharp_first:
        verb = "se movió" if len(ctx.sharp_first) == 1 else "se movieron"
        text += f"; {', '.join(map(book_name, ctx.sharp_first))} {verb} primero"
    elif ctx.sharp_now:
        text += f"; incluye {', '.join(map(book_name, ctx.sharp_now))}"
    if key:
        text += f"; número clave {key}"
    return Signal("steam", ctx.eid, ctx.market, clamp(strength), f_val, t_val,
                  text + "." + _drift_note(ctx), {**ctx.base, "key_number": key},
                  dedupe_key=f"steam|{ctx.eid}|{ctx.market}|{ctx.sid}")


def reversion(ctx: MarketCtx) -> Signal | None:
    """La línea regresó a su punto de partida tras un recorrido real."""
    drift, opening = ctx.drift, ctx.opening
    if drift is None or opening is None or drift.state != "retorno":
        return None
    if drift.prev_net is None or abs(drift.prev_net) < ctx.th["step"] - EPS:
        return None
    if ctx.n_books < max(config.THRESHOLDS["open_min_books"], opening.books * MIN_BOOK_SHARE):
        return None
    hours = (ctx.snaps[ctx.sid] - opening.seen_at).total_seconds() / 3600
    strength = (45 + 15 * min(drift.swing / ctx.th["reversion"], 3)
                + (10 if hours >= 6 else 0) + (5 if ctx.n_books >= 6 else 0))
    text = (f"La línea volvió a su apertura ({ctx.fmt(drift.open_value)}): llegó hasta "
            f"{ctx.fmt(drift.extreme)} y regresó. Ida y vuelta de "
            f"{fmt_magnitude(ctx.market, drift.swing)} en {hours:.0f} h; el movimiento anterior "
            "no se sostuvo y el mercado quedó donde empezó.")
    return Signal("reversion", ctx.eid, ctx.market, clamp(strength),
                  ctx.cons[ctx.pid], ctx.cons[ctx.sid], text,
                  {**ctx.base, "swing": drift.swing, "hours": round(hours, 1)},
                  dedupe_key=f"reversion|{ctx.eid}|{ctx.market}|{_excursion_start(ctx)}")


def _excursion_start(ctx: MarketCtx) -> int:
    """Último snapshot en que la línea aún estaba en su apertura.

    Identifica esta ida y vuelta concreta, para que una posterior vuelva a avisar."""
    tol = ctx.th["step"] - EPS
    start = ctx.ids[0]
    for snap_id in ctx.ids[:-1]:
        value = ctx.cons.get(snap_id)
        if value is not None and abs(value - ctx.drift.open_value) < tol:
            start = snap_id
    return start


def rlm(ctx: MarketCtx, primary: str | None) -> Signal | None:
    """La línea se mueve contra el lado que tiene la mayoría de los tickets."""
    T, th = config.THRESHOLDS, ctx.th
    if abs(ctx.cons_delta) < th["move"] - EPS or not ctx.split_data:
        return None
    side = move_side(ctx.market, ctx.cons_delta)
    other = OPPOSITE[side]
    t_side = (ctx.split_data.get(side) or {}).get("tickets")
    t_other = (ctx.split_data.get(other) or {}).get("tickets")
    m_side = (ctx.split_data.get(side) or {}).get("money")
    if t_other is None and t_side is not None:
        t_other = 100 - t_side
    if t_other is None or t_other < T["rlm_min_tickets"]:
        return None
    cons_from, cons_to = ctx.cons[ctx.pid], ctx.cons[ctx.sid]
    divergence = (m_side - t_side) if m_side is not None and t_side is not None else 0
    strength = (45 + (t_other - T["rlm_min_tickets"]) + 0.5 * max(divergence, 0)
                + 5 * min(abs(ctx.cons_delta) / th["move"], 3) + (10 if primary else 0)
                + ctx.drift_bonus)
    text = (f"{t_other:.0f}% de los tickets van con {ctx.label(other)}, pero la línea se movió "
            f"hacia {ctx.label(side)} ({ctx.fmt(cons_from)} → {ctx.fmt(cons_to)}).")
    if m_side is not None:
        text += f" Dinero en {ctx.label(side)}: {m_side:.0f}%."
    return Signal(
        "rlm", ctx.eid, ctx.market, clamp(strength), cons_from, cons_to, text + _drift_note(ctx),
        {**ctx.base, "move_side": side, "tickets_public": t_other,
         "tickets_move_side": t_side, "money_move_side": m_side},
        dedupe_key=f"rlm|{ctx.eid}|{ctx.market}|{cons_from}|{cons_to}",
    )


def gradual(ctx: MarketCtx, primary: str | None) -> Signal | None:
    """Pasos pequeños y consecutivos en la misma dirección."""
    T, th = config.THRESHOLDS, ctx.th
    if primary == "info" or abs(ctx.cons_delta) < th["step"] - EPS:
        return None
    levels: list[tuple[int, float]] = []
    for s in ctx.ids:
        value = ctx.cons[s]
        if value is not None and (not levels or abs(value - levels[-1][1]) >= th["step"] - EPS):
            levels.append((s, value))
    if not levels or levels[-1][0] != ctx.sid:
        return None
    trail = 0
    for i in range(len(levels) - 1, 0, -1):
        step = levels[i][1] - levels[i - 1][1]
        if step * ctx.cons_delta > 0 and abs(step) < th["info"] - EPS:
            trail += 1
        else:
            break
    if trail < T["gradual_min_steps"]:
        return None
    start_sid, start_val = levels[-trail - 1]
    total = round(ctx.cons[ctx.sid] - start_val, 2)
    hours = (ctx.snaps[ctx.sid] - ctx.snaps[start_sid]).total_seconds() / 3600
    if abs(total) < th["gradual_total"] - EPS or hours < T["gradual_min_hours"]:
        return None
    strength = 35 + 5 * trail + 10 * min(abs(total) / th["gradual_total"], 3) + ctx.drift_bonus
    side = move_side(ctx.market, total)
    text = (f"{trail} ajustes seguidos hacia {ctx.label(side)} en {hours:.0f} h: "
            f"{ctx.fmt(start_val)} → {ctx.fmt(ctx.cons[ctx.sid])} ({fmt_delta(ctx.market, total)}).")
    return Signal("gradual", ctx.eid, ctx.market, clamp(strength), start_val, ctx.cons[ctx.sid],
                  text + _drift_note(ctx), {**ctx.base, "steps": trail, "hours": round(hours, 1)},
                  dedupe_key=f"gradual|{ctx.eid}|{ctx.market}|{start_val}|{ctx.cons[ctx.sid]}")


def noise(ctx: MarketCtx) -> Signal | None:
    """Movimiento sin confirmación del mercado: se guarda pero no se envía."""
    T = config.THRESHOLDS
    juice: list[str] = []
    if ctx.market != "h2h":
        cur, prev = ctx.by_snap[ctx.sid], ctx.by_snap[ctx.pid]
        juice = sorted(
            b for b, d in ctx.deltas.items()
            if abs(d) < EPS and cur[b][1] is not None and prev[b][1] is not None
            and abs(juice_cents(cur[b][1]) - juice_cents(prev[b][1])) >= T["noise_juice_cents"]
        )
    text, reason = None, None
    if ctx.up and ctx.down:
        text = f"Movimientos en direcciones opuestas: {len(ctx.up)} casas suben y {len(ctx.down)} bajan."
        reason = "mixto"
    elif ctx.dom and not ctx.fast:
        text = (f"{len(ctx.dom)} casas movieron la línea tras {round(ctx.gap)} min sin snapshots; "
                "no se puede medir la velocidad.")
        reason = "hueco"
    elif ctx.dom:
        names = ", ".join(map(book_name, ctx.dom))
        text = f"Solo {names} {'movió' if len(ctx.dom) == 1 else 'movieron'} la línea; el mercado no confirma."
        if ctx.sharp_now:
            text += " Hay una casa sharp: posible inicio de vapor."
        reason = "sin_confirmacion"
    elif len(juice) >= 2:
        text, reason = f"Cambio de juice en {len(juice)} casas sin mover la línea.", "juice"
    if not text:
        return None
    return Signal(
        "noise", ctx.eid, ctx.market, clamp(10 + 5 * len(ctx.dom) + 2 * len(juice)),
        ctx.cons[ctx.pid], ctx.cons[ctx.sid], text + _drift_note(ctx),
        {**ctx.base, "reason": reason, "juice_books": juice},
        silenced=True, dedupe_key=f"noise|{ctx.eid}|{ctx.market}|{ctx.sid}",
    )


def _drift_note(ctx: MarketCtx) -> str:
    """Coletilla con la posición respecto a la apertura."""
    drift = ctx.drift
    if drift is None or drift.state == "estable":
        return ""
    if drift.state == "retorno":
        return f" Desde la apertura ({ctx.fmt(drift.open_value)}) el saldo es cero."
    return f" Desde la apertura ({ctx.fmt(drift.open_value)}): {fmt_delta(ctx.market, drift.net)}."
