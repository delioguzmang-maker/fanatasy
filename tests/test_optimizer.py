import pytest

from fantasy_autopilot.optimizer import (
    OptimizerSettings,
    current_plan,
    lineup_expected_points,
    optimize_lineup,
    plan_moves,
)
from conftest import SLOTS, SNF, mk


def by_name(plan):
    return {a.player.name: a.slot for a in plan.assignments if a.player}


def test_week3_matches_hand_analysis(week3_roster):
    plan = optimize_lineup(week3_roster, SLOTS)
    lineup = by_name(plan)
    assert lineup["Bhayshul Tuten"] == "W/R/T"
    assert lineup["Malik Nabers"] == "WR"
    assert lineup["Jaylen Warren"] == "RB"
    assert "Rashod Bateman" not in lineup
    assert "Puka Nacua" not in lineup      # SNF, 30%: no later backup exists
    assert "Nico Collins" not in lineup
    assert plan.naive == pytest.approx(104.66)


def test_week3_moves_are_the_three_clicks(week3_roster):
    plan = optimize_lineup(week3_roster, SLOTS)
    moves = {(m.player.name, m.from_slot, m.to_slot) for m in plan_moves(week3_roster, plan)}
    assert moves == {
        ("Rashod Bateman", "WR", "BN"),
        ("Malik Nabers", "W/R/T", "WR"),
        ("Bhayshul Tuten", "BN", "W/R/T"),
    }
    before = current_plan(week3_roster, SLOTS)
    assert plan.naive - before.naive == pytest.approx(1.25)


def test_warren_inactive_gives_99_9(week3_roster):
    warren = next(p for p in week3_roster if p.name == "Jaylen Warren")
    warren.status, warren.p_play = "O", 0.0
    plan = optimize_lineup(week3_roster, SLOTS)
    lineup = by_name(plan)
    assert "Jaylen Warren" not in lineup
    assert lineup["Bhayshul Tuten"] == "RB"
    assert lineup["Rashod Bateman"] in ("W/R/T", "WR")
    assert plan.naive == pytest.approx(99.90)


def test_healthy_nacua_starts(week3_roster):
    nacua = next(p for p in week3_roster if p.name == "Puka Nacua")
    nacua.p_play = 0.95
    lineup = by_name(optimize_lineup(week3_roster, SLOTS))
    assert "Puka Nacua" in lineup
    assert "Xavier Worthy" not in lineup


def test_same_window_backup_makes_risky_start_worth_it(week3_roster):
    nacua = next(p for p in week3_roster if p.name == "Puka Nacua")
    worthy = next(p for p in week3_roster if p.name == "Xavier Worthy")
    warren = next(p for p in week3_roster if p.name == "Jaylen Warren")
    warren.status, warren.p_play = "", None  # isolate Nacua (Worthy would also hedge Warren)
    nacua.p_play = 0.6
    # Worthy plays at 4:25: if Nacua is inactive at 6:50 pm nobody can replace him.
    assert "Puka Nacua" not in by_name(optimize_lineup(week3_roster, SLOTS))
    # Same player, but Worthy now plays in the same SNF game: he becomes the hedge.
    worthy.kickoff = SNF
    plan = optimize_lineup(week3_roster, SLOTS)
    assert "Puka Nacua" in by_name(plan)
    assert plan.hedges[nacua.pid] == worthy.pid


def test_no_autopilot_means_no_hedge(week3_roster):
    nacua = next(p for p in week3_roster if p.name == "Puka Nacua")
    worthy = next(p for p in week3_roster if p.name == "Xavier Worthy")
    nacua.p_play, worthy.kickoff = 0.6, SNF
    settings = OptimizerSettings(assume_autoswap=False)
    assert "Puka Nacua" not in by_name(optimize_lineup(week3_roster, SLOTS, settings))


def test_two_step_replacement_through_flex(week3_roster):
    """Warren out at 11:30 -> Tuten slides FLEX->RB and Bateman (4:25) takes FLEX."""
    plan = optimize_lineup(week3_roster, SLOTS)
    starters = [(a.slot, a.player) for a in plan.assignments if a.player]
    bench = plan.bench
    settings = OptimizerSettings(hedge_reliability=1.0, uncertain_penalty=0)
    ev, hedges = lineup_expected_points(starters, bench, settings)
    warren = next(p for _, p in starters if p.name == "Jaylen Warren")
    bateman = next(p for p in bench if p.name == "Rashod Bateman")
    assert hedges[warren.pid] == bateman.pid
    expected = plan.naive - 13.36 + 0.75 * 13.36 + 0.25 * 8.60
    assert ev == pytest.approx(expected)


def test_locked_players_stay_put(week3_roster):
    purdy = next(p for p in week3_roster if p.name == "Brock Purdy")
    jones = next(p for p in week3_roster if p.name == "Daniel Jones")
    purdy.locked, purdy.proj = True, 5.0     # game started badly; still cannot be moved
    jones.proj = 30.0
    lineup = by_name(optimize_lineup(week3_roster, SLOTS))
    assert lineup["Brock Purdy"] == "QB"
    assert "Daniel Jones" not in lineup


def test_locked_bench_player_cannot_enter(week3_roster):
    tuten = next(p for p in week3_roster if p.name == "Bhayshul Tuten")
    tuten.locked = True
    lineup = by_name(optimize_lineup(week3_roster, SLOTS))
    assert "Bhayshul Tuten" not in lineup


def test_pins(week3_roster):
    lineup = by_name(optimize_lineup(week3_roster, SLOTS, pins_start=["11"], pins_bench=["10"]))
    assert "Puka Nacua" in lineup
    assert "Bhayshul Tuten" not in lineup


def test_empty_slot_filled_even_by_questionable_player():
    players = [
        mk(1, "QB One", "QB", 20, "QB"),
        mk(2, "Only K", "K", 7, "BN", status="Q", p=0.5),
    ]
    plan = optimize_lineup(players, ["QB", "K"])
    assert by_name(plan) == {"QB One": "QB", "Only K": "K"}
