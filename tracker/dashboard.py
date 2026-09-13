"""Genera dashboard/index.html con los datos embebidos."""
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, detect, notify, scheduler

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
HISTORY_DAYS = 7
MAX_ALERTS = 500


def build_data(conn, now: datetime) -> dict:
    T = config.THRESHOLDS
    since = now - timedelta(days=HISTORY_DAYS)
    snaps, history = detect.load_history(conn, db.iso(since))
    snap_ids = sorted(snaps)
    position = {sid: i for i, sid in enumerate(snap_ids)}
    last = db.last_snapshot(conn)
    splits = detect.load_splits(conn, last["id"]) if last else {}
    horizon = now + timedelta(days=config.LOOKAHEAD_DAYS)

    games = []
    events = conn.execute(
        "SELECT * FROM events WHERE commence_time >= ? AND commence_time <= ? ORDER BY commence_time, home",
        (db.iso(now - timedelta(hours=4)), db.iso(horizon)),
    ).fetchall()
    for ev in events:
        markets = {}
        for market in ("spreads", "totals", "h2h"):
            by_snap = history.get((ev["id"], market))
            if not by_snap:
                continue
            cons_series = [None] * len(snap_ids)
            books: dict[str, list] = {}
            first = None
            for sid in sorted(by_snap):
                value, _ = detect.consensus(by_snap[sid], T[market]["outlier"], discrete=market != "h2h")
                cons_series[position[sid]] = value
                first = value if first is None else first
                for book, (v, _) in by_snap[sid].items():
                    books.setdefault(book, [None] * len(snap_ids))[position[sid]] = v
            latest = max(by_snap)
            now_value, outliers = detect.consensus(by_snap[latest], T[market]["outlier"], discrete=market != "h2h")
            markets[market] = {
                "cons": cons_series,
                "books": books,
                "open": first,
                "now": now_value,
                "current": {
                    b: {"value": v, "juice": j, "outlier": b in outliers} for b, (v, j) in by_snap[latest].items()
                },
                "splits": splits.get((ev["id"], market), {}),
            }
        games.append({
            "id": ev["id"], "home": ev["home"], "away": ev["away"],
            "home_short": detect.short(ev["home"]), "away_short": detect.short(ev["away"]),
            "commence": ev["commence_time"], "started": db.parse_iso(ev["commence_time"]) <= now,
            "markets": markets,
        })

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
            "message": notify.plain(r["message"]) if r["message"] else None,
            "game": {"home": home, "away": away, "home_short": detect.short(home), "away_short": detect.short(away)},
        })

    day = db.iso(now - timedelta(hours=24))
    usage = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(credits_last), 0) AS credits FROM snapshots WHERE error IS NULL AND taken_at >= ?",
        (day,),
    ).fetchone()
    counts = conn.execute(
        "SELECT COALESCE(SUM(silenced = 0), 0) AS shown, COALESCE(SUM(silenced = 1), 0) AS muted FROM alerts WHERE created_at >= ?",
        (day,),
    ).fetchone()
    latest_any = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    upcoming = conn.execute(
        "SELECT commence_time FROM events WHERE commence_time > ? AND commence_time <= ?",
        (db.iso(now), db.iso(horizon)),
    ).fetchall()
    next_run = scheduler.next_slot(now)
    next_snapshot, next_reason = scheduler.should_snapshot(next_run, upcoming, last)

    return {
        "generated_at": db.iso(now),
        "tz": str(config.TIMEZONE),
        "lookahead_days": config.LOOKAHEAD_DAYS,
        "low_credits": config.LOW_CREDITS_WARNING,
        "books": [
            {"key": b, "name": detect.book_name(b), "sharp": b in config.SHARP_BOOKS} for b in config.BOOKMAKERS
        ],
        "times": [db.iso(snaps[s]) for s in snap_ids],
        "status": {
            "last_snapshot": last["taken_at"] if last else None,
            "last_error": latest_any["error"] if latest_any and latest_any["error"] else None,
            "credits_remaining": last["credits_remaining"] if last else None,
            "credits_used": last["credits_used"] if last else None,
            "snapshots_24h": usage["n"], "credits_24h": usage["credits"],
            "alerts_24h": counts["shown"], "silenced_24h": counts["muted"],
            "next_run": db.iso(next_run), "next_snapshot": next_snapshot, "next_reason": next_reason,
        },
        "games": games,
        "alerts": alerts,
    }


def render(conn, now: datetime | None = None) -> Path:
    data = build_data(conn, now or db.utcnow())
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DASHBOARD_PATH.write_text(page, encoding="utf-8")
    return config.DASHBOARD_PATH
