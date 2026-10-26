"""What past weeks teach: the league's real scoring, and who keeps missing his projection.

* ``scoring_bonus``: Sleeper counts 4 points per passing touchdown and -1 per
  interception. Comparing Sleeper's stats with the points Yahoo gave the same
  players finds this league's rule (e.g. 6 and -2 -> {"pass_td": 2, "pass_int": -1}),
  so Sleeper-based projections are in the league's scoring.
* ``chronic_misses``: your players who scored below (or above) Yahoo's projection
  in most weeks. Information for you; the own projections (ownproj.py) already learn
  each player's misses with a brake, so one wild game barely moves them.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import product
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from .history import PastRow
from .models import Player
from .nfl import eligible_slots
from .optimizer import assign
from .sources.sleeper import league_points

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "K", "DEF"]
BONUS_GRID = {"pass_td": (0.0, 1.0, 2.0), "pass_int": (0.0, -1.0), "pass_yd": (0.0, 0.01)}


def scoring_bonus(pairs: Iterable[tuple[dict[str, Any], float]], reception_points: float = 0.5,
                  min_better: float = 0.2) -> dict[str, float]:
    """`pairs` = (Sleeper stats of a passer's game, points Yahoo gave him). Returns the extra
    points per stat that make Sleeper match the league ({} when Sleeper already matches)."""
    pairs = [(s, float(y)) for s, y in pairs if s and y is not None and (s.get("pass_att") or s.get("pass_td")
                                                                         or s.get("pass_yd"))]
    if len(pairs) < 2:
        return {}

    def mae(bonus: dict[str, float]) -> float:
        return float(np.mean([abs((league_points(s, reception_points, bonus) or 0.0) - y) for s, y in pairs]))

    base = mae({})
    best, best_err = {}, base
    for values in product(*BONUS_GRID.values()):
        bonus = {k: v for k, v in zip(BONUS_GRID, values) if v}
        err = mae(bonus)
        if err < best_err - 1e-9:
            best, best_err = bonus, err
    return best if base - best_err >= min_better else {}


def chronic_misses(rows: Iterable[PastRow], mine: Callable[[PastRow], bool], min_games: int = 4) -> list[str]:
    by = defaultdict(list)
    for r in rows:
        if mine(r) and r.usable:
            by[r.name].append(r.pts - r.proj)
    out = []
    for name, d in by.items():
        if len(d) < min_games:
            continue
        below, above = sum(1 for x in d if x < 0), sum(1 for x in d if x > 0)
        if below >= 0.8 * len(d) or above >= 0.8 * len(d):
            word = "por debajo" if below > above else "por encima"
            out.append(f"{name}: {max(below, above)} de {len(d)} semanas {word} de Yahoo, "
                       f"promedio {np.mean(d):+.1f} pts")
    return sorted(out)


def lineup_points(rows: Sequence[PastRow], value: Callable[[PastRow], Optional[float]],
                  slots: Sequence[str] = SLOTS) -> float:
    """Real points of the lineup a projection would have chosen from these players."""
    players = [Player(pid=str(i), name=r.name, position=r.pos, eligible=eligible_slots([r.pos], slots),
                      proj=value(r) or 0.0) for i, r in enumerate(rows)]
    chosen = assign(players, slots, lambda p, s: p.proj + 1e-9)
    return float(sum(rows[int(p.pid)].pts or 0.0 for p in chosen if p is not None))
