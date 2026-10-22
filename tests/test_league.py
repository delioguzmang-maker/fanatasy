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


def test_profiles_accept_pandas_records_with_empty_cells():
    """Cell 6c passes DataFrame records: a free-agent add has no bid, which pandas turns into NaN."""
    import math

    import pandas as pd

    from fantasy_autopilot.league import claim_threats, manager_profiles

    moves = pd.DataFrame([
        {"fecha": "2026-09-09", "semana": 1, "team_id": "7", "jugador": "Braelon Allen", "pid": None,
         "pos": "RB", "accion": "alta", "puja": None, "tipo": "add"},
        {"fecha": "2026-09-16", "semana": 2, "team_id": "4", "jugador": "Josh Downs", "pid": "1",
         "pos": None, "accion": "alta", "puja": 11, "tipo": "add"},
        {"fecha": "2026-09-16", "semana": 2, "team_id": None, "jugador": "Someone", "pid": "2",
         "pos": "WR", "accion": "alta", "puja": 3, "tipo": "add"},
    ]).to_dict("records")
    assert math.isnan(moves[0]["puja"])                     # exactly what crashed in Colab
    history = pd.DataFrame([
        {"semana": 1, "team_id": "4", "pid": None, "jugador": "Josh Downs", "puesto": "WR"},
        {"semana": None, "team_id": "4", "pid": "1", "jugador": "Josh Downs", "puesto": "WR"},
    ]).to_dict("records")
    prof = manager_profiles(moves, history, last_week_played=5)
    assert prof["7"].bids == [] and prof["7"].unused_names == ["Braelon Allen (sem 1)"]
    assert prof["4"].bids == [11] and prof["4"].positions["?"] == 1
    assert "nan" not in prof                                 # an add without team is skipped
    threats = claim_threats([("X", "WR", 5)], prof, {"4": float("nan"), "7": 80}, {}, "7", 3)
    assert all(isinstance(t.beat_with, int) for t in threats)
