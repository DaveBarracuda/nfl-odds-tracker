import unittest
from datetime import datetime, timedelta, timezone

from tracker import dashboard, db, detect, notify, odds_api, openings, scheduler, splits

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

    def snap(self, minutes, spreads=None, totals=None, register=True):
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
        if register:
            openings.ensure(self.conn, sid)
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

    def test_reversion_marks_previous_alerts_as_reverted(self):
        self.snap(0, every(-3))
        sid2 = self.snap(60, every(-4))
        notify.process(self.conn, sid2, self.signals(sid2), dry_run=True)
        sid3 = self.snap(120, every(-3))
        notify.process(self.conn, sid3, self.signals(sid3), dry_run=True)
        rows = dict(self.conn.execute("SELECT kind, reverted FROM alerts WHERE snapshot_id = ?", (sid2,)))
        self.assertEqual(rows["steam"], 1)

    def test_reversion_does_not_mark_its_own_snapshot(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4))
        sid = self.snap(120, every(-3))
        notify.process(self.conn, sid, self.signals(sid), dry_run=True)
        same = dict(self.conn.execute("SELECT kind, reverted FROM alerts WHERE snapshot_id = ?", (sid,)))
        self.assertTrue(all(v == 0 for v in same.values()), same)

    def test_message_shows_the_opening_line(self):
        self.snap(0, every(-9.5))
        sid = self.snap(60, every(-7))
        notify.process(self.conn, sid, self.signals(sid), dry_run=True)
        message = self.conn.execute("SELECT message FROM alerts WHERE kind = 'info'").fetchone()["message"]
        self.assertIn("Apertura", notify.plain(message))
        self.assertIn("Colts -9.5", notify.plain(message))

    def test_dashboard_exposes_opening_and_drift(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4.5))
        self.snap(120, every(-3))
        data = dashboard.build_data(self.conn, T0 + timedelta(minutes=121))
        spreads = data["games"][0]["markets"]["spreads"]
        self.assertEqual(spreads["open"], -3)
        self.assertEqual(spreads["net"], 0)
        self.assertEqual(spreads["swing"], 1.5)
        self.assertEqual(spreads["state"], "retorno")


class OpeningTests(TrackerCase):
    """La apertura es la primera linea observada y es la base de toda comparacion."""

    def test_opening_is_first_observed_line(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4.5))
        opens = openings.load(self.conn)[("e1", "spreads")]
        self.assertEqual(opens[openings.CONSENSUS].value, -3)
        self.assertEqual(opens["pinnacle"].value, -3)

    def test_opening_tracks_extremes_across_snapshots(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4.5))
        self.snap(120, every(-2.5))
        cons = openings.load(self.conn)[("e1", "spreads")][openings.CONSENSUS]
        self.assertEqual((cons.low, cons.high), (-4.5, -2.5))
        self.assertEqual(cons.value, -3)

    def test_consensus_opening_waits_for_enough_books(self):
        self.snap(0, {"pinnacle": -3, "fanduel": -3})
        openings.ensure(self.conn, 1)
        self.assertNotIn(openings.CONSENSUS, openings.load(self.conn)[("e1", "spreads")])
        self.snap(60, every(-4))
        self.assertEqual(openings.load(self.conn)[("e1", "spreads")][openings.CONSENSUS].value, -4)

    def test_backfill_uses_oldest_snapshot_not_newest(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4.5))
        self.conn.execute("DELETE FROM openings")
        openings.backfill(self.conn)
        self.assertEqual(openings.load(self.conn)[("e1", "spreads")][openings.CONSENSUS].value, -3)


class OpeningRobustnessTests(TrackerCase):
    """Huecos del pipeline y alimentaciones incompletas de la API."""

    def test_skipped_snapshot_is_recovered_on_the_next_run(self):
        self.snap(0, every(-3))
        self.snap(60, every(-5), register=False)
        sid = self.snap(120, every(-3))
        cons = openings.load(self.conn)[("e1", "spreads")][openings.CONSENSUS]
        self.assertEqual(cons.low, -5)
        self.assertIn("reversion", self.kinds(sid))

    def test_thin_feed_does_not_move_the_extremes(self):
        self.snap(0, every(-3))
        self.snap(60, {"pinnacle": -4.5, "fanduel": -4.5, "betmgm": -4.5})
        sid = self.snap(120, every(-3))
        cons = openings.load(self.conn)[("e1", "spreads")][openings.CONSENSUS]
        self.assertEqual((cons.low, cons.high), (-3, -3))
        self.assertNotIn("reversion", self.kinds(sid))

    def test_analyze_does_not_write_openings(self):
        sid = self.snap(0, every(-3), register=False)
        self.assertEqual(detect.analyze(self.conn, sid), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM openings").fetchone()[0], 0)

    def test_second_round_trip_alerts_again(self):
        """Una ida y vuelta posterior no debe quedar suprimida por la anterior."""
        reversions = 0
        for minutes, line in ((0, -3), (60, -4), (120, -3), (180, -4), (240, -3)):
            sid = self.snap(minutes, every(line))
            found = [s for s in self.signals(sid) if s.kind == "reversion"]
            reversions += notify.process(self.conn, sid, found, dry_run=True)["new"]
        self.assertEqual(reversions, 2)


class DriftTests(unittest.TestCase):
    """Estado de la linea actual contra su apertura."""

    @staticmethod
    def opening(value=-3.0, high=None, low=None):
        return openings.Opening(value=value, seen_at=T0, snapshot_id=1,
                                high=value if high is None else high,
                                low=value if low is None else low, books=10)

    def test_sustained_move_away_from_open(self):
        drift = openings.compare("spreads", self.opening(low=-4.5), current=-4.5, previous=-4.0)
        self.assertEqual(drift.state, "sostenido")
        self.assertEqual(drift.net, -1.5)
        self.assertEqual(drift.move, "extiende")
        self.assertFalse(drift.at_open)

    def test_return_to_opening_point(self):
        drift = openings.compare("spreads", self.opening(low=-4.5), current=-3.0, previous=-4.0)
        self.assertEqual(drift.state, "retorno")
        self.assertEqual(drift.swing, 1.5)
        self.assertEqual(drift.move, "vuelve")
        self.assertTrue(drift.at_open)

    def test_pullback_that_does_not_reach_opening(self):
        drift = openings.compare("spreads", self.opening(low=-4.5), current=-4.0, previous=-4.5)
        self.assertEqual(drift.move, "corrige")
        self.assertEqual(drift.state, "sostenido")

    def test_crossing_the_opening_line(self):
        drift = openings.compare("spreads", self.opening(high=-2.5, low=-4.5), current=-2.5, previous=-3.5)
        self.assertEqual(drift.move, "cruza")

    def test_describe_reads_naturally_when_net_is_zero(self):
        drift = openings.compare("spreads", self.opening(low=-4.5), current=-3.0, previous=-4.0)
        text = openings.describe("spreads", drift, -3.0, "Indianapolis Colts")
        self.assertIn("sin cambio neto", text)
        self.assertNotIn("+0", text)

    def test_line_that_never_moved(self):
        drift = openings.compare("spreads", self.opening(), current=-3.0, previous=-3.0)
        self.assertEqual(drift.state, "estable")
        self.assertIsNone(drift.move)


class ReversionTests(TrackerCase):
    def test_round_trip_emits_reversion_and_is_not_silenced(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4))
        signals = self.signals(self.snap(120, every(-3)))
        reversion = [s for s in signals if s.kind == "reversion"]
        self.assertEqual(len(reversion), 1)
        self.assertFalse(reversion[0].silenced)
        self.assertEqual(reversion[0].details["swing"], 1.0)
        self.assertIn("volvió a su apertura", reversion[0].summary)

    def test_small_round_trip_is_not_a_reversion(self):
        self.snap(0, every(-3))
        self.snap(60, every(-3.5))
        self.assertNotIn("reversion", self.kinds(self.snap(120, every(-3))))

    def test_signals_carry_the_comparison_against_opening(self):
        self.snap(0, every(-9.5))
        info = [s for s in self.signals(self.snap(60, every(-7))) if s.kind == "info"][0]
        self.assertEqual(info.details["open_value"], -9.5)
        self.assertEqual(info.details["open_net"], 2.5)
        self.assertEqual(info.details["open_state"], "sostenido")
        self.assertEqual(info.details["open_move"], "extiende")
        self.assertIn("apertura", info.details["open_text"])

    def test_move_back_toward_open_is_reported_as_correction(self):
        self.snap(0, every(-3))
        self.snap(60, every(-4.5))
        steam = [s for s in self.signals(self.snap(120, every(-3.5))) if s.kind == "steam"][0]
        self.assertEqual(steam.details["open_move"], "corrige")
        self.assertEqual(steam.details["open_net"], -0.5)


if __name__ == "__main__":
    unittest.main()
