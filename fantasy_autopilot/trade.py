"""Trade evaluator: numbers first, roster shape second.

Checks, in order: totals on both sides, which of your starters leave,
whether each incoming player beats your current best at his position, and
the real test, rest-of-season *lineup* points before vs after (with
replacement-level streamers available, same as the waiver math).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .models import Player
from .optimizer import assign
from .waivers import WaiverSettings, drop_costs, horizon, lineup_value, replacement_players


@dataclass
class TradeReport:
    give: list[Player]
    get: list[Player]
    ros_out: float
    ros_in: float
    before: float
    after: float
    per_week: dict[int, float]
    checks: list[str] = field(default_factory=list)
    drops: list[Player] = field(default_factory=list)
    verdict: str = ""

    @property
    def delta(self) -> float:
        return self.after - self.before


def _ros(p: Player, weeks: Sequence[int]) -> float:
    return sum(p.weekly.get(w, 0.0) for w in weeks)


def evaluate_trade(
    roster: Sequence[Player],
    give: Sequence[Player],
    get: Sequence[Player],
    slots: Sequence[str],
    start_week: int,
    roster_limit: int,
    settings: Optional[WaiverSettings] = None,
    free_agents: Sequence[Player] = (),
    threshold: float = 5.0,
) -> TradeReport:
    settings = settings or WaiverSettings()
    weeks = horizon(start_week, settings)
    repl = replacement_players(free_agents, slots, weeks, settings) if free_agents else []
    give_ids = {p.pid for p in give}
    after_roster = [p for p in roster if p.pid not in give_ids] + list(get)

    drops: list[Player] = []
    while len(after_roster) > roster_limit:
        costs = drop_costs(after_roster, slots, weeks, settings, repl)
        costs = {k: v for k, v in costs.items() if k not in {p.pid for p in get}}
        if not costs:
            break
        cheapest = min(costs, key=costs.get)
        drops.append(next(p for p in after_roster if p.pid == cheapest))
        after_roster = [p for p in after_roster if p.pid != cheapest]

    before, before_w = lineup_value(roster, slots, weeks, settings.discount, repl)
    after, after_w = lineup_value(after_roster, slots, weeks, settings.discount, repl)
    report = TradeReport(
        give=list(give), get=list(get),
        ros_out=sum(_ros(p, weeks) for p in give),
        ros_in=sum(_ros(p, weeks) for p in get),
        before=before, after=after,
        per_week={w: after_w[w] - before_w[w] for w in weeks},
        drops=drops,
    )

    report.checks.append(
        f"1) Totales resto de temporada: das {report.ros_out:.1f} pts, recibes {report.ros_in:.1f} pts "
        f"({report.ros_in - report.ros_out:+.1f})."
    )
    first = weeks[0]
    chosen = assign(list(roster), slots, lambda p, s: p.weekly.get(first, 0.0) + 1e-9)
    starters = {p.pid for p in chosen if p is not None}
    leaving = [p.name for p in give if p.pid in starters]
    report.checks.append(
        "2) Titulares que se van: " + (", ".join(leaving) if leaving else "ninguno (solo banca).")
    )
    for p in get:
        same = [r for r in roster if r.position == p.position and r.pid not in give_ids]
        best = max(same, key=lambda r: _ros(r, weeks), default=None)
        if best is None:
            report.checks.append(f"3) {p.name}: no tienes otro {p.position}; llena un hueco.")
        else:
            diff = _ros(p, weeks) - _ros(best, weeks)
            word = "mejora" if diff > 0 else "NO mejora"
            report.checks.append(
                f"3) {p.name} ({_ros(p, weeks):.1f}) {word} a tu mejor {p.position}, {best.name} ({_ros(best, weeks):.1f})."
            )
    if drops:
        report.checks.append("   Para hacer espacio tendrías que soltar: " + ", ".join(d.name for d in drops) + ".")
    report.checks.append(
        f"4) Puntos de alineación (ponderados) antes {before:.1f} → después {after:.1f} ({report.delta:+.1f})."
    )
    report.checks.append("5) Antes de enviarlo, pásalo también por el evaluador de trades de Yahoo.")
    if report.delta >= threshold:
        report.verdict = f"ACEPTAR: sube tu alineación {report.delta:+.1f} pts en lo que queda."
    elif report.delta <= -threshold:
        report.verdict = f"RECHAZAR: baja tu alineación {report.delta:+.1f} pts en lo que queda."
    else:
        report.verdict = f"NEUTRAL ({report.delta:+.1f} pts): no cambia mucho; decide por otra razón concreta."
    return report
