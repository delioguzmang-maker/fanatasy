from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fantasy_autopilot.models import Player  # noqa: E402
from fantasy_autopilot.nfl import eligible_slots  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SLOTS = ["QB", "WR", "WR", "RB", "RB", "TE", "W/R/T", "K", "DEF"]

# Week 3 of 2026, times in UTC (EDT = UTC-4).
SUN_1PM = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
SUN_405 = datetime(2026, 9, 27, 20, 5, tzinfo=timezone.utc)
SUN_425 = datetime(2026, 9, 27, 20, 25, tzinfo=timezone.utc)
SNF = datetime(2026, 9, 28, 0, 20, tzinfo=timezone.utc)


def mk(pid, name, pos, proj, slot="BN", team="", kickoff=SUN_1PM, status="", p=None, **kw):
    player = Player(
        pid=str(pid),
        name=name,
        position=pos,
        team=team,
        eligible=eligible_slots([pos], SLOTS),
        slot=slot,
        proj=proj,
        status=status,
        kickoff=kickoff,
        **kw,
    )
    player.p_play = p
    return player


@pytest.fixture
def week3_roster():
    """The Week 3 roster analysed by hand, with the projections from the Yahoo matchup page."""
    return [
        mk(1, "Brock Purdy", "QB", 23.66, "QB", "SF", SUN_425),
        mk(2, "Cam Skattebo", "RB", 13.33, "RB", "NYG", SUN_1PM),
        mk(3, "Jaylen Warren", "RB", 13.36, "RB", "PIT", SUN_1PM, "Q", p=0.75),
        mk(4, "Xavier Worthy", "WR", 9.18, "WR", "KC", SUN_425),
        mk(5, "Rashod Bateman", "WR", 8.60, "WR", "BAL", SUN_425),
        mk(6, "Malik Nabers", "WR", 10.49, "W/R/T", "NYG", SUN_1PM),
        mk(7, "Sam LaPorta", "TE", 9.36, "TE", "DET", SUN_1PM),
        mk(8, "Cameron Dicker", "K", 8.33, "K", "LAC", SUN_405),
        mk(9, "Detroit", "DEF", 7.10, "DEF", "DET", SUN_1PM),
        mk(10, "Bhayshul Tuten", "RB", 9.85, "BN", "JAX", SUN_1PM),
        mk(11, "Puka Nacua", "WR", 12.26, "BN", "LAR", SNF, "Q", p=0.30),
        mk(12, "Nico Collins", "WR", 12.96, "BN", "HOU", SUN_1PM, "Q", p=0.15),
        mk(13, "Kenneth Gainwell", "RB", 6.72, "BN", "PIT", SUN_1PM),
        mk(14, "Daniel Jones", "QB", 17.83, "BN", "IND", SUN_1PM),
        mk(15, "Alec Pierce", "WR", 0.0, "BN", "IND", SUN_1PM, "IR", p=0.0),
    ]
