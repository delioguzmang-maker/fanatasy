"""Manager profiles and threats to your waiver claims."""

from fantasy_autopilot.league import claim_threats, manager_profiles


def mv(team, week, name, pos, action, bid=None, fecha=None):
    return {"fecha": fecha or f"2026-09-{10 + 7 * week:02d}", "semana": week, "team_id": team, "jugador": name,
            "pid": "", "pos": pos, "accion": action, "puja": bid}


def test_unused_adds_and_high_bidder_threat():
    moves = [mv("7", 2, "A", "WR", "alta", 12), mv("7", 3, "B", "RB", "alta", 10),
             mv("7", 4, "C", "WR", "alta", 12), mv("7", 5, "D", "RB", "alta", 0),
             mv("1", 3, "E", "RB", "alta", 2)]
    history = [{"semana": 3, "team_id": "7", "pid": "", "jugador": "B", "puesto": "RB"},
               {"semana": 4, "team_id": "1", "pid": "", "jugador": "E", "puesto": "RB"}]
    prof = manager_profiles(moves, history, last_week_played=6)
    josh = prof["7"]
    assert josh.adds == 4 and josh.adds_checked == 4 and josh.unused == 3
    assert josh.high_bid == 12 and josh.max_bid == 12
    threats = claim_threats([("Target", "WR", 7)], prof, {"7": 60, "1": 90}, {"1": ["RB"]}, me="13", current_week=6)
    assert [t.team_id for t in threats] == ["7"]
    assert threats[0].beat_with == 13 and "máxima $12" in threats[0].reason


def test_no_money_no_threat():
    moves = [mv("7", w, f"P{w}", "WR", "alta", 12) for w in range(1, 6)]
    prof = manager_profiles(moves, [], last_week_played=6)
    assert claim_threats([("T", "WR", 7)], prof, {"7": 3}, {}, me="13", current_week=6) == []
