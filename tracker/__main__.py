"""CLI del tracker: python -m tracker <comando>."""
import argparse
import logging
import os
import subprocess
import sys
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from . import (config, dashboard, db, detect, notify, odds_api, openings, scheduler,
               splits, telegram)

log = logging.getLogger("tracker")


def setup_logging(verbose: bool) -> None:
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(config.LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"),
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def take_snapshot(conn, now, reason: str):
    try:
        odds, usage = odds_api.fetch_odds(now)
    except Exception as exc:
        conn.execute("INSERT INTO snapshots (taken_at, reason, error) VALUES (?, ?, ?)", (db.iso(now), reason, str(exc)[:500]))
        conn.commit()
        raise
    snapshot_id = conn.execute(
        """INSERT INTO snapshots (taken_at, reason, credits_last, credits_used, credits_remaining, events)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (db.iso(now), reason, usage["last"], usage["used"], usage["remaining"], len(odds)),
    ).lastrowid
    db.upsert_events(conn, odds)
    rows = odds_api.normalize(odds)
    conn.executemany("INSERT OR REPLACE INTO lines VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(snapshot_id, *r) for r in rows])
    conn.commit()
    split_status = splits.collect(conn, snapshot_id, odds)
    conn.commit()
    log.info(
        "Snapshot %s (%s): %s partidos, %s líneas, %s créditos (restan %s), splits %s",
        snapshot_id, reason, len(odds), len(rows), usage["last"], usage["remaining"], split_status,
    )
    return snapshot_id, usage


def _warn_if_chain_broke(conn, now, last, dry_run: bool) -> None:
    """El loop del workflow deja huecos de minutos; uno de horas significa que la
    cadena de ejecuciones se rompió y la revivió el cron de respaldo."""
    if last is None:
        return
    minutes = (now - db.parse_iso(last["taken_at"])).total_seconds() / 60
    if minutes >= config.STALE_GAP_MIN:
        notify.system(
            conn, "stale-gap",
            f"⚠️ <b>Odds Tracker</b>: {minutes / 60:.1f} h sin snapshot. La cadena de "
            "ejecuciones se cortó y acaba de reanudarse.",
            dry_run,
        )
    else:
        notify.clear_system(conn, "stale-gap")


def cmd_run(args) -> int:
    conn = db.connect(config.DB_PATH)
    now = db.utcnow()
    try:
        events = odds_api.fetch_events(now)
        db.upsert_events(conn, events)
        conn.commit()
    except Exception as exc:
        log.error("No se pudieron leer los partidos: %s", exc)
        notify.system(conn, f"events-error:{str(exc)[:80]}", f"⚠️ <b>Odds Tracker</b>: error al leer partidos: {exc}", args.dry_run)
        return 1

    last = db.last_snapshot(conn)
    should, reason = (True, "manual") if args.force else scheduler.should_snapshot(now, events, last)
    if not should:
        log.info("Sin snapshot: %s", reason)
        dashboard.render(conn, now)
        return 0

    try:
        snapshot_id, usage = take_snapshot(conn, now, reason)
    except Exception as exc:
        log.exception("Falló el snapshot")
        notify.system(conn, f"odds-error:{str(exc)[:80]}", f"⚠️ <b>Odds Tracker</b>: falló el snapshot: {exc}", args.dry_run)
        dashboard.render(conn, now)
        return 1
    openings.ensure(conn, snapshot_id)
    _warn_if_chain_broke(conn, now, last, args.dry_run)
    notify.clear_system(conn, "odds-error")
    notify.clear_system(conn, "events-error")

    signals = detect.analyze(conn, snapshot_id)
    result = notify.process(conn, snapshot_id, signals, dry_run=args.dry_run)
    log.info("Detección: %s", result)
    notify.check_credits(conn, usage["remaining"], args.dry_run)
    pruned = db.prune(conn, now, config.KEEP_DAYS, config.ARCHIVE_DAYS)
    if pruned:
        log.info("Compactación: %s líneas de más de %s días (se conservan apertura, cierre y diario)",
                 pruned, config.KEEP_DAYS)
    path = dashboard.render(conn, now)
    log.info("Dashboard: %s", path)
    return 0


def cmd_analyze(args) -> int:
    conn = db.connect(config.DB_PATH)
    snapshot_id = args.snapshot or (db.last_snapshot(conn) or {"id": None})["id"]
    if snapshot_id is None:
        print("No hay snapshots.")
        return 1
    events = {r["id"]: r for r in conn.execute("SELECT * FROM events")}
    signals = detect.analyze(conn, snapshot_id)
    print(f"Snapshot {snapshot_id}: {len(signals)} señales")
    for s in sorted(signals, key=lambda s: (detect.PRIORITY[s.kind], s.strength), reverse=True):
        ev = events.get(s.event_id)
        game = f"{ev['away']} @ {ev['home']}" if ev else s.event_id
        muted = " (silenciada)" if s.silenced else ""
        print(f"- [{detect.KIND_NAMES[s.kind]} {s.strength}]{muted} {game} · {config.MARKET_NAMES[s.market]}: {s.summary}")
    return 0


def cmd_dashboard(args) -> int:
    conn = db.connect(config.DB_PATH)
    path = dashboard.render(conn)
    print(f"Dashboard generado: {path}")
    if args.open:
        os.startfile(path)  # noqa: S606 - solo Windows
    return 0


def cmd_sync(args) -> int:
    """Descarga de la rama state el dashboard y la base que genera GitHub Actions."""
    def git(*cmd):
        return subprocess.run(["git", *cmd], cwd=config.ROOT, check=True, capture_output=True)

    try:
        git("fetch", "--depth=1", "origin", "state")
        for name, dest in (("dashboard.html", config.REMOTE_DASHBOARD_PATH), ("tracker.db", config.REMOTE_DB_PATH)):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(git("show", f"FETCH_HEAD:{name}").stdout)
        updated = git("log", "-1", "--format=%cd", "--date=format-local:%Y-%m-%d %H:%M", "FETCH_HEAD").stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"No se pudo descargar el estado de GitHub: {detail}")
        return 1
    print(f"Dashboard de GitHub Actions (actualizado {updated.decode().strip()}): {config.REMOTE_DASHBOARD_PATH}")
    if args.open:
        os.startfile(config.REMOTE_DASHBOARD_PATH)  # noqa: S606 - solo Windows
    return 0


def cmd_next(args) -> int:
    """Cuándo toca el próximo snapshot. El loop del workflow usa --seconds para dormir
    exactamente lo que falta, en vez de despertar en balde."""
    conn = db.connect(config.DB_PATH)
    now = db.utcnow()
    # Solo partidos que la API siguió devolviendo: uno cancelado conserva su kickoff
    # futuro en la base y haría creer al loop que hay que apretar la cadencia.
    fresh = db.iso(now - timedelta(hours=config.EVENT_FRESH_HOURS))
    known = conn.execute(
        "SELECT 1 FROM events WHERE updated_at >= ? LIMIT 1", (fresh,)
    ).fetchone() is not None
    upcoming = conn.execute(
        "SELECT commence_time FROM events WHERE commence_time > ? AND updated_at >= ?",
        (db.iso(now), fresh),
    ).fetchall()
    last = db.last_snapshot(conn)
    due = scheduler.due_at(now, upcoming, last)
    if args.seconds:
        print(scheduler.wait_seconds(now, upcoming, last, known_schedule=known))
    elif due is None:
        print("No toca snapshot en el horizonte.")
    else:
        print(f"Próximo snapshot: {due.astimezone(config.TIMEZONE):%Y-%m-%d %H:%M}")
    return 0


def cmd_status(args) -> int:
    conn = db.connect(config.DB_PATH)
    now = db.utcnow()
    last = db.last_snapshot(conn)
    total = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(credits_last), 0) AS c FROM snapshots WHERE error IS NULL").fetchone()
    print(f"Base de datos: {config.DB_PATH}")
    print(f"Snapshots: {total['n']} · créditos consumidos por el tracker: {total['c']}")
    if last:
        taken = db.parse_iso(last["taken_at"]).astimezone(config.TIMEZONE)
        print(f"Último snapshot: {taken:%Y-%m-%d %H:%M} ({last['reason']}) · restan {last['credits_remaining']} créditos")
    upcoming = conn.execute("SELECT commence_time FROM events WHERE commence_time > ?", (db.iso(now),)).fetchall()
    due = scheduler.due_at(now, upcoming, last)
    reason = scheduler.should_snapshot(due or now, upcoming, last)[1]
    if due is None:
        print(f"Próximo snapshot: no toca en el horizonte ({reason})")
    else:
        print(f"Próximo snapshot {due.astimezone(config.TIMEZONE):%H:%M} ({reason})")
    print(f"Casas: {', '.join(config.BOOKMAKERS)}")
    print(f"Telegram: {'listo' if telegram.is_configured() else 'falta TELEGRAM_CHAT_ID (python -m tracker telegram-setup)'}")
    return 0


def _write_env(key: str, value: str) -> None:
    path = config.ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_telegram_setup(args) -> int:
    chat_id = args.chat_id
    if not chat_id:
        chats = telegram.discover_chats()
        if not chats:
            print("El bot no tiene mensajes. Abre Telegram, busca tu bot, mándale /start y vuelve a correr este comando.")
            return 1
        if len(chats) > 1:
            print("Hay varios chats; elige uno con --chat-id:")
            for cid, name in chats.items():
                print(f"  {cid}  {name}")
            return 1
        chat_id, name = next(iter(chats.items()))
        print(f"Chat encontrado: {name}")
    _write_env("TELEGRAM_CHAT_ID", str(chat_id))
    print(f"TELEGRAM_CHAT_ID={chat_id} guardado en .env")
    return 0


def cmd_telegram_test(args) -> int:
    if not telegram.is_configured():
        print("Falta TELEGRAM_CHAT_ID. Corre: python -m tracker telegram-setup")
        return 1
    telegram.send_message("✅ <b>Odds Tracker</b>: conexión con Telegram lista.")
    print("Mensaje de prueba enviado.")
    return 0


def main(argv=None) -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m tracker", description="Tracker de movimiento de líneas NFL")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Ejecución programada: decide, toma snapshot, detecta y alerta")
    p.add_argument("--force", action="store_true", help="Toma snapshot aunque no toque (gasta créditos)")
    p.add_argument("--dry-run", action="store_true", help="No envía nada a Telegram")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("analyze", help="Vuelve a correr la detección sobre un snapshot, sin enviar")
    p.add_argument("--snapshot", type=int)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("dashboard", help="Regenera dashboard/index.html")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("sync", help="Descarga el dashboard y la base que genera GitHub Actions")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("next", help="Cuándo toca el próximo snapshot")
    p.add_argument("--seconds", action="store_true", help="Solo los segundos de espera (lo usa el loop)")
    p.set_defaults(func=cmd_next)

    sub.add_parser("status", help="Estado, créditos y próximo snapshot").set_defaults(func=cmd_status)

    p = sub.add_parser("telegram-setup", help="Guarda el chat ID de Telegram en .env")
    p.add_argument("--chat-id")
    p.set_defaults(func=cmd_telegram_setup)

    sub.add_parser("telegram-test", help="Envía un mensaje de prueba").set_defaults(func=cmd_telegram_test)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if args.command in ("run",) and not config.ODDS_API_KEY:
        log.error("Falta ODDS_API_KEY en .env")
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
