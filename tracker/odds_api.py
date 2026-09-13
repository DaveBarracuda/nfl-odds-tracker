"""The Odds API: eventos (gratis) y momios (3 créditos por snapshot)."""
from datetime import datetime, timedelta

from . import config
from .db import iso
from .net import get_json

BASE = "https://api.the-odds-api.com/v4"


def _window(now: datetime) -> dict:
    return {
        "commenceTimeFrom": iso(now),
        "commenceTimeTo": iso(now + timedelta(days=config.LOOKAHEAD_DAYS)),
    }


def fetch_events(now: datetime) -> list[dict]:
    """Partidos próximos. Este endpoint no consume créditos."""
    data, _ = get_json(
        f"{BASE}/sports/{config.SPORT}/events",
        {"apiKey": config.ODDS_API_KEY, **_window(now)},
    )
    return data


def fetch_odds(now: datetime) -> tuple[list[dict], dict]:
    data, headers = get_json(
        f"{BASE}/sports/{config.SPORT}/odds",
        {
            "apiKey": config.ODDS_API_KEY,
            "bookmakers": ",".join(config.BOOKMAKERS),
            "markets": ",".join(config.MARKETS),
            "oddsFormat": "american",
            **_window(now),
        },
    )
    usage = {
        "last": _int(headers.get("x-requests-last")),
        "used": _int(headers.get("x-requests-used")),
        "remaining": _int(headers.get("x-requests-remaining")),
    }
    return data, usage


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize(events: list[dict]) -> list[tuple]:
    """Filas (event_id, book, market, outcome, point, price, last_update).

    El outcome se normaliza a home/away (spread y moneyline) u over/under (total)."""
    rows = []
    for ev in events:
        home, away = ev["home_team"], ev["away_team"]
        for book in ev.get("bookmakers", []):
            for market in book.get("markets", []):
                for o in market.get("outcomes", []):
                    name = o.get("name", "")
                    if market["key"] == "totals":
                        side = name.lower()
                        if side not in ("over", "under"):
                            continue
                    elif name == home:
                        side = "home"
                    elif name == away:
                        side = "away"
                    else:
                        continue
                    rows.append((
                        ev["id"], book["key"], market["key"], side,
                        o.get("point"), int(o["price"]),
                        market.get("last_update") or book.get("last_update"),
                    ))
    return rows
