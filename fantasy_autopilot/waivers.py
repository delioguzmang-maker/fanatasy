"""Waiver wire: who to add, who to drop, how much FAAB to bid.

Value = how many *lineup* points a move adds over the rest of the season, not
how good the player looks. For every remaining week we compute the best
possible lineup with and without the move (exact assignment over the league's
slots) and add up the differences, weighting near weeks more
(``discount`` per week), because far-away projections are less reliable.

Everything is measured against *replacement level*: a free agent you could
stream that week (the ``replacement_rank``-th best available at the position,
times ``replacement_haircut``). So a backup QB who only covers a bye week is
worth little, because a free-agent QB could cover it too.

The bid is a policy, not a fact: it converts that value into dollars with
``full_budget_ppw`` ("a player worth this many lineup points per week for the
rest of the season is worth my entire remaining budget"), capped by
``max_pct`` of the budget and by what any rival can actually bid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .models import Player
from .nfl import eligible_slots, norm_name
from .optimizer import assign, best_total


@dataclass
class WaiverSettings:
    discount: float = 0.92
    last_week: int = 17
    full_budget_ppw: float = 8.0
    max_pct: float = 0.5
    min_bid: int = 0
    avoid_round: bool = True
    min_gain: float = 8.0            # discounted lineup points needed to bother (~1 pt/week)
    max_bid_kdef: int = 2             # kickers and defenses are streamable: never pay more
    protect_starters: bool = True     # never drop someone in this week's best lineup
    position_caps: dict[str, int] = field(default_factory=lambda: {"QB": 2, "TE": 2, "K": 1, "DEF": 1})
    max_claims: int = 2
    drop_candidates: int = 4          # how many of the least valuable players to consider dropping
    replacement_rank: int = 3         # replacement level = n-th best free agent at the position
    replacement_haircut: float = 0.9  # streaming costs a move and a bit of certainty
    never_drop: set[str] = field(default_factory=set)  # names or ids


@dataclass
class ClaimPlan:
    add: Player
    drop: Optional[Player]
    gain: float                       # discounted lineup points over the horizon
    gain_by_week: dict[int, float]
    bid: int
    bid_reason: str
    on_waivers: bool
    next_weeks: list[float] = field(default_factory=list)

    @property
    def avg_gain(self) -> float:
        return sum(self.gain_by_week.values()) / max(1, len(self.gain_by_week))


def horizon(start_week: int, settings: WaiverSettings) -> list[int]:
    return list(range(start_week, max(start_week, settings.last_week) + 1))


def _weekly(p: Player, week: int) -> float:
    return p.weekly.get(week, 0.0)


def replacement_players(
    free_agents: Iterable[Player], slots: Sequence[str], weeks: Sequence[int], settings: WaiverSettings
) -> list[Player]:
    """One phantom "streamer" per position with the replacement-level projection each week."""
    free_agents = list(free_agents)
    out = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        elig = eligible_slots([pos], slots)
        if not elig:
            continue
        weekly = {}
        for w in weeks:
            vals = sorted((_weekly(c, w) for c in free_agents if c.position == pos), reverse=True)
            k = settings.replacement_rank
            weekly[w] = settings.replacement_haircut * vals[k - 1] if len(vals) >= k else 0.0
        out.append(Player(pid=f"repl-{pos}", name=f"reemplazo {pos}", position=pos, eligible=elig, weekly=weekly))
    return out


def lineup_value(
    players: Sequence[Player],
    slots: Sequence[str],
    weeks: Sequence[int],
    discount: float,
    replacements: Sequence[Player] = (),
) -> tuple[float, dict[int, float]]:
    pool = list(players) + list(replacements)
    per_week = {w: best_total(pool, slots, lambda p, w=w: _weekly(p, w)) for w in weeks}
    total = sum(v * discount ** i for i, (w, v) in enumerate(sorted(per_week.items())))
    return total, per_week


def _protected(p: Player, never_drop: set[str]) -> bool:
    keys = {norm_name(x) for x in never_drop} | set(never_drop)
    return p.pid in keys or norm_name(p.name) in keys


def drop_costs(
    roster: Sequence[Player],
    slots: Sequence[str],
    weeks: Sequence[int],
    settings: WaiverSettings,
    replacements: Sequence[Player] = (),
) -> dict[str, float]:
    """Lineup points lost (discounted) if each player left the roster."""
    base, _ = lineup_value(roster, slots, weeks, settings.discount, replacements)
    out = {}
    for p in roster:
        if _protected(p, settings.never_drop):
            continue
        rest = [x for x in roster if x.pid != p.pid]
        out[p.pid] = base - lineup_value(rest, slots, weeks, settings.discount, replacements)[0]
    return out


def suggest_bid(
    gain: float,
    weeks: Sequence[int],
    balance: Optional[int],
    settings: WaiverSettings,
    rival_max_balance: Optional[int] = None,
    on_waivers: bool = True,
    position: str = "",
) -> tuple[int, str]:
    """FAAB bid for a claim worth `gain` discounted lineup points."""
    if not on_waivers:
        return 0, "agente libre: se agrega de inmediato, sin puja"
    if balance is None:
        return settings.min_bid, "no se conoce tu saldo FAAB; puja mínima"
    eff_weeks = sum(settings.discount ** i for i in range(len(weeks))) or 1.0
    full_value = settings.full_budget_ppw * eff_weeks
    raw = balance * gain / full_value
    cap = int(balance * settings.max_pct)
    reason = (f"{gain:.1f} pts ÷ {full_value:.0f} pts (= {settings.full_budget_ppw:g} pts/sem × "
              f"{eff_weeks:.1f} semanas efectivas) × saldo ${balance} = ${raw:.1f}")
    bid = int(round(raw))
    if rival_max_balance is not None and bid > rival_max_balance + 1:
        bid = rival_max_balance + 1
        reason += f"; tope ${bid}: ningún rival tiene más de ${rival_max_balance}"
    if bid > cap:
        bid = cap
        reason += f"; tope {settings.max_pct:.0%} del saldo = ${cap}"
    if position in ("K", "DEF") and bid > settings.max_bid_kdef:
        bid = settings.max_bid_kdef
        reason += f"; tope ${bid} para {position} (se consiguen gratis casi cada semana)"
    bid = max(settings.min_bid, min(bid, balance))
    if settings.avoid_round and bid >= 5 and bid % 5 == 0 and bid + 1 <= min(balance, max(cap, 1)):
        bid += 1
        reason += "; +$1 para no empatar con pujas redondas"
    return bid, reason


def plan_claims(
    roster: Sequence[Player],
    candidates: Iterable[Player],
    slots: Sequence[str],
    start_week: int,
    roster_limit: int,
    balance: Optional[int],
    settings: Optional[WaiverSettings] = None,
    rival_max_balance: Optional[int] = None,
    replacements: Optional[Sequence[Player]] = None,
) -> list[ClaimPlan]:
    """Best add/drop moves, each valued on the roster that results from the previous ones.

    `roster` should exclude players sitting in an IR slot (they do not take a
    roster spot); `roster_limit` = starting slots + bench spots.
    """
    settings = settings or WaiverSettings()
    weeks = horizon(start_week, settings)
    current = list(roster)
    budget = balance
    claims: list[ClaimPlan] = []
    pool = [c for c in candidates if c.pid not in {p.pid for p in roster}]
    if replacements is None:
        replacements = replacement_players(pool, slots, weeks, settings)
    repl = list(replacements)
    for _ in range(settings.max_claims):
        base, base_weeks = lineup_value(current, slots, weeks, settings.discount, repl)
        costs = drop_costs(current, slots, weeks, settings, repl)
        drops: list[Optional[Player]] = []
        if len(current) < roster_limit:
            drops.append(None)
        if settings.protect_starters:
            this_week = assign(current, slots, lambda p, s: _weekly(p, weeks[0]) + 1e-9)
            for p in this_week:
                if p is not None:
                    costs.pop(p.pid, None)
        cheapest = sorted(costs, key=costs.get)[: settings.drop_candidates]
        drops += [next(p for p in current if p.pid == pid) for pid in cheapest]
        best: Optional[tuple[float, Player, Optional[Player], dict[int, float]]] = None
        for cand in pool:
            for drop in drops:
                trial = [p for p in current if drop is None or p.pid != drop.pid] + [cand]
                cap = settings.position_caps.get(cand.position)
                if cap is not None and sum(1 for p in trial if p.position == cand.position) > cap:
                    continue  # e.g. a third QB just to cover one bye week
                value, per_week = lineup_value(trial, slots, weeks, settings.discount, repl)
                gain = value - base
                if best is None or gain > best[0] + 1e-9:
                    best = (gain, cand, drop, {w: per_week[w] - base_weeks[w] for w in weeks})
        if best is None or best[0] < settings.min_gain:
            break
        gain, cand, drop, by_week = best
        on_waivers = cand.owner.upper().startswith("W")
        bid, reason = suggest_bid(gain, weeks, budget, settings, rival_max_balance, on_waivers, cand.position)
        claims.append(ClaimPlan(cand, drop, gain, by_week, bid, reason, on_waivers,
                                next_weeks=[_weekly(cand, w) for w in weeks[:4]]))
        current = [p for p in current if drop is None or p.pid != drop.pid] + [cand]
        pool = [c for c in pool if c.pid != cand.pid]
        if budget is not None:
            budget -= bid
    return claims
