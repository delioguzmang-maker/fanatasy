"""Plain data objects shared by every module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .nfl import NON_STARTING, norm_slot


@dataclass
class Player:
    pid: str                      # Yahoo player id (digits) or a stable synthetic key
    name: str
    position: str = ""            # primary position: QB/RB/WR/TE/K/DEF
    team: str = ""                # canonical NFL abbreviation
    eligible: list[str] = field(default_factory=list)  # starting slots the player can fill
    slot: str = "BN"              # current Yahoo slot (QB, RB, W/R/T, BN, IR, ...)
    proj: float = 0.0             # projected fantasy points for the target week (league scoring)
    status: str = ""              # canonical injury status, "" when healthy
    locked: bool = False          # game started (or Yahoo disabled the slot) -> cannot move
    ir_eligible: bool = False     # Yahoo offers the IR slot for this player
    kickoff: Optional[datetime] = None  # UTC kickoff of the player's game this week
    on_bye: bool = False
    game_text: str = ""           # e.g. "Sun 1:00 pm vs Hou" as shown by Yahoo
    practice: str = ""            # last practice participation (Full / Limited / DNP)
    p_play: Optional[float] = None  # probability the player is active (status model)
    weekly: dict[int, float] = field(default_factory=dict)  # week -> projected points
    owner: str = ""               # free agents: "FA" or "W (Wed)"; others: team id
    percent_owned: Optional[float] = None
    sleeper_id: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.slot = norm_slot(self.slot) or "BN"
        self.eligible = [norm_slot(s) for s in self.eligible if norm_slot(s) not in NON_STARTING]

    @property
    def is_starter(self) -> bool:
        return self.slot not in NON_STARTING

    @property
    def prob(self) -> float:
        return 1.0 if self.p_play is None else self.p_play

    @property
    def expected(self) -> float:
        return self.proj * self.prob

    def can_fill(self, slot: str) -> bool:
        return norm_slot(slot) in self.eligible

    def label(self) -> str:
        tag = f" ({self.status})" if self.status else ""
        return f"{self.name}{tag}"


@dataclass
class Roster:
    league_id: str
    team_id: str
    week: int
    slots: list[str]              # starting slots in Yahoo order (no BN / IR)
    players: list[Player]
    bench_size: int = 0
    ir_size: int = 0
    team_name: str = ""
    faab_balance: Optional[int] = None
    fetched_at: Optional[datetime] = None
    source: str = "yahoo-web"

    def by_pid(self) -> dict[str, Player]:
        return {p.pid: p for p in self.players}

    def find(self, name_or_pid: str) -> Optional[Player]:
        from .nfl import norm_name

        key = norm_name(name_or_pid)
        for p in self.players:
            if p.pid == name_or_pid or norm_name(p.name) == key:
                return p
        return None

    @property
    def starters(self) -> list[Player]:
        return [p for p in self.players if p.is_starter]

    @property
    def bench(self) -> list[Player]:
        return [p for p in self.players if p.slot == "BN"]

    @property
    def injured_reserve(self) -> list[Player]:
        return [p for p in self.players if p.slot == "IR"]

    def open_bench_spots(self) -> int:
        return max(0, self.bench_size - len(self.bench)) if self.bench_size else 0

    def open_ir_spots(self) -> int:
        return max(0, self.ir_size - len(self.injured_reserve)) if self.ir_size else 0


@dataclass
class Assignment:
    slot: str
    player: Optional[Player]


@dataclass
class LineupPlan:
    assignments: list[Assignment]
    bench: list[Player]
    ev: float                     # expected points, counting automatic inactive swaps
    naive: float                  # plain sum of starters' projections (players ruled out count 0)
    hedges: dict[str, str] = field(default_factory=dict)  # starter pid -> planned backup pid
    notes: list[str] = field(default_factory=list)

    def target_slots(self) -> dict[str, str]:
        out = {a.player.pid: a.slot for a in self.assignments if a.player}
        for p in self.bench:
            out.setdefault(p.pid, "BN")
        return out

    def starters(self) -> list[Player]:
        return [a.player for a in self.assignments if a.player]


@dataclass
class Move:
    player: Player
    from_slot: str
    to_slot: str

    def describe(self) -> str:
        return f"{self.player.name}: {self.from_slot} -> {self.to_slot}"


@dataclass
class Game:
    home: str
    away: str
    kickoff: datetime             # UTC
    state: str = "pre"            # pre / in / post
    game_id: str = ""

    def teams(self) -> tuple[str, str]:
        return self.home, self.away


@dataclass
class WriteResult:
    ok: bool
    action: str
    detail: str = ""
    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    dry_run: bool = False
    evidence: list[str] = field(default_factory=list)  # screenshot / html dump paths
