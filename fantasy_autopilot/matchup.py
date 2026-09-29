"""Matchup view: win probability and mistakes in the opponent's lineup."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import Player
from .optimizer import best_total
from .status import is_out

# Weekly scoring volatility as a fraction of the projection (coefficient of
# variation). Rough public estimates; used only for the win-probability number.
CV = {"QB": 0.40, "RB": 0.55, "WR": 0.60, "TE": 0.65, "K": 0.45, "DEF": 0.70}


def lineup_sd(players: Iterable[Player]) -> float:
    return math.sqrt(sum((CV.get(p.position, 0.6) * max(p.proj, 0.0)) ** 2 for p in players))


def win_probability(my_mean: float, my_sd: float, opp_mean: float, opp_sd: float) -> float:
    sd = math.sqrt(my_sd ** 2 + opp_sd ** 2)
    if sd < 1e-9:  # every game is over: the score decides
        return 1.0 if my_mean > opp_mean else 0.0 if my_mean < opp_mean else 0.5
    z = (my_mean - opp_mean) / sd
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class TeamOutlook:
    mean: float        # expected final score (points already scored + what is still to come)
    sd: float          # uncertainty of what is still to come
    scored: float      # points already on the board
    pending: int       # starters whose game has not finished


def team_outlook(starters: Iterable[Player]) -> TeamOutlook:
    mean = var = scored = 0.0
    pending = 0
    for p in starters:
        m, still_open = p.outlook()
        mean += m
        var += (CV.get(p.position, 0.6) * max(p.proj, 0.0) * still_open) ** 2
        if p.game_state in ("in", "post") and p.actual is not None:
            scored += p.actual
        if p.game_state != "post":
            pending += 1
    return TeamOutlook(mean, math.sqrt(var), scored, pending)


def started(p: Player) -> bool:
    return p.game_state in ("in", "post")


def movable_part(players: Sequence[Player], slots: Sequence[str]) -> tuple[list[Player], list[str]]:
    """Players whose games have not started, and the slots they can still fill."""
    free = list(slots)
    for p in players:
        if p.is_starter and started(p) and p.slot in free:
            free.remove(p.slot)
    return [p for p in players if p.slot != "IR" and not started(p)], free


@dataclass
class LineupAudit:
    current: float                   # what the lineup projects as set now
    optimal: float                   # best lineup available from the same roster
    issues: list[str] = field(default_factory=list)
    starters: list[Player] = field(default_factory=list)

    @property
    def fixable(self) -> float:
        return self.optimal - self.current


def _points(p: Player) -> float:
    return 0.0 if (p.on_bye or is_out(p.status)) else max(p.proj, 0.0)


def audit_lineup(players: Sequence[Player], slots: Sequence[str]) -> LineupAudit:
    """How much a (usually the opponent's) lineup leaves on the bench."""
    active = [p for p in players if p.slot != "IR"]
    starters = [p for p in active if p.is_starter]
    current = sum(_points(p) for p in starters)
    optimal = best_total(active, slots, _points)
    issues = []
    bench = sorted((p for p in active if not p.is_starter), key=lambda p: -_points(p))
    flagged: set[str] = set()
    for p in starters:
        if _points(p) == 0.0:
            why = "bye" if p.on_bye else (p.status or "0 pts")
            sub = next((b for b in bench if b.can_fill(p.slot) and _points(b) > 0 and b.pid not in flagged), None)
            tail = f"; podría poner a {sub.name} ({_points(sub):.2f})" if sub else ""
            issues.append(f"{p.name} ({why}) está de titular con 0.00{tail}")
            if sub:
                flagged.add(sub.pid)
    for b in bench:
        if b.pid in flagged:
            continue
        worse = [s for s in starters if b.can_fill(s.slot) and 0 < _points(s) < _points(b) - 0.01]
        if worse and _points(b) > 0:
            w = min(worse, key=_points)
            issues.append(f"{b.name} ({_points(b):.2f}) está en la banca y {w.name} ({_points(w):.2f}) juega")
    return LineupAudit(current=current, optimal=optimal, issues=issues, starters=starters)
