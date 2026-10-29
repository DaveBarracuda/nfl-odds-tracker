"""Guarda señales y envía alertas agrupadas por partido y mercado a Telegram."""
import html
import json
import logging
import re
import time
from collections import defaultdict

from . import config, db, telegram
from .detect import KIND_NAMES, PRIORITY, Signal

log = logging.getLogger(__name__)

DAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
ICONS = {"info": "📰", "steam": "🔥", "rlm": "🔄", "gradual": "📈", "reversion": "↩️", "noise": "•"}


def kickoff_label(value: str) -> str:
    dt = db.parse_iso(value).astimezone(config.TIMEZONE)
    return f"{DAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} {dt:%H:%M}"


def format_message(event, market: str, signals: list[Signal]) -> str:
    ordered = sorted(signals, key=lambda s: (PRIORITY[s.kind], s.strength), reverse=True)
    title = " + ".join(KIND_NAMES[s.kind].upper() for s in ordered)
    lines = [
        f"{ICONS[ordered[0].kind]} <b>{html.escape(title)}</b> · {config.MARKET_NAMES[market]}",
        f"<b>{html.escape(event['away'])} @ {html.escape(event['home'])}</b>",
        f"Kickoff: {kickoff_label(event['commence_time'])}",
    ]
    opening = _first_detail(ordered, "open_text")
    if opening:
        lines.append(html.escape(opening))
    for s in ordered:
        lines += ["", f"<b>{KIND_NAMES[s.kind]}</b> · fuerza {s.strength}/100", html.escape(s.summary)]
    splits_text = next((s.details.get("splits_text") for s in ordered if s.details.get("splits_text")), None)
    if splits_text:
        lines += ["", html.escape(splits_text)]
    return "\n".join(lines)


def _first_detail(signals: list[Signal], key: str):
    """Primer valor no vacío de ese campo entre las señales del mensaje."""
    return next((s.details.get(key) for s in signals if s.details.get(key)), None)


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def process(conn, snapshot_id: int, signals: list[Signal], dry_run: bool = False) -> dict:
    created_at = db.iso(db.utcnow())
    events = {r["id"]: r for r in conn.execute("SELECT * FROM events")}
    new = []
    for s in signals:
        cur = conn.execute(
            """INSERT OR IGNORE INTO alerts (snapshot_id, created_at, event_id, market, kind, strength,
               from_value, to_value, summary, details, silenced, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, created_at, s.event_id, s.market, s.kind, s.strength, s.from_value, s.to_value,
             s.summary, json.dumps(s.details, ensure_ascii=False), int(s.silenced), s.dedupe_key or None),
        )
        if cur.rowcount:
            new.append((cur.lastrowid, s))
            if s.kind == "reversion":
                mark_reverted(conn, s.event_id, s.market, snapshot_id)

    groups = defaultdict(list)
    for alert_id, s in new:
        if not s.silenced:
            groups[(s.event_id, s.market)].append((alert_id, s))

    sent = 0
    for (event_id, market), items in groups.items():
        text = format_message(events[event_id], market, [s for _, s in items])
        ids = [(alert_id,) for alert_id, _ in items]
        conn.executemany("UPDATE alerts SET message = ? WHERE id = ?", [(text, i) for (i,) in ids])
        if dry_run or not telegram.is_configured():
            why = "dry-run" if dry_run else "Telegram sin chat configurado"
            log.info("Alerta sin enviar (%s): %s", why, plain(text).replace("\n", " | "))
            continue
        try:
            telegram.send_message(text)
            conn.executemany("UPDATE alerts SET sent = 1 WHERE id = ?", ids)
            sent += 1
            time.sleep(0.4)
        except Exception as exc:
            log.error("No se pudo enviar a Telegram: %s", exc)
    conn.commit()
    return {"signals": len(signals), "new": len(new), "messages": len(groups), "sent": sent}


def mark_reverted(conn, event_id: str, market: str, snapshot_id: int) -> int:
    """Marca las alertas anteriores del mercado: su movimiento ya se deshizo.

    Solo las de snapshots previos; las de este mismo snapshot describen el regreso."""
    cur = conn.execute(
        """UPDATE alerts SET reverted = 1 WHERE event_id = ? AND market = ? AND snapshot_id < ?
           AND kind NOT IN ('noise', 'reversion') AND reverted = 0""",
        (event_id, market, snapshot_id),
    )
    return cur.rowcount


def system(conn, key: str, text: str, dry_run: bool = False) -> None:
    """Aviso operativo (errores, créditos). Se manda una sola vez por clave."""
    meta_key = f"notified:{key}"
    if db.get_meta(conn, meta_key):
        return
    db.set_meta(conn, meta_key, db.iso(db.utcnow()))
    conn.commit()
    if dry_run or not telegram.is_configured():
        log.warning("Aviso sin enviar: %s", plain(text))
        return
    try:
        telegram.send_message(text)
    except Exception as exc:
        log.error("No se pudo enviar el aviso a Telegram: %s", exc)


def clear_system(conn, prefix: str) -> None:
    conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"notified:{prefix}%",))
    conn.commit()


def check_credits(conn, remaining: int | None, dry_run: bool = False) -> None:
    if remaining is None:
        return
    if remaining >= config.LOW_CREDITS_WARNING:
        clear_system(conn, "low-credits")
        return
    system(
        conn, "low-credits",
        f"⚠️ <b>Odds Tracker</b>: quedan {remaining} créditos de The Odds API "
        f"(≈{remaining // 3} snapshots).",
        dry_run,
    )
