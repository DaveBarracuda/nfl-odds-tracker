"""% de tickets y dinero: Action Network (consenso) y DK Network (DraftKings).

Ninguna de las dos es una API oficial; si una falla, el snapshot sigue con la otra."""
import html
import logging
import re
from collections import defaultdict
from datetime import timedelta

from .db import parse_iso
from .net import get_json, request

log = logging.getLogger(__name__)

AN_URL = "https://api.actionnetwork.com/web/v2/scoreboard/nfl"
AN_CONSENSUS_BOOK = "15"
AN_MARKETS = {"spread": "spreads", "total": "totals", "moneyline": "h2h"}

DK_URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
DK_NFL_GROUP = "88808"
DK_MARKETS = {"Spread": "spreads", "Total": "totals", "Moneyline": "h2h"}
DK_MAX_PAGES = 8

OPPOSITE_SIDE = {"home": "away", "away": "home"}


def nickname(team: str) -> str:
    return team.strip().split()[-1].lower() if team and team.strip() else ""


def fetch_action_network() -> list[dict]:
    data, _ = get_json(AN_URL, {"bookIds": AN_CONSENSUS_BOOK, "periods": "event"})
    rows = []
    for game in data.get("games", []):
        teams = {t["id"]: t["full_name"] for t in game.get("teams", [])}
        home, away = teams.get(game.get("home_team_id")), teams.get(game.get("away_team_id"))
        if not home or not away:
            continue
        event = ((game.get("markets") or {}).get(AN_CONSENSUS_BOOK) or {}).get("event") or {}
        for an_key, market in AN_MARKETS.items():
            for outcome in event.get(an_key) or []:
                info = outcome.get("bet_info") or {}
                tickets = (info.get("tickets") or {}).get("percent")
                money = (info.get("money") or {}).get("percent")
                if not tickets and not money:
                    continue
                rows.append({
                    "source": "actionnetwork", "home": home, "away": away,
                    "start": game.get("start_time"), "market": market,
                    "side": outcome.get("side"), "line": outcome.get("value"),
                    "tickets": tickets, "money": money,
                })
    return rows


def fetch_dk_network() -> list[dict]:
    rows, seen = [], set()
    for page in range(1, DK_MAX_PAGES + 1):
        text, _ = request(DK_URL, {
            "tb_eg": DK_NFL_GROUP, "tb_edate": "n7days", "tb_emt": "0", "tb_page": page,
        })
        games = [g for g in parse_dk_page(text) if g["title"] not in seen]
        if not games:
            break
        for game in games:
            seen.add(game["title"])
            rows.extend(game["rows"])
    return rows


def _clean(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return " ".join(text.replace("−", "-").split())


def parse_dk_page(text: str) -> list[dict]:
    games = []
    for block in re.split(r'<div class="tb-se ', text)[1:]:
        title_match = re.search(r"<h5[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
        if not title_match:
            continue
        title = _clean(title_match.group(1))
        if " @ " not in title:
            continue
        away, home = (x.strip() for x in title.split(" @ ", 1))
        rows = []
        for part in block.split("tb-se-head")[1:]:
            head = re.search(r'<div class="flex-1">(.*?)</div>', part, re.S)
            market = DK_MARKETS.get(_clean(head.group(1))) if head else None
            if not market:
                continue
            for row in part.split("tb-sodd")[1:]:
                label_match = re.search(r"tb-slipline[^>]*>(.*?)</div>", row, re.S)
                percents = re.findall(r">\s*(\d{1,3})%", row)
                if not label_match or len(percents) < 2:
                    continue
                side, line = dk_side(market, _clean(label_match.group(1)), home, away)
                if side is None:
                    continue
                rows.append({
                    "source": "dknetwork", "home": home, "away": away, "start": None,
                    "market": market, "side": side, "line": line,
                    "money": float(percents[0]), "tickets": float(percents[1]),
                })
        games.append({"title": title, "rows": rows})
    return games


def dk_side(market: str, label: str, home: str, away: str):
    """'PIT Steelers -6.5' → ('home', -6.5); 'Over 40.5' → ('over', 40.5)."""
    label = re.sub(r"\s+(PK|pk|EVEN|Even)$", " 0", label)
    number = re.search(r"\s([+-]?\d+(?:\.\d+)?)$", label) if market != "h2h" else None
    line = float(number.group(1)) if number else None
    name = label[: number.start()].strip() if number else label
    if market == "totals":
        lowered = name.lower()
        if lowered.startswith("over"):
            return "over", line
        if lowered.startswith("under"):
            return "under", line
        return None, line
    if nickname(name) == nickname(home):
        return "home", line
    if nickname(name) == nickname(away):
        return "away", line
    return None, line


def match_events(rows: list[dict], events: list[dict]) -> list[tuple[str, dict]]:
    """Asigna cada fila de splits a un evento de The Odds API por apodo de equipo."""
    index = defaultdict(list)
    for event in events:
        index[(nickname(event["home_team"]), nickname(event["away_team"]))].append(event)
    matched = []
    for row in rows:
        key = (nickname(row["home"]), nickname(row["away"]))
        candidates, flipped = index.get(key), False
        if not candidates:
            candidates, flipped = index.get((key[1], key[0])), True
        if not candidates:
            continue
        if row.get("start"):
            start = parse_iso(row["start"])
            candidates = [
                e for e in candidates
                if abs(parse_iso(e["commence_time"]) - start) <= timedelta(hours=12)
            ]
            if not candidates:
                continue
        event = min(candidates, key=lambda e: e["commence_time"])
        row = dict(row)
        if flipped and row["side"] in OPPOSITE_SIDE:
            row["side"] = OPPOSITE_SIDE[row["side"]]
        matched.append((event["id"], row))
    return matched


def collect(conn, snapshot_id: int, events: list[dict]) -> dict:
    """Descarga ambas fuentes y guarda los splits. Devuelve partidos emparejados por fuente."""
    status = {}
    for source, fetch in (("actionnetwork", fetch_action_network), ("dknetwork", fetch_dk_network)):
        try:
            rows = fetch()
        except Exception as exc:  # fuente no oficial: nunca debe tumbar el snapshot
            log.warning("Splits de %s no disponibles: %s", source, exc)
            status[source] = f"error: {exc}"[:120]
            continue
        matched = match_events(rows, events)
        conn.executemany(
            """INSERT OR REPLACE INTO splits
               (snapshot_id, event_id, source, market, side, line, tickets_pct, money_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (snapshot_id, event_id, r["source"], r["market"], r["side"], r["line"], r["tickets"], r["money"])
                for event_id, r in matched
            ],
        )
        status[source] = len({event_id for event_id, _ in matched})
    return status
