"""What every manager in the league does: adds, drops, bids, and adds he never uses.

An add the manager never starts and soon drops is either a block (taking a player
someone else needed) or a speculation. From the outside both look the same, and
both matter for the same reason: that manager bids on players he does not need,
so he can take the player *you* want. The rate is pulled toward the league
average when a manager has few adds.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Optional, Sequence

from .nfl import norm_name
from .yahoo.parse import Transaction

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                      "Nov", "Dec"], 1)}
STREAMED = ("K", "DEF")


def week_of(d: date, season_start: date) -> int:
    """Fantasy week of a date; `season_start` is the Tuesday before week 1."""
    return 1 + (d - season_start).days // 7


def moves_table(txs: Iterable[Transaction], season: int, season_start: date) -> list[dict]:
    """One row per player and transaction."""
    rows = []
    for t in txs:
        year = season + (1 if MONTHS[t.month] < 3 else 0)
        d = date(year, MONTHS[t.month], t.day)
        for i, p in enumerate(t.players):
            tid = t.team_ids[0] if t.team_ids else ""
            if t.kind == "trade" and len(t.team_ids) >= 2:
                tid = ""  # who sent whom is not reliable from the row text
            rows.append({"fecha": d.isoformat(), "semana": week_of(d, season_start), "team_id": tid,
                         "jugador": p.name, "pid": p.pid, "pos": p.pos, "nfl": p.nfl, "accion": p.action,
                         "puja": p.bid, "tipo": t.kind})
    return rows


@dataclass
class ManagerProfile:
    team_id: str
    adds: int = 0
    adds_checked: int = 0          # adds (not K/DEF) old enough to know if he used them
    unused: int = 0                # never started, then dropped or still on the bench
    unused_names: list[str] = field(default_factory=list)
    bids: list[int] = field(default_factory=list)
    positions: Counter = field(default_factory=Counter)
    last_add_week: int = 0
    unused_rate: float = 0.0       # shrunk toward the league rate

    @property
    def typical_bid(self) -> float:
        paid = sorted(b for b in self.bids if b > 0)
        return paid[len(paid) // 2] if paid else 0.0

    @property
    def high_bid(self) -> float:
        paid = sorted(b for b in self.bids if b > 0)
        return paid[min(len(paid) - 1, int(0.75 * len(paid)))] if paid else 0.0

    @property
    def max_bid(self) -> int:
        return max((b for b in self.bids if b), default=0)


def manager_profiles(
    moves: Sequence[Mapping],
    history: Sequence[Mapping],
    last_week_played: int,
    prior_strength: float = 3.0,
) -> dict[str, ManagerProfile]:
    """Profiles from the season's moves and the weekly roster history (cell 7d).

    history rows: semana, team_id, pid, jugador, puesto (BN / IR / a starting slot)."""
    started = defaultdict(set)       # (team, key) -> weeks started
    for h in history:
        if str(h.get("puesto", "BN")) in ("BN", "IR"):
            continue
        for key in (str(h.get("pid", "")), norm_name(str(h.get("jugador", "")))):
            if key:
                started[(str(h["team_id"]), key)].add(int(h["semana"]))
    drops = defaultdict(list)
    for m in moves:
        if m["accion"] == "baja":
            drops[(str(m["team_id"]), norm_name(m["jugador"]))].append(m["fecha"])
    prof: dict[str, ManagerProfile] = {}
    for m in moves:
        if m["accion"] != "alta" or not m["team_id"]:
            continue
        tid = str(m["team_id"])
        p = prof.setdefault(tid, ManagerProfile(tid))
        p.adds += 1
        p.positions[m["pos"] or "?"] += 1
        p.last_add_week = max(p.last_add_week, int(m["semana"]))
        if m.get("puja") is not None and m["puja"] == m["puja"]:  # not NaN
            p.bids.append(int(m["puja"]))
        if m["pos"] in STREAMED or int(m["semana"]) + 1 > last_week_played:
            continue  # kickers/defenses are streamed; too recent to judge
        w = int(m["semana"])
        keys = [str(m.get("pid", "")), norm_name(m["jugador"])]
        weeks = set().union(*(started.get((tid, k), set()) for k in keys if k))
        used = any(w <= x <= w + 2 for x in weeks)
        p.adds_checked += 1
        if not used:
            p.unused += 1
            p.unused_names.append(f"{m['jugador']} (sem {w}{', $' + str(int(m['puja'])) if m.get('puja') else ''})")
    total_checked = sum(p.adds_checked for p in prof.values())
    league_rate = sum(p.unused for p in prof.values()) / total_checked if total_checked else 0.3
    for p in prof.values():
        p.unused_rate = (p.unused + prior_strength * league_rate) / (p.adds_checked + prior_strength)
    return prof


@dataclass
class Threat:
    target: str
    pos: str
    my_bid: int
    team_id: str
    reason: str
    their_bid: int
    beat_with: int


def claim_threats(
    claims: Sequence[tuple[str, str, int]],          # (player, position, my bid)
    profiles: Mapping[str, ManagerProfile],
    faab: Mapping[str, Optional[int]],
    holes: Mapping[str, Iterable[str]],               # team -> positions he needs this week
    me: str,
    current_week: int,
) -> list[Threat]:
    """Managers likely to outbid each of your claims, and the bid that beats them.

    A threat is an active manager who either needs that position this week or
    usually pays at least your bid (high bidders take players they do not need)."""
    checked = sum(p.adds_checked for p in profiles.values())
    league_rate = sum(p.unused for p in profiles.values()) / checked if checked else 0.0
    out = []
    for name, pos, bid in claims:
        for tid, p in profiles.items():
            if tid == me or current_week - p.last_add_week > 3:
                continue
            money = faab.get(tid)
            need = pos in set(holes.get(tid, ()))
            guess = int(round(p.high_bid if pos not in STREAMED else min(p.high_bid, 2)))
            if money is not None:
                guess = min(guess, int(money))
            bidder = p.adds >= 4 and guess >= max(bid, 1)
            if not (bidder or (need and guess >= bid)):
                continue
            beat = guess + 1
            beat += 1 - beat % 2           # odd: fewer ties
            if money is not None and beat > int(money) + 1:
                beat = int(money) + 1
            why = []
            if need:
                why.append(f"necesita {pos} esta semana")
            if p.bids:
                why.append(f"puja típica ${p.typical_bid:.0f}, alta ${p.high_bid:.0f}, máxima ${p.max_bid}")
            if p.adds_checked >= 3:
                why.append(f"{p.unused} de {p.adds_checked} altas no las usó como titular "
                           f"(la liga: {league_rate:.0%})")
            out.append(Threat(name, pos, bid, tid, "; ".join(why), guess, beat))
    out.sort(key=lambda t: -t.their_bid)
    return out
