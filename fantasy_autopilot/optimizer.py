"""Lineup optimizer.

Two layers:

* ``assign`` solves the classic problem: put the best eligible players in the
  starting slots (max-weight bipartite matching, exact).
* ``optimize_lineup`` maximizes *expected* points for this week. A player who
  may be inactive (Q/D) is worth ``p * proj`` on his own, but if a bench player
  whose game starts at the same time or later can replace him when inactives
  are announced (~90 min before kickoff), the lineup keeps most of the value.
  That "hedge" only exists if someone (this autopilot) makes the swap, so its
  value is discounted by ``hedge_reliability``.

Nothing here talks to Yahoo; it only works on ``Player`` objects.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

from .models import Assignment, LineupPlan, Move, Player
from .nfl import is_flex, norm_slot

NEG = -1e9
BIG = 1e5


@dataclass
class OptimizerSettings:
    hedge_reliability: float = 0.85   # chance the autopilot executes an inactive swap in time
    inactive_lead_minutes: int = 90   # NFL inactive lists: ~90 min before kickoff
    poll_margin_minutes: int = 20     # worst-case delay before the autopilot notices
    assume_autoswap: bool = True      # False -> nobody swaps on game day, no hedge value
    max_uncertain: int = 8            # cap for the exact scenario enumeration
    stickiness: float = 1e-4          # prefer the current slot when values tie
    flex_bonus: float = 1e-2          # prefer flex slots for uncertain starters when it helps
    uncertain_penalty: float = 0.75   # risk premium (points) per starter who may be inactive


# --------------------------------------------------------------------------
# exact slot assignment
# --------------------------------------------------------------------------

def assign(
    players: Sequence[Player],
    slots: Sequence[str],
    value: Callable[[Player, str], float],
) -> list[Optional[Player]]:
    """Max-weight assignment of players to slots. A slot may stay empty (None)."""
    n, m = len(players), len(slots)
    if m == 0:
        return []
    if n == 0:
        return [None] * m
    mat = np.zeros((n + m, m))  # m dummy rows = "leave this slot empty" (value 0)
    for i, p in enumerate(players):
        for j, s in enumerate(slots):
            mat[i, j] = value(p, s) if p.can_fill(s) else NEG
    rows, cols = linear_sum_assignment(mat, maximize=True)
    out: list[Optional[Player]] = [None] * m
    for r, c in zip(rows, cols):
        if r < n and mat[r, c] > NEG / 2:
            out[c] = players[r]
    return out


def best_total(players: Sequence[Player], slots: Sequence[str], points: Callable[[Player], float]) -> float:
    """Best achievable lineup total when each player scores `points(p)` (used for ROS math)."""
    chosen = assign(players, slots, lambda p, s: points(p) + 1e-9)
    return float(sum(points(p) for p in chosen if p is not None))


# --------------------------------------------------------------------------
# expected value with game-day swaps
# --------------------------------------------------------------------------

def _acted_at(starter: Player, settings: OptimizerSettings) -> Optional[datetime]:
    """When the autopilot can react to `starter` being declared inactive."""
    if starter.kickoff is None:
        return None
    return starter.kickoff - timedelta(minutes=settings.inactive_lead_minutes - settings.poll_margin_minutes)


def can_hedge(backup: Player, starter: Player, settings: OptimizerSettings) -> bool:
    """Could `backup` still enter the lineup when `starter` is announced inactive?"""
    acted_at = _acted_at(starter, settings)
    if backup.locked or backup.kickoff is None or acted_at is None:
        return False
    if backup.prob <= 0 or backup.proj <= 0:
        return False
    return backup.kickoff > acted_at


def lineup_expected_points(
    starters: Sequence[tuple[str, Player]],
    bench: Sequence[Player],
    settings: OptimizerSettings,
) -> tuple[float, dict[str, str]]:
    """Expected points of a lineup, counting automatic swaps of inactive starters.

    A replacement is either a bench player eligible for the vacated slot, or a
    two-step move: an unlocked flex starter slides into the vacated slot and a
    bench player takes the flex. Returns (ev, hedges) where hedges maps
    starter pid -> backup pid used when only that starter is inactive.
    """
    certain = 0.0
    uncertain: list[tuple[str, Player]] = []
    for slot, p in starters:
        if p.locked:
            certain += p.proj if p.prob > 0 else 0.0
        elif p.prob >= 1.0:
            certain += p.proj
        else:
            uncertain.append((slot, p))

    if not uncertain:
        return certain, {}
    if not settings.assume_autoswap:
        return certain + sum(p.prob * p.proj for _, p in uncertain), {}

    far = datetime.max.replace(tzinfo=timezone.utc)
    uncertain.sort(key=lambda sp: (sp[1].kickoff is None, sp[1].kickoff or far))
    exact, approx = uncertain[: settings.max_uncertain], uncertain[settings.max_uncertain:]
    extra = sum(p.prob * p.proj for _, p in approx)
    pool = [b for b in bench if not b.locked]
    flex_starters = [(s, p) for s, p in starters if is_flex(s) and not p.locked]

    def replacement(
        slot: str, starter: Player, used: set[str], inactive: set[str]
    ) -> tuple[Optional[Player], Optional[Player]]:
        """Best (bench player, flex starter that slides over or None)."""
        acted_at = _acted_at(starter, settings)
        best: tuple[Optional[Player], Optional[Player]] = (None, None)
        best_val = 0.0
        for b in pool:
            if b.pid in used or not can_hedge(b, starter, settings):
                continue
            val = b.prob * b.proj
            if val <= best_val:
                continue
            if b.can_fill(slot):
                best, best_val = (b, None), val
                continue
            # two-step: a flex starter who has not played yet slides into `slot`
            for fslot, f in flex_starters:
                if (
                    f.pid != starter.pid
                    and f.pid not in inactive
                    and f.pid not in used
                    and f.can_fill(slot)
                    and f.kickoff is not None
                    and acted_at is not None
                    and f.kickoff > acted_at
                    and b.can_fill(fslot)
                ):
                    best, best_val = (b, f), val
                    break
        return best

    ev = 0.0
    for outcome in itertools.product((True, False), repeat=len(exact)):
        prob = 1.0
        for active, (_, p) in zip(outcome, exact):
            prob *= p.prob if active else (1.0 - p.prob)
        if prob <= 0.0:
            continue
        inactive = {p.pid for active, (_, p) in zip(outcome, exact) if not active}
        pts, used = 0.0, set()
        for active, (slot, p) in zip(outcome, exact):
            if active:
                pts += p.proj
                continue
            b, slid = replacement(slot, p, used, inactive)
            if b is not None:
                used.add(b.pid)
                if slid is not None:
                    used.add(slid.pid)  # that flex starter already moved once
                pts += settings.hedge_reliability * b.prob * b.proj
        ev += prob * pts

    hedges: dict[str, str] = {}
    for slot, p in exact:
        b, _ = replacement(slot, p, set(), {p.pid})
        if b is not None:
            hedges[p.pid] = b.pid
    return certain + extra + ev, hedges


# --------------------------------------------------------------------------
# weekly optimizer
# --------------------------------------------------------------------------

def _is_uncertain(p: Player) -> bool:
    return 0.0 < p.prob < 1.0


def optimize_lineup(
    players: Iterable[Player],
    slots: Sequence[str],
    settings: Optional[OptimizerSettings] = None,
    pins_start: Iterable[str] = (),
    pins_bench: Iterable[str] = (),
) -> LineupPlan:
    """Best lineup for the week, respecting locks, pins and game-day swap hedges."""
    settings = settings or OptimizerSettings()
    slots = [norm_slot(s) for s in slots]
    pins_start, pins_bench = set(pins_start), set(pins_bench)
    roster = [p for p in players if p.slot != "IR"]

    # Locked starters keep their slot; their slot indices leave the problem.
    fixed: dict[int, Player] = {}
    open_idx = list(range(len(slots)))
    for p in roster:
        if p.locked and p.is_starter:
            idx = next((i for i in open_idx if slots[i] == p.slot), None)
            if idx is not None:
                fixed[idx] = p
                open_idx.remove(idx)
    open_slots = [slots[i] for i in open_idx]
    pool = [p for p in roster if not p.locked]
    locked_bench = [p for p in roster if p.locked and not p.is_starter]
    startable = [p for p in pool if p.pid not in pins_bench]

    def place(starter_set: Sequence[Player], flex_pref: bool) -> Optional[list[Optional[Player]]]:
        def val(p: Player, s: str) -> float:
            v = BIG
            if p.slot == s:
                v += settings.stickiness
            if flex_pref and _is_uncertain(p) and is_flex(s):
                v += settings.flex_bonus
            return v

        placed = assign(list(starter_set), open_slots, val)
        if sum(1 for p in placed if p is not None) < len(starter_set):
            return None
        return placed

    def evaluate(placed: list[Optional[Player]]) -> tuple[float, float, dict[str, str]]:
        """(objective, expected points, hedges). The objective charges a risk
        premium for every unlocked starter who may end up inactive."""
        starters = [(slots[i], p) for i, p in fixed.items()]
        starters += [(s, p) for s, p in zip(open_slots, placed) if p is not None]
        chosen = {p.pid for _, p in starters}
        bench = [p for p in pool if p.pid not in chosen]
        ev, hedges = lineup_expected_points(starters, bench, settings)
        risky = sum(1 for _, p in starters if not p.locked and _is_uncertain(p))
        return ev - settings.uncertain_penalty * risky, ev, hedges

    def best_placement(starter_set: Sequence[Player]) -> Optional[tuple[float, float, list, dict]]:
        best = None
        for flex_pref in (False, True):
            placed = place(starter_set, flex_pref)
            if placed is None:
                continue
            obj, ev, hedges = evaluate(placed)
            if best is None or obj > best[0] + 1e-6:
                best = (obj, ev, placed, hedges)
        return best

    # 1) candidate starter sets: every subset of uncertain players forced in.
    uncertain = sorted(
        (p for p in startable if _is_uncertain(p) and p.pid not in pins_start), key=lambda p: -p.proj
    )
    head = uncertain[: settings.max_uncertain]
    candidates: list[tuple[float, float, list, dict]] = []
    for r in range(len(head) + 1):
        for subset in itertools.combinations(head, r):
            forced = {p.pid for p in subset}
            excluded = {p.pid for p in head} - forced

            def value(p: Player, s: str, forced=forced) -> float:
                v = p.prob * p.proj if p.pid not in forced else BIG + p.proj
                if p.pid in pins_start:
                    v += BIG
                if p.slot == s:
                    v += settings.stickiness
                return v + (1e-6 if p.prob > 0 else 0.0)

            chosen = assign([p for p in startable if p.pid not in excluded], open_slots, value)
            starter_set = [p for p in chosen if p is not None]
            if not forced.issubset({p.pid for p in starter_set}):
                continue
            result = best_placement(starter_set)
            if result:
                candidates.append(result)

    if not candidates:  # nothing startable at all
        empty: list[Optional[Player]] = [None] * len(open_slots)
        obj0, ev0, hedges0 = evaluate(empty)
        candidates.append((obj0, ev0, empty, hedges0))

    current_pids = {p.pid for p in roster if p.is_starter}

    def churn(placed: list[Optional[Player]]) -> int:
        return sum(1 for p in placed if p is not None and p.pid not in current_pids)

    candidates.sort(key=lambda c: (-round(c[0], 6), churn(c[2])))
    obj, ev, placed, hedges = candidates[0]

    # 2) local search: single starter <-> bench swaps (keeps late-game hedges on the bench)
    improved = True
    guard = 0
    while improved and guard < 60:
        improved, guard = False, guard + 1
        starter_set = [p for p in placed if p is not None]
        in_lineup = {p.pid for p in starter_set}
        for out_p in [p for p in starter_set if p.pid not in pins_start]:
            for in_p in [p for p in startable if p.pid not in in_lineup]:
                trial = [p for p in starter_set if p.pid != out_p.pid] + [in_p]
                result = best_placement(trial)
                if result and result[0] > obj + 1e-6:
                    obj, ev, placed, hedges = result
                    improved = True
                    break
            if improved:
                break

    assignments: list[Assignment] = []
    placed_iter = iter(placed)
    for i, slot in enumerate(slots):
        if i in fixed:
            assignments.append(Assignment(slot, fixed[i]))
        else:
            assignments.append(Assignment(slot, next(placed_iter)))
    starters_now = {a.player.pid for a in assignments if a.player}
    bench = sorted(
        [p for p in pool if p.pid not in starters_now] + locked_bench,
        key=lambda p: -p.proj,
    )
    naive = sum(a.player.proj for a in assignments if a.player and a.player.prob > 0)
    return LineupPlan(assignments=assignments, bench=bench, ev=ev, naive=naive, hedges=hedges)


def current_plan(players: Iterable[Player], slots: Sequence[str], settings: Optional[OptimizerSettings] = None) -> LineupPlan:
    """The lineup exactly as it is on Yahoo right now, valued the same way."""
    settings = settings or OptimizerSettings()
    slots = [norm_slot(s) for s in slots]
    roster = [p for p in players if p.slot != "IR"]
    remaining = [p for p in roster if p.is_starter]
    assignments: list[Assignment] = []
    for slot in slots:
        p = next((x for x in remaining if x.slot == slot), None)
        if p is not None:
            remaining.remove(p)
        assignments.append(Assignment(slot, p))
    starters = [(a.slot, a.player) for a in assignments if a.player]
    chosen = {p.pid for _, p in starters}
    bench = [p for p in roster if p.pid not in chosen]
    ev, hedges = lineup_expected_points(starters, bench, settings)
    naive = sum(p.proj for _, p in starters if p.prob > 0)  # players ruled out score 0
    return LineupPlan(assignments=assignments, bench=sorted(bench, key=lambda p: -p.proj), ev=ev, naive=naive, hedges=hedges)


def plan_moves(players: Iterable[Player], plan: LineupPlan) -> list[Move]:
    """Slot changes needed to go from the current Yahoo lineup to `plan`."""
    target = plan.target_slots()
    moves = []
    for p in players:
        if p.slot == "IR" or p.pid not in target:
            continue
        if target[p.pid] != p.slot:
            moves.append(Move(p, p.slot, target[p.pid]))
    # bench first, then fill: Yahoo rejects moving into an occupied slot mid-way
    moves.sort(key=lambda m: (m.to_slot != "BN", m.to_slot))
    return moves


def starters_changed(players: Iterable[Player], plan: LineupPlan) -> bool:
    current = {p.pid for p in players if p.is_starter}
    return current != {p.pid for p in plan.starters()}


def plan_objective(plan: LineupPlan, settings: Optional[OptimizerSettings] = None) -> tuple[float, float]:
    """(objective, expected points) of any plan, e.g. the current Yahoo lineup."""
    settings = settings or OptimizerSettings()
    starters = [(a.slot, a.player) for a in plan.assignments if a.player]
    ev, _ = lineup_expected_points(starters, plan.bench, settings)
    risky = sum(1 for _, p in starters if not p.locked and _is_uncertain(p))
    return ev - settings.uncertain_penalty * risky, ev


def breakeven_probability(player: Player, plan: LineupPlan, settings: Optional[OptimizerSettings] = None) -> Optional[float]:
    """Chance of playing a benched player needs to beat the weakest starter he could replace."""
    settings = settings or OptimizerSettings()
    rivals = [a.player for a in plan.assignments if a.player and not a.player.locked and player.can_fill(a.slot)]
    if not rivals or player.proj <= 0:
        return None
    weakest = min(rivals, key=lambda p: p.expected)
    return min(1.0, (weakest.expected + settings.uncertain_penalty) / player.proj)
