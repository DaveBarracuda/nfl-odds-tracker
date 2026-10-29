"""Genera dashboard/index.html con los datos embebidos."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, detect, notify, openings, scheduler

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
HISTORY_DAYS = 7
MAX_ALERTS = 500
MARKETS = ("spreads", "totals", "h2h")


def _market_data(market: str, by_snap: dict, position: dict, opens: dict, splits: dict) -> dict:
    """Serie del consenso, posición contra la apertura y foto del último snapshot."""
    size = len(position)
    cons_series: list = [None] * size
    books: dict[str, list] = {}
    first = None
    for sid in sorted(by_snap):
        value, _ = detect.market_consensus(market, by_snap[sid])
        cons_series[position[sid]] = value
        first = value if first is None else first
        for book, (v, _) in by_snap[sid].items():
            books.setdefault(book, [None] * size)[position[sid]] = v
    latest = max(by_snap)
    now_value, outliers = detect.market_consensus(market, by_snap[latest])
    opening = opens.get(openings.CONSENSUS)
    drift = (openings.compare(market, opening, now_value, None)
             if opening is not None and now_value is not None else None)
    return {
        "cons": cons_series,
        "books": books,
        "open": opening.value if opening else first,
        "open_at": db.iso(opening.seen_at) if opening else None,
        "open_books": opening.books if opening else None,
        "net": drift.net if drift else None,
        "swing": drift.swing if drift else None,
        "extreme": drift.extreme if drift else None,
        "state": drift.state if drift else None,
        "now": now_value,
        "current": {
            b: {"value": v, "juice": j, "outlier": b in outliers,
                "open": opens[b].value if b in opens else None}
            for b, (v, j) in by_snap[latest].items()
        },
        "splits": splits,
    }


def _games(conn, now: datetime, history: dict, position: dict, opens: dict, splits: dict) -> list:
    horizon = now + timedelta(days=config.LOOKAHEAD_DAYS)
    events = conn.execute(
        "SELECT * FROM events WHERE commence_time >= ? AND commence_time <= ? "
        "ORDER BY commence_time, home",
        (db.iso(now - timedelta(hours=4)), db.iso(horizon)),
    ).fetchall()
    games = []
    for ev in events:
        markets = {}
        for market in MARKETS:
            by_snap = history.get((ev["id"], market))
            if by_snap:
                markets[market] = _market_data(
                    market, by_snap, position,
                    opens.get((ev["id"], market), {}), splits.get((ev["id"], market), {}),
                )
        games.append({
            "id": ev["id"], "home": ev["home"], "away": ev["away"],
            "home_short": detect.short(ev["home"]), "away_short": detect.short(ev["away"]),
            "commence": ev["commence_time"], "started": db.parse_iso(ev["commence_time"]) <= now,
            "markets": markets,
        })
    return games


def _alerts(conn, since: datetime) -> list:
    alerts = []
    for r in conn.execute(
        """SELECT a.*, e.home, e.away FROM alerts a LEFT JOIN events e ON e.id = a.event_id
           WHERE a.created_at >= ? ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
        (db.iso(since), MAX_ALERTS),
    ):
        home, away = r["home"] or "", r["away"] or ""
        alerts.append({
            "id": r["id"], "created_at": r["created_at"], "event_id": r["event_id"],
            "market": r["market"], "kind": r["kind"], "strength": r["strength"],
            "from_label": detect.fmt_value(r["market"], r["from_value"], home),
            "to_label": detect.fmt_value(r["market"], r["to_value"], home),
            "summary": r["summary"], "details": json.loads(r["details"] or "{}"),
            "silenced": bool(r["silenced"]), "sent": bool(r["sent"]),
            "reverted": bool(r["reverted"]),
            "message": notify.plain(r["message"]) if r["message"] else None,
            "game": {"home": home, "away": away,
                     "home_short": detect.short(home), "away_short": detect.short(away)},
        })
    return alerts


def _status(conn, now: datetime, last) -> dict:
    day = db.iso(now - timedelta(hours=24))
    usage = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(credits_last), 0) AS credits FROM snapshots "
        "WHERE error IS NULL AND taken_at >= ?",
        (day,),
    ).fetchone()
    counts = conn.execute(
        "SELECT COALESCE(SUM(silenced = 0), 0) AS shown, COALESCE(SUM(silenced = 1), 0) AS muted "
        "FROM alerts WHERE created_at >= ?",
        (day,),
    ).fetchone()
    latest_any = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    upcoming = conn.execute(
        "SELECT commence_time FROM events WHERE commence_time > ? AND commence_time <= ?",
        (db.iso(now), db.iso(now + timedelta(days=config.LOOKAHEAD_DAYS))),
    ).fetchall()
    due = scheduler.due_at(now, upcoming, last)
    reason = scheduler.should_snapshot(due or now, upcoming, last)[1]
    return {
        "last_snapshot": last["taken_at"] if last else None,
        "last_error": latest_any["error"] if latest_any and latest_any["error"] else None,
        "credits_remaining": last["credits_remaining"] if last else None,
        "credits_used": last["credits_used"] if last else None,
        "snapshots_24h": usage["n"], "credits_24h": usage["credits"],
        "alerts_24h": counts["shown"], "silenced_24h": counts["muted"],
        "next_run": db.iso(due) if due else None, "next_reason": reason,
    }


def build_data(conn, now: datetime) -> dict:
    since = now - timedelta(days=HISTORY_DAYS)
    snaps, history = detect.load_history(conn, db.iso(since))
    snap_ids = sorted(snaps)
    position = {sid: i for i, sid in enumerate(snap_ids)}
    last = db.last_snapshot(conn)
    splits = detect.load_splits(conn, last["id"]) if last else {}
    return {
        "generated_at": db.iso(now),
        "tz": str(config.TIMEZONE),
        "lookahead_days": config.LOOKAHEAD_DAYS,
        "low_credits": config.LOW_CREDITS_WARNING,
        "books": [
            {"key": b, "name": detect.book_name(b), "sharp": b in config.SHARP_BOOKS}
            for b in config.BOOKMAKERS
        ],
        "times": [db.iso(snaps[s]) for s in snap_ids],
        "status": _status(conn, now, last),
        "games": _games(conn, now, history, position, openings.load(conn), splits),
        "alerts": _alerts(conn, since),
    }


def render(conn, now: datetime | None = None) -> Path:
    data = build_data(conn, now or db.utcnow())
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DASHBOARD_PATH.write_text(page, encoding="utf-8")
    return config.DASHBOARD_PATH
