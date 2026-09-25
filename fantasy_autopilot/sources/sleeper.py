"""Sleeper public API: NFL state, player database (injuries, practice), projections.

No account or key needed. The player database is ~5 MB and Sleeper asks to
download it at most once a day, so it is cached on disk.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

from ..nfl import norm_name, norm_team
from .http import make_session

STATE_URL = "https://api.sleeper.app/v1/state/nfl"
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/add"
# Projections live on the .com host and need a browser User-Agent.
PROJECTIONS_URL = "https://api.sleeper.com/projections/nfl/{season}/{week}"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


class Sleeper:
    def __init__(self, cache_dir: Optional[Path] = None, session: Optional[requests.Session] = None):
        self.session = session or make_session()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._players: Optional[dict[str, dict[str, Any]]] = None
        self._by_yahoo: Optional[dict[str, dict[str, Any]]] = None
        self._by_name: Optional[dict[tuple[str, str], dict[str, Any]]] = None

    def _get(self, url: str, params: Any = None, timeout: int = 30) -> Any:
        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def state(self) -> dict[str, Any]:
        """{'season': '2026', 'week': 3, 'season_type': 'regular', ...}"""
        return self._get(STATE_URL)

    def players(self, max_age_hours: float = 12.0) -> dict[str, dict[str, Any]]:
        if self._players is not None:
            return self._players
        cache = self.cache_dir / "sleeper_players.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
            self._players = json.loads(cache.read_text())
            return self._players
        data = self._get(PLAYERS_URL, timeout=90)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        self._players = data
        return data

    def by_yahoo_id(self) -> dict[str, dict[str, Any]]:
        if self._by_yahoo is None:
            self._by_yahoo = {}
            for sid, p in self.players().items():
                yid = p.get("yahoo_id")
                if yid:
                    self._by_yahoo[str(yid)] = {**p, "player_id": sid}
        return self._by_yahoo

    def match(self, yahoo_id: str = "", name: str = "", team: str = "", position: str = "") -> Optional[dict[str, Any]]:
        """Find a Sleeper player by Yahoo id, else by name (+team)."""
        if yahoo_id and yahoo_id in self.by_yahoo_id():
            return self.by_yahoo_id()[yahoo_id]
        if position == "DEF" and team:
            p = self.players().get(norm_team(team))
            return {**p, "player_id": norm_team(team)} if p else None
        if self._by_name is None:
            self._by_name = {}
            for sid, p in self.players().items():
                key = norm_name(p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}")
                if key:
                    self._by_name.setdefault((key, norm_team(p.get("team"))), {**p, "player_id": sid})
                    self._by_name.setdefault((key, ""), {**p, "player_id": sid})
        key = norm_name(name)
        return self._by_name.get((key, norm_team(team))) or self._by_name.get((key, ""))

    def projections(self, season: int, week: int, reception_points: float = 1.0) -> dict[str, float]:
        """Projected fantasy points by Sleeper player id for one week."""
        field = "pts_ppr" if reception_points >= 1 else "pts_half_ppr" if reception_points > 0 else "pts_std"
        params = [("season_type", "regular")] + [("position[]", p) for p in POSITIONS]
        rows = self._get(PROJECTIONS_URL.format(season=season, week=week), params=params, timeout=60)
        out: dict[str, float] = {}
        for row in rows if isinstance(rows, list) else []:
            stats = row.get("stats") or {}
            pid = str(row.get("player_id", ""))
            if pid and field in stats:
                out[pid] = float(stats[field] or 0.0)
        return out

    def trending_adds(self, hours: int = 24, limit: int = 40) -> list[dict[str, Any]]:
        return self._get(TRENDING_URL, params={"lookback_hours": hours, "limit": limit})


def injury_fields(p: dict[str, Any]) -> dict[str, Any]:
    """The bits of a Sleeper player we use: status, practice, freshness of the news."""
    updated = p.get("news_updated")
    return {
        "injury_status": p.get("injury_status") or "",
        "practice": p.get("practice_participation") or "",
        "news_updated": (float(updated) / 1000.0) if updated else None,
        "injury_notes": p.get("injury_notes") or p.get("injury_body_part") or "",
        "sleeper_id": p.get("player_id"),
    }
