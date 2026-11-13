"""Were the suggestions good? Every suggestion checked against the points really scored.

* Lineups: the last recommendation of each week (decisiones_log.jsonl) against the
  lineup you played and the best one possible with the same roster.
* Trades: points scored after the trade by what you got and by what you gave.
* Adds: points of each player you added, against the one you dropped for him.
* Suggested claims and "win the week" plans (logged since version 0.3): what the
  suggested player scored next, against the player it said to drop.

Real points come from Yahoo where the notebook saved them (cell 7d's history) and
otherwise from Sleeper's stats in the league's scoring (kickers are approximate).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .models import Player
from .nfl import eligible_slots, norm_name, norm_slot, team_from_name
from .optimizer import assign

Points = Callable[[int, str, str], Optional[float]]   # (week, name, position) -> real points


def name_key(name: str, pos: str = "") -> str:
    """Players by normalized name; defenses ("Lions", "Detroit") by team abbreviation."""
    if pos in ("DEF", ""):
        team = team_from_name(name)
        if team:
            return f"DEF:{team}"
    return norm_name(name)


@dataclass
class LineupWeek:
    week: int
    played: float
    recommended: float
    best: float
    followed: bool
    differences: list[str] = field(default_factory=list)   # "Strange 9.0 en vez de LaPorta 3.1"
    missing: list[str] = field(default_factory=list)       # players without real points yet


def _starters(slots: Mapping[str, str]) -> dict[str, str]:
    return {n: norm_slot(s) for n, s in slots.items() if norm_slot(s) not in ("BN", "IR", "")}


def lineup_weeks(decisions: Sequence[Mapping], points: Points, positions: Mapping[str, str]) -> list[LineupWeek]:
    """One row per week with the last logged decision of that week."""
    last: dict[int, Mapping] = {}
    for d in decisions:
        if d.get("week") is not None and d.get("yahoo_now") and d.get("recomendado"):
            last[int(d["week"])] = d
    out = []
    for week in sorted(last):
        d = last[week]
        played, rec = _starters(d["yahoo_now"]), _starters(d["recomendado"])
        roster = [n for n, s in d["yahoo_now"].items() if norm_slot(s) != "IR"]
        missing = sorted(n for n in set(played) | set(rec) if points(week, n, positions.get(n, "")) is None)
        val = {n: points(week, n, positions.get(n, "")) or 0.0 for n in roster}
        slots = sorted(played.values())
        pool = [Player(pid=n, name=n, position=positions.get(n, ""),
                       eligible=eligible_slots([positions.get(n, "")], slots), proj=val[n]) for n in roster]
        best = sum(p.proj for p in assign(pool, slots, lambda p, s: p.proj + 1e-9) if p is not None)
        only_rec = sorted(set(rec) - set(played), key=lambda n: -val.get(n, 0.0))
        only_played = sorted(set(played) - set(rec), key=lambda n: -val.get(n, 0.0))
        diffs = [f"{a} {val.get(a, 0.0):.1f} en vez de {b} {val.get(b, 0.0):.1f}"
                 for a, b in zip(only_rec, only_played)]
        out.append(LineupWeek(week, sum(val.get(n, 0.0) for n in played), sum(val.get(n, 0.0) for n in rec),
                              best, not only_rec, diffs, missing))
    return out


@dataclass
class MoveCheck:
    date: str
    kind: str                 # trade | alta | sugerencia
    got: list[str]
    gave: list[str]
    weeks: list[int]
    got_points: float
    gave_points: float
    note: str = ""

    @property
    def net(self) -> float:
        return self.got_points - self.gave_points


Roster = Optional[Mapping[int, set[str]]]   # week -> name keys of the players on your roster that week


def _sum(points: Points, names: Iterable[tuple[str, str]], weeks: Sequence[int], roster: Roster = None,
         on_roster: Optional[bool] = None) -> float:
    """Points of these players in these weeks. With `on_roster` True only the weeks each was on your roster,
    with False only the weeks he was not (weeks without roster data always count)."""
    total = 0.0
    for n, p in names:
        for w in weeks:
            if roster is not None and on_roster is not None and w in roster:
                if (name_key(n, p) in roster[w]) != on_roster:
                    continue
            total += points(w, n, p) or 0.0
    return total


def _dedupe(moves: Sequence[Mapping]) -> list[Mapping]:
    seen, out = set(), []
    for m in moves:
        key = (m.get("fecha"), m.get("team_id"), m.get("jugador"), m.get("accion"))
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def trade_checks(moves: Sequence[Mapping], me: str, points: Points, last_week: int,
                 roster: Roster = None) -> list[MoveCheck]:
    """Each trade of yours: points after it (until the last finished week) of what came, while it stayed on
    your roster, and of what went. A player the moves page puts on your side who never shows up on your
    roster is left out (that page does not say reliably who sent whom)."""
    trades = defaultdict(list)
    for m in _dedupe(moves):
        if str(m.get("accion")) == "trade":
            trades[str(m.get("fecha"))].append(m)
    out = []
    for date, rows in sorted(trades.items()):
        if me not in {str(r.get("team_id")) for r in rows}:
            continue
        first = int(float(rows[0].get("semana") or 0)) or 1
        weeks = list(range(first, last_week + 1))
        got = list(dict.fromkeys((r["jugador"], r.get("pos") or "") for r in rows if str(r.get("team_id")) == me))
        gave = list(dict.fromkeys((r["jugador"], r.get("pos") or "") for r in rows if str(r.get("team_id")) != me))
        note = ""
        if roster is not None:
            known = [w for w in weeks if w in roster]
            never = [n for n, p in got if known and not any(name_key(n, p) in roster[w] for w in known)]
            if never:
                got = [(n, p) for n, p in got if n not in never]
                note = "no cuento a " + ", ".join(never) + ": nunca apareció en tu roster"
        out.append(MoveCheck(date, "trade", [n for n, _ in got], [n for n, _ in gave], weeks,
                             _sum(points, got, weeks, roster, True), _sum(points, gave, weeks), note))
    return out


def add_checks(moves: Sequence[Mapping], me: str, points: Points, last_week: int,
               roster: Roster = None) -> list[MoveCheck]:
    """Each player you added, against the one dropped in the same move (Yahoo lists a move as the add
    followed by its drop), both over the weeks you kept the new one."""
    mine = [m for m in _dedupe(moves) if str(m.get("team_id")) == me and m.get("accion") in ("alta", "baja")]
    out = []
    for i, m in enumerate(mine):
        if m["accion"] != "alta":
            continue
        nxt = mine[i + 1] if i + 1 < len(mine) else None
        drop = ([(nxt["jugador"], nxt.get("pos") or "")]
                if nxt and nxt["accion"] == "baja" and nxt.get("fecha") == m.get("fecha") else [])
        start = int(float(m.get("semana") or 0)) or 1
        add = [(m["jugador"], m.get("pos") or "")]
        key = name_key(*add[0])
        # the weeks you kept him (weeks without roster data count), and the same weeks for the one you dropped
        weeks = [w for w in range(start, last_week + 1) if roster is None or w not in roster or key in roster[w]]
        if not weeks:
            continue
        bid = m.get("puja")
        note = f"puja ${int(float(bid))}" if bid not in (None, "", "nan") and str(bid) != "nan" else ""
        out.append(MoveCheck(str(m.get("fecha")), "alta", [m["jugador"]], [n for n, _ in drop], weeks,
                             _sum(points, add, weeks), _sum(points, drop, weeks, roster, False), note))
    out.sort(key=lambda c: c.date)
    return out


def suggestion_checks(decisions: Sequence[Mapping], points: Points, last_week: int, horizon: int = 3) -> list[MoveCheck]:
    """Claims and "win the week" plans the notebook suggested (logged with tipo=waivers / ganar)."""
    out, seen = [], set()
    for d in decisions:
        if d.get("tipo") not in ("waivers", "ganar"):
            continue
        week = int(d.get("week") or 0)
        for c in d.get("sugerencias") or []:
            key = (week, c.get("add"), c.get("drop"))
            if key in seen or not c.get("add"):
                continue
            seen.add(key)
            weeks = [w for w in range(week, week + (1 if d["tipo"] == "ganar" else horizon)) if w <= last_week]
            if not weeks:
                continue
            got, gave = [(c["add"], c.get("add_pos") or "")], ([(c["drop"], c.get("drop_pos") or "")] if c.get("drop") else [])
            out.append(MoveCheck(str(d.get("time", ""))[:10], "sugerencia", [c["add"]], [n for n, _ in gave], weeks,
                                 _sum(points, got, weeks), _sum(points, gave, weeks),
                                 f"{d['tipo']}, puja ${c.get('bid', 0)}"))
    return out
