"""ESPN public scoreboard: kickoff time and state of every NFL game of a week."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from ..models import Game
from ..nfl import norm_team
from .http import make_session

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def parse_iso(value: str) -> Optional[datetime]:
    """'2026-09-27T17:00Z' / '2026-09-27T17:00:00Z' / with offset -> aware UTC datetime."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", v):
        v = v[:16] + ":00" + v[16:]
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_scoreboard(data: dict[str, Any]) -> list[Game]:
    games: list[Game] = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): norm_team((c.get("team") or {}).get("abbreviation")) for c in comp.get("competitors", [])}
        kickoff = parse_iso(comp.get("date") or ev.get("date", ""))
        if not kickoff or "home" not in teams or "away" not in teams:
            continue
        state = ((ev.get("status") or comp.get("status") or {}).get("type") or {}).get("state", "pre")
        games.append(Game(home=teams["home"], away=teams["away"], kickoff=kickoff, state=state, game_id=str(ev.get("id", ""))))
    return games


def week_games(season: int, week: int, session: Optional[requests.Session] = None) -> list[Game]:
    session = session or make_session()
    resp = session.get(SCOREBOARD_URL, params={"dates": season, "seasontype": 2, "week": week}, timeout=30)
    resp.raise_for_status()
    return parse_scoreboard(resp.json())


def games_by_team(games: list[Game]) -> dict[str, Game]:
    out: dict[str, Game] = {}
    for g in games:
        out[g.home] = g
        out[g.away] = g
    return out
