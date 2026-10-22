"""Weekly decision audit: was a loss bad luck, a lineup mistake, or a roster problem?

For a finished week, compare three lineups from the same roster:

* the one you played (real points),
* the best one by the projections you had before kickoff (the decision you could make),
* the best one knowing the real points (hindsight; nobody can play it, it only
  bounds what any lineup change could have done).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import Player
from .nfl import eligible_slots
from .optimizer import assign, best_total


@dataclass
class WeekAudit:
    week: int
    scored: float
    proj_used: float          # projection of the starters you played
    proj_best: float          # best lineup by projections (what the optimizer would have said)
    perfect: float            # best lineup knowing the real points
    opponent: Optional[float] = None
    came_in: tuple = ()       # bench players in the hindsight lineup: (name, points)
    went_out: tuple = ()      # starters left out of it: (name, points)

    @property
    def decision_gap(self) -> float:
        return self.proj_best - self.proj_used

    @property
    def bench_left(self) -> float:
        return self.perfect - self.scored

    def verdict(self) -> str:
        extra = (f" Además la alineación tenía {self.decision_gap:.2f} pts proyectados menos que la mejor posible: "
                 "eso sí era evitable." if self.decision_gap > 1.0 else "")
        if self.opponent is None:
            return extra.strip()
        if self.scored > self.opponent:
            return "ganaste." + extra
        if self.perfect <= self.opponent:
            return (f"perdiste por {self.opponent - self.scored:.2f} y ni la alineación perfecta ({self.perfect:.2f}) "
                    "ganaba: no fue un error de alineación." + extra)
        if self.decision_gap > 1.0:
            return f"perdiste por {self.opponent - self.scored:.2f}: error evitable." + extra
        return (f"perdiste por {self.opponent - self.scored:.2f}; la perfecta ({self.perfect:.2f}) ganaba, pero solo "
                "sabiendo el resultado: las proyecciones decían lo que jugaste.")


def week_audit(rows: Sequence[Mapping], slots: Sequence[str], opponent: Optional[float] = None) -> WeekAudit:
    """rows: one team-week (pid, jugador, pos, puesto, proy, real), IR rows excluded."""
    players, real = [], {}
    for r in rows:
        if str(r.get("puesto")) == "IR":
            continue
        pos = str(r.get("pos", ""))
        p = Player(pid=str(r["pid"]), name=str(r.get("jugador", "")), position=pos,
                   eligible=eligible_slots([pos], slots), slot=str(r.get("puesto", "BN")), proj=float(r["proy"] or 0))
        players.append(p)
        real[p.pid] = float(r["real"] or 0)
    starters = [p for p in players if p.slot not in ("BN", "IR")]
    scored = sum(real[p.pid] for p in starters)
    proj_used = sum(p.proj for p in starters)
    proj_best = best_total(players, slots, lambda p: p.proj)
    hind = assign(players, slots, lambda p, s: real[p.pid] + 1e-9)
    perfect = float(sum(real[p.pid] for p in hind if p is not None))
    chosen = {p.pid for p in hind if p is not None}
    ins = sorted((p for p in players if p.pid in chosen and p.slot in ("BN",)), key=lambda p: -real[p.pid])
    outs = sorted((p for p in starters if p.pid not in chosen), key=lambda p: real[p.pid])
    return WeekAudit(0, scored, proj_used, proj_best, perfect, opponent,
                     tuple((p.name, real[p.pid]) for p in ins), tuple((p.name, real[p.pid]) for p in outs))
