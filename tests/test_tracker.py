import unittest
from datetime import datetime, timedelta, timezone

from tracker import dashboard, db, detect, notify, odds_api, scheduler, splits

BOOKS = ["pinnacle", "lowvig", "betonlineag", "draftkings", "fanduel",
         "betmgm", "espnbet", "betrivers", "hardrockbet", "bovada"]
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
HOME, AWAY = "Indianapolis Colts", "Baltimore Ravens"


def every(value, **overrides):
    lines = {b: value for b in BOOKS}
    lines.update(overrides)
    return lines


class TrackerCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.conn.execute(
            "INSERT INTO events (id, home, away, commence_time) VALUES ('e1', ?, ?, ?)",
            (HOME, AWAY, db.iso(T0 + timedelta(days=4))),
        )

    def snap(self, minutes, spreads=None, totals=None):
        taken = T0 + timedelta(minutes=minutes)
        sid = self.conn.execute(
            "INSERT INTO snapshots (taken_at, reason) VALUES (?, 'test')", (db.iso(taken),)
        ).lastrowid
        rows = []
        for book, value in (spreads or {}).items():
            point, price = value if isinstance(value, tuple) else (value, -110)
            rows += [(sid, "e1", book, "spreads", "home", point, price, None),
                     (sid, "e1", book, "spreads", "away", -point, -110, None)]
        for book, point in (totals or {}).items():
            rows += [(sid, "e1", book, "totals", "over", point, -110, None),
                     (sid, "e1", book, "totals", "under", point, -110, None)]
        self.conn.executemany("INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return sid

    def signals(self, sid):
        return detect.analyze(self.conn, sid)

    def kinds(self, sid):
        return sorted(s.kind for s in self.signals(sid))


class DetectTests(TrackerCase):
    def test_no_signal_without_movement(self):
        self.snap(0, every(-3))
        self.assertEqual(self.kinds(self.snap(60, every(-3))), [])

    def test_steam_led_by_sharp_book(self):
        self.snap(0, every(-3))
        s2 = self.snap(60, every(-3, pinnacle=-3.5))
        self.assertEqual(self.kinds(s2), ["noise"])
        s3 = self.snap(120, every(-3, pinnacle=-3.5, lowvig=-3.5, draftkings=-3.5, fanduel=-3.5))
        steam = [s for s in self.signals(s3) if s.kind == "steam"]
        self.assertEqual(len(steam), 1)
        self.assertEqual(steam[0].details["sharp_first"], ["pinnacle"])
        self.assertEqual((steam[0].from_value, steam[0].to_value), (-3, -3.5))
        self.assertIn("Pinnacle se movió primero", steam[0].summary)

    def test_information_move_lands_on_key_number(self):
        self.snap(0, every(-9.5))
        info = [s for s in self.signals(self.snap(60, every(-7))) if s.kind == "info"]
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0].details["key_number"], 7)
        self.assertGreaterEqual(info[0].strength, 80)

    def test_rlm_against_public_tickets(self):
        # Ravens (visitante) de -4 a -3.5: la línea va hacia Colts con 71% de tickets en Ravens.
        self.snap(0, every(4))
        sid = self.snap(180, every(3.5))
        self.conn.executemany(
            "INSERT INTO splits VALUES (?, 'e1', 'actionnetwork', 'spreads', ?, ?, ?, ?)",
            [(sid, "home", 3.5, 29, 56), (sid, "away", -3.5, 71, 44)],
        )
        signals = self.signals(sid)
        self.assertEqual(sorted(s.kind for s in signals), ["rlm"])
        rlm = signals[0]
        self.assertEqual(rlm.details["tickets_public"], 71)
        self.assertIn("71% de los tickets van con Ravens", rlm.summary)
        self.assertIn("hacia Colts", rlm.summary)

    def test_fast_move_with_public_splits_is_steam_and_rlm(self):
        self.snap(0, every(4))
        sid = self.snap(60, every(3.5))
        self.conn.execute("INSERT INTO splits VALUES (?, 'e1', 'dknetwork', 'spreads', 'away', -3.5, 68, 51)", (sid,))
        self.assertEqual(self.kinds(sid), ["rlm", "steam"])

    def test_gradual_drift(self):
        values = [45.5, 46, 46.5, 47]
        sids = [self.snap(720 * i, totals=every(v)) for i, v in enumerate(values)]
        self.assertEqual(self.kinds(sids[2]), ["noise"])
        gradual = self.signals(sids[3])
        self.assertEqual([s.kind for s in gradual], ["gradual"])
        self.assertEqual((gradual[0].from_value, gradual[0].to_value), (45.5, 47))
        self.assertIn("hacia Over", gradual[0].summary)

    def test_single_book_move_is_noise(self):
        self.snap(0, every(-3))
        signals = self.signals(self.snap(60, every(-3, bovada=-3.5)))
        self.assertEqual([s.kind for s in signals], ["noise"])
        self.assertTrue(signals[0].silenced)

    def test_juice_only_move_is_noise(self):
        self.snap(0, every((-3, -110)))
        signals = self.signals(self.snap(60, every((-3, -110), draftkings=(-3, -125), fanduel=(-3, -122))))
        self.assertEqual([s.details["reason"] for s in signals], ["juice"])

    def test_consensus_is_a_real_line_with_sharp_tiebreak(self):
        split = every(-3.5, fanduel=-3, betmgm=-3, espnbet=-3, betrivers=-3, hardrockbet=-3)
        lines = {b: (v, -110) for b, v in split.items()}
        self.assertEqual(detect.consensus(lines, 1.5, discrete=True)[0], -3.5)
        self.assertEqual(detect.consensus(lines, 1.5)[0], -3.25)

    def kept_snapshots(self):
        return {r["snapshot_id"] for r in self.conn.execute("SELECT DISTINCT snapshot_id FROM lines")}

    def test_prune_compacts_old_lines_to_open_daily_and_close(self):
        # Kickoff de e1 = T0 + 4 días (T0 es a las 12:00 UTC). Snapshots cada 6 h durante 6 días.
        hours = range(0, 24 * 6, 6)
        sids = {h: self.snap(60 * h, every(-3)) for h in hours}
        recent = self.snap(60 * 24 * 20, every(-3))
        removed = db.prune(self.conn, T0 + timedelta(days=20), keep_days=8, archive_days=200)
        first_of_day = {}
        for h in hours:
            first_of_day.setdefault((T0 + timedelta(hours=h)).date(), sids[h])
        close = sids[24 * 4]                                     # último snapshot <= kickoff
        expected = set(first_of_day.values()) | {close, recent}
        self.assertEqual(self.kept_snapshots(), expected)
        self.assertEqual(removed, (len(sids) - len(expected) + 1) * len(BOOKS) * 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], len(sids) + 1)

    def test_prune_archives_real_alerts_but_not_old_noise(self):
        self.snap(0, every(-3))
        for kind in ("rlm", "noise"):
            self.conn.execute("INSERT INTO alerts (snapshot_id, created_at, event_id, market, kind, strength)"
                              " VALUES (1, ?, 'e1', 'spreads', ?, 60)", (db.iso(T0), kind))
        db.prune(self.conn, T0 + timedelta(days=20), keep_days=8, archive_days=200)
        kinds = [r["kind"] for r in self.conn.execute("SELECT kind FROM alerts")]
        self.assertEqual(kinds, ["rlm"])

    def test_prune_keeps_recent_lines_complete(self):
        sids = {self.snap(60 * h, every(-3)) for h in range(0, 24, 6)}
        removed = db.prune(self.conn, T0 + timedelta(days=1), keep_days=8, archive_days=200)
        self.assertEqual(removed, 0)
        self.assertEqual(self.kept_snapshots(), sids)

    def test_prune_drops_everything_past_the_archive(self):
        self.snap(0, every(-3))
        self.conn.execute("INSERT INTO alerts (snapshot_id, created_at, event_id, market, kind, strength)"
                          " VALUES (1, ?, 'e1', 'spreads', 'rlm', 60)", (db.iso(T0),))
        recent = self.snap(60 * 24 * 205, every(-3))
        db.prune(self.conn, T0 + timedelta(days=205), keep_days=8, archive_days=200)
        self.assertEqual(self.kept_snapshots(), {recent})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_outlier_book_is_flagged_and_excluded(self):
        signals = self.signals(self.snap(0, every(-3, draftkings=3)))
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].details["reason"], "discrepancia")
        self.assertEqual(signals[0].from_value, -3)


class SchedulerTests(unittest.TestCase):
    events = [{"commence_time": db.iso(T0 + timedelta(hours=3))}]

    @staticmethod
    def last(minutes_ago, now=T0):
        return {"taken_at": db.iso(now - timedelta(minutes=minutes_ago))}

    def test_first_snapshot(self):
        self.assertEqual(scheduler.should_snapshot(T0, self.events, None), (True, "horario"))

    def test_hourly_far_from_kickoff(self):
        self.assertFalse(scheduler.should_snapshot(T0, self.events, self.last(30))[0])
        self.assertEqual(scheduler.should_snapshot(T0, self.events, self.last(58)), (True, "horario"))

    def test_every_half_hour_within_two_hours(self):
        now = T0 + timedelta(minutes=90)
        self.assertEqual(scheduler.should_snapshot(now, self.events, self.last(30, now)), (True, "pregame"))
        self.assertFalse(scheduler.should_snapshot(now, self.events, self.last(10, now))[0])

    def test_no_games(self):
        self.assertFalse(scheduler.should_snapshot(T0, [], None)[0])

    def test_next_slot(self):
        self.assertEqual(scheduler.next_slot(T0 + timedelta(minutes=7)), T0 + timedelta(minutes=37))
        self.assertEqual(scheduler.next_slot(T0 + timedelta(minutes=41)), T0 + timedelta(minutes=67))


DK_HTML = """
<div class="tb-se border-b px-2 py-4">
  <div class="tb-se-title flex"><h5 class="mt-0"><a href="#">ATL Falcons @ PIT Steelers </a></h5><span>9/13, 01:00PM</span></div>
  <div class="tb-market-wrap"><div>
    <div class="tb-se-head flex"><div class="flex-1">Spread</div><div class="flex-1">Odds</div><div class="flex-1">% Handle</div><div class="flex-1">% Bets</div></div>
    <div class="tb-sm">
      <div class="tb-sodd flex"><div class="tb-slipline flex-1">PIT Steelers -6.5</div><div class="flex-1"><a class="tb-odd-s">&minus;112</a></div>
        <div class="flex-1">86% <div class="tb-progress"><div style="width: 86%"></div></div></div>
        <div class="flex-1">62% <div class="tb-progress"><div style="width: 62%"></div></div></div></div>
      <div class="tb-sodd flex"><div class="tb-slipline flex-1">ATL Falcons +6.5</div><div class="flex-1"><a class="tb-odd-s">&minus;108</a></div>
        <div class="flex-1">14%</div><div class="flex-1">38%</div></div>
    </div></div><div>
    <div class="tb-se-head flex"><div class="flex-1">Total</div></div>
    <div class="tb-sm"><div class="tb-sodd flex"><div class="tb-slipline flex-1">Over 40.5</div><div class="flex-1"><a>-112</a></div><div class="flex-1">55%</div><div class="flex-1">60%</div></div></div>
  </div></div>
</div>
"""


class SourceTests(unittest.TestCase):
    def test_parse_dk_network(self):
        games = splits.parse_dk_page(DK_HTML)
        self.assertEqual(games[0]["title"], "ATL Falcons @ PIT Steelers")
        rows = {(r["market"], r["side"]): r for r in games[0]["rows"]}
        self.assertEqual(rows[("spreads", "home")]["line"], -6.5)
        self.assertEqual((rows[("spreads", "home")]["money"], rows[("spreads", "home")]["tickets"]), (86, 62))
        self.assertEqual(rows[("totals", "over")]["tickets"], 60)

    def test_match_events_by_nickname_and_flip(self):
        events = [{"id": "x", "home_team": "Pittsburgh Steelers", "away_team": "Atlanta Falcons",
                   "commence_time": "2026-09-13T17:00:00Z"}]
        rows = [{"home": "PIT Steelers", "away": "ATL Falcons", "start": None, "side": "home", "market": "spreads"},
                {"home": "Atlanta Falcons", "away": "Pittsburgh Steelers", "start": "2026-09-13T17:00:00.000Z",
                 "side": "home", "market": "h2h"}]
        matched = splits.match_events(rows, events)
        self.assertEqual([(eid, r["side"]) for eid, r in matched], [("x", "home"), ("x", "away")])

    def test_normalize_odds(self):
        data = [{"id": "e", "home_team": HOME, "away_team": AWAY, "bookmakers": [{"key": "pinnacle", "markets": [
            {"key": "spreads", "last_update": "t", "outcomes": [
                {"name": AWAY, "point": -3, "price": 100}, {"name": HOME, "point": 3, "price": -110}]},
            {"key": "totals", "outcomes": [{"name": "Over", "point": 44.5, "price": -105}]}]}]}]
        rows = odds_api.normalize(data)
        self.assertIn(("e", "pinnacle", "spreads", "home", 3, -110, "t"), rows)
        self.assertIn(("e", "pinnacle", "totals", "over", 44.5, -105, None), rows)


class OutputTests(TrackerCase):
    def test_message_and_dashboard_data(self):
        self.snap(0, every(-9.5))
        sid = self.snap(60, every(-7))
        signals = self.signals(sid)
        result = notify.process(self.conn, sid, signals, dry_run=True)
        self.assertEqual(result["messages"], 1)
        message = self.conn.execute("SELECT message FROM alerts WHERE kind = 'info'").fetchone()["message"]
        self.assertIn("INFORMACIÓN", message)
        self.assertIn("Ravens @ Indianapolis Colts", notify.plain(message).replace("Baltimore ", ""))
        again = notify.process(self.conn, sid, signals, dry_run=True)
        self.assertEqual(again["new"], 0)

        data = dashboard.build_data(self.conn, T0 + timedelta(minutes=61))
        game = data["games"][0]
        self.assertEqual(game["markets"]["spreads"]["open"], -9.5)
        self.assertEqual(game["markets"]["spreads"]["now"], -7)
        self.assertEqual(data["alerts"][0]["to_label"], "Colts -7")


if __name__ == "__main__":
    unittest.main()
