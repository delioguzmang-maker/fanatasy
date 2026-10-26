"""NFL schedule from nflverse (a public CSV on GitHub): kickoff time of every game.

Used when ESPN refuses the request (it answers 403 to Google Colab). The file
lists every game of every season with the date and the Eastern kickoff time,
and fills in the score once the game is over.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import requests

from ..models import Game
from ..nfl import norm_team
from .http import make_session

GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
EASTERN = ZoneInfo("America/New_York")
GAME_LENGTH = timedelta(hours=3, minutes=45)


def parse_games_csv(text: str, season: int, week: int, now: Optional[datetime] = None) -> list[Game]:
    now = now or datetime.now(timezone.utc)
    games = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") != str(season) or row.get("week") != str(week) or row.get("game_type", "REG") != "REG":
            continue
        try:
            day = datetime.strptime(row["gameday"], "%Y-%m-%d")
            hh, mm = (row.get("gametime") or "13:00").split(":")[:2]
        except (KeyError, ValueError):
            continue
        kickoff = day.replace(hour=int(hh), minute=int(mm), tzinfo=EASTERN).astimezone(timezone.utc)
        scored = bool((row.get("home_score") or "").strip())
        if scored or now > kickoff + GAME_LENGTH:
            state = "post"
        elif now >= kickoff:
            state = "in"
        else:
            state = "pre"
        games.append(Game(home=norm_team(row.get("home_team")), away=norm_team(row.get("away_team")),
                          kickoff=kickoff, state=state, game_id=row.get("game_id", "")))
    return games


def implied_totals(text: str, seasons: Iterable[int]) -> dict[tuple[int, int, str], float]:
    """Points the betting market expects each team to score: {(season, week, team): points}.
    nflverse's spread_line is in favor of the home team (positive = home favored)."""
    wanted = {str(s) for s in seasons}
    out: dict[tuple[int, int, str], float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("season") not in wanted or row.get("game_type", "REG") != "REG":
            continue
        try:
            total, spread = float(row["total_line"]), float(row["spread_line"])
            key = (int(row["season"]), int(row["week"]))
        except (KeyError, TypeError, ValueError):
            continue
        out[key + (norm_team(row.get("home_team")),)] = (total + spread) / 2
        out[key + (norm_team(row.get("away_team")),)] = (total - spread) / 2
    return out


_CSV: dict[str, object] = {"at": 0.0, "text": ""}


def games_csv(session: Optional[requests.Session] = None, max_age: float = 3600) -> str:
    """The whole schedule file (2 MB), downloaded at most once an hour."""
    if _CSV["text"] and time.time() - float(_CSV["at"]) < max_age:
        return str(_CSV["text"])
    session = session or make_session()
    resp = session.get(GAMES_CSV, timeout=60)
    resp.raise_for_status()
    _CSV.update(at=time.time(), text=resp.text)
    return resp.text


def week_games(season: int, week: int, session: Optional[requests.Session] = None) -> list[Game]:
    return parse_games_csv(games_csv(session), season, week)
