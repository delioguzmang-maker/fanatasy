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
STATS_URL = "https://api.sleeper.com/stats/nfl/{season}/{week}"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
ROWS_TTL = 1800   # seconds a week still in play is reused (finished weeks never change)


class Sleeper:
    def __init__(self, cache_dir: Optional[Path] = None, session: Optional[requests.Session] = None):
        self.session = session or make_session()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._players: Optional[dict[str, dict[str, Any]]] = None
        self._loaded_at = 0.0
        self.max_age_hours = 12.0   # the engine lowers it to 20 minutes on game days
        self._by_yahoo: Optional[dict[str, dict[str, Any]]] = None
        self._by_name: Optional[dict[tuple[str, str], dict[str, Any]]] = None
        self._rows: dict[tuple[str, int, int], tuple[float, list[dict[str, Any]]]] = {}

    def _get(self, url: str, params: Any = None, timeout: int = 30) -> Any:
        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def state(self) -> dict[str, Any]:
        """{'season': '2026', 'week': 3, 'season_type': 'regular', ...}"""
        return self._get(STATE_URL)

    def players(self, max_age_hours: Optional[float] = None) -> dict[str, dict[str, Any]]:
        max_age_hours = self.max_age_hours if max_age_hours is None else max_age_hours
        if self._players is not None and time.time() - self._loaded_at < max_age_hours * 3600:
            return self._players
        if self._players is not None:  # stale in a long session (the Sunday loop): reload
            self._players = self._by_yahoo = self._by_name = None
        self._loaded_at = time.time()
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

    def projections(self, season: int, week: int, reception_points: float = 1.0,
                    bonus: Optional[dict[str, float]] = None) -> dict[str, float]:
        """Projected fantasy points by Sleeper player id for one week (league scoring, see league_points)."""
        out: dict[str, float] = {}
        for row in self.rows("projections", season, week):
            pid = str(row.get("player_id", ""))
            pts = league_points(row.get("stats") or {}, reception_points, bonus)
            if pid and pts is not None:
                out[pid] = pts
        return out

    def rows(self, kind: str, season: int, week: int, final: bool = False) -> list[dict[str, Any]]:
        """Rows of Sleeper's projections or stats ("projections" / "stats") for one week, every
        position, with the player's name and team (only the fields this package uses).
        `final` = the week is over: kept on disk, it does not change."""
        key = (kind, season, week)
        hit = self._rows.get(key)
        if hit and (final or time.time() - hit[0] < ROWS_TTL):
            return hit[1]
        cache = self.cache_dir / f"{kind}_{season}_{week}.json" if self.cache_dir and final else None
        if cache and cache.exists():
            self._rows[key] = (time.time(), json.loads(cache.read_text()))
            return self._rows[key][1]
        url = (PROJECTIONS_URL if kind == "projections" else STATS_URL).format(season=season, week=week)
        params = [("season_type", "regular")] + [("position[]", p) for p in POSITIONS]
        data = self._get(url, params=params, timeout=90)
        rows = [compact_row(r) for r in data] if isinstance(data, list) else []
        if cache and rows:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows))
        self._rows[key] = (time.time(), rows)
        return rows

    def weekly_stats(self, season: int, week: int) -> dict[str, dict[str, Any]]:
        """Real stats of one played week by Sleeper player id (snaps, carries, targets...).

        A finished week is cached on disk: it does not change except for small corrections."""
        cache = self.cache_dir / f"stats_{season}_{week}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text())
        params = [("season_type", "regular")] + [("position[]", p) for p in ("QB", "RB", "WR", "TE")]
        rows = self._get(STATS_URL.format(season=season, week=week), params=params, timeout=60)
        out: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                pid = str(row.get("player_id", ""))
                if pid:
                    out[pid] = row.get("stats") or {}
        elif isinstance(rows, dict):
            out = {str(k): (v or {}) for k, v in rows.items()}
        if cache and out:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out))
        return out

    def trending_adds(self, hours: int = 24, limit: int = 40) -> list[dict[str, Any]]:
        return self._get(TRENDING_URL, params={"lookback_hours": hours, "limit": limit})


def league_points(stats: dict[str, Any], reception_points: float = 1.0,
                  bonus: Optional[dict[str, float]] = None) -> Optional[float]:
    """Sleeper's points in a league's scoring: its PPR / half / standard total plus `bonus`
    points per unit of a stat. Sleeper counts 4 per passing touchdown and -1 per interception;
    a league with 6 and -2 is bonus={"pass_td": 2, "pass_int": -1}."""
    field = "pts_ppr" if reception_points >= 1 else "pts_half_ppr" if reception_points > 0 else "pts_std"
    if field not in stats:
        return None
    pts = float(stats[field] or 0.0)
    for stat, extra in (bonus or {}).items():
        pts += extra * float(stats.get(stat) or 0.0)
    return round(pts, 2)


KEEP_STATS = ("pts_ppr", "pts_half_ppr", "pts_std", "pass_att", "pass_td", "pass_int", "pass_yd", "rec",
              "rec_tgt", "rush_att", "off_snp", "tm_off_snp", "gp", "gms_active")


def compact_row(r: dict[str, Any]) -> dict[str, Any]:
    p = r.get("player") or {}
    st = r.get("stats") or {}
    return {"player_id": r.get("player_id"), "team": r.get("team"), "opponent": r.get("opponent"),
            "player": {k: p.get(k) for k in ("first_name", "last_name", "position")},
            "stats": {k: st[k] for k in KEEP_STATS if k in st}}


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    """(position, name) of a projections/stats row; defenses go by team ("DEF", "CLE")."""
    p = row.get("player") or {}
    pos = p.get("position") or ""
    if pos == "DEF":
        return pos, norm_team(row.get("team") or p.get("team") or row.get("player_id") or "")
    return pos, norm_name(f"{p.get('first_name', '')} {p.get('last_name', '')}")


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
