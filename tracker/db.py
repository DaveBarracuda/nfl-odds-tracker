"""Persistencia en SQLite."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    reason TEXT,
    credits_last INTEGER,
    credits_used INTEGER,
    credits_remaining INTEGER,
    events INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS lines (
    snapshot_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    book TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    point REAL,
    price INTEGER NOT NULL,
    last_update TEXT,
    PRIMARY KEY (snapshot_id, event_id, book, market, outcome)
);
CREATE INDEX IF NOT EXISTS idx_lines_event ON lines (event_id, market);
CREATE TABLE IF NOT EXISTS splits (
    snapshot_id INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL,
    tickets_pct REAL,
    money_pct REAL,
    PRIMARY KEY (snapshot_id, event_id, source, market, side)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    kind TEXT NOT NULL,
    strength INTEGER NOT NULL,
    from_value REAL,
    to_value REAL,
    summary TEXT,
    details TEXT,
    silenced INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    dedupe_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_events(conn: sqlite3.Connection, events: list[dict]) -> None:
    now = iso(utcnow())
    conn.executemany(
        """INSERT INTO events (id, home, away, commence_time, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET home = excluded.home, away = excluded.away,
           commence_time = excluded.commence_time, updated_at = excluded.updated_at""",
        [(e["id"], e["home_team"], e["away_team"], e["commence_time"], now) for e in events],
    )


# Pares (snapshot, evento) que se conservan al compactar: apertura, cierre (último
# snapshot antes del kickoff) y el primer snapshot de cada día UTC de cada partido.
_KEEP_PAIRS_SQL = """
CREATE TEMP TABLE keep_pairs AS
WITH old AS (
    SELECT DISTINCT l.snapshot_id, l.event_id, s.taken_at
    FROM lines l JOIN snapshots s ON s.id = l.snapshot_id
    WHERE s.taken_at < :cutoff
),
ranked AS (
    SELECT o.snapshot_id, o.event_id,
           (e.commence_time IS NULL OR o.taken_at <= e.commence_time) AS pregame,
           ROW_NUMBER() OVER (PARTITION BY o.event_id ORDER BY o.taken_at) AS open_rank,
           ROW_NUMBER() OVER (PARTITION BY o.event_id, substr(o.taken_at, 1, 10) ORDER BY o.taken_at) AS day_rank,
           ROW_NUMBER() OVER (PARTITION BY o.event_id,
                              (e.commence_time IS NULL OR o.taken_at <= e.commence_time)
                              ORDER BY o.taken_at DESC) AS close_rank
    FROM old o LEFT JOIN events e ON e.id = o.event_id
)
SELECT snapshot_id, event_id FROM ranked
WHERE open_rank = 1 OR day_rank = 1 OR (pregame AND close_rank = 1)
"""


def prune(conn: sqlite3.Connection, now: datetime, keep_days: int, archive_days: int) -> int:
    """Retención por niveles para conservar la temporada sin que la base crezca sin límite.

    * Últimos ``keep_days``: todo completo (la detección usa 72 h y el dashboard 7 días).
    * Hasta ``archive_days``: por partido solo apertura, cierre y un snapshot por día
      (líneas y splits). Alertas (salvo ruido), eventos y filas de snapshots se conservan.
    * Más viejo que ``archive_days``: se borra todo.

    Devuelve cuántas filas de líneas se eliminaron.
    """
    cutoff = iso(now - timedelta(days=keep_days))
    archive_cutoff = iso(now - timedelta(days=archive_days))
    before = conn.total_changes
    lines_before = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    expired = conn.execute("SELECT MAX(id) AS id FROM snapshots WHERE taken_at < ?", (archive_cutoff,)).fetchone()
    if expired["id"] is not None:
        conn.execute("DELETE FROM lines WHERE snapshot_id <= ?", (expired["id"],))
        conn.execute("DELETE FROM splits WHERE snapshot_id <= ?", (expired["id"],))
        conn.execute("DELETE FROM snapshots WHERE id <= ?", (expired["id"],))
    conn.execute("DROP TABLE IF EXISTS temp.keep_pairs")
    conn.execute(_KEEP_PAIRS_SQL, {"cutoff": cutoff})
    for table in ("lines", "splits"):
        conn.execute(
            f"""DELETE FROM {table}
                WHERE snapshot_id IN (SELECT id FROM snapshots WHERE taken_at < ?)
                  AND (snapshot_id, event_id) NOT IN (SELECT snapshot_id, event_id FROM temp.keep_pairs)""",
            (cutoff,),
        )
    conn.execute("DROP TABLE temp.keep_pairs")
    removed_lines = lines_before - conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    conn.execute("DELETE FROM alerts WHERE created_at < ?", (archive_cutoff,))
    # El ruido solo se muestra en el dashboard reciente: no se archiva (~75% del peso de alertas).
    conn.execute("DELETE FROM alerts WHERE kind = 'noise' AND created_at < ?", (cutoff,))
    conn.execute("DELETE FROM events WHERE commence_time < ?", (archive_cutoff,))
    conn.commit()
    if conn.total_changes > before:
        conn.execute("VACUUM")
    return removed_lines


def last_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE error IS NULL ORDER BY taken_at DESC, id DESC LIMIT 1"
    ).fetchone()
