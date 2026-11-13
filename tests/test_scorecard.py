"""Were the suggestions good? Checked with the points really scored."""

import pytest

from fantasy_autopilot import scorecard as score
from fantasy_autopilot.yahoo.cookies import cookie_problems, parse_cookies
from fantasy_autopilot.yahoo.web import YahooWeb

REAL = {(7, "brock purdy"): 14.0, (7, "jaylen warren"): 15.2, (7, "bucky irving"): 23.5, (7, "puka nacua"): 17.8,
        (7, "nico collins"): 1.6, (7, "sam laporta"): 2.3, (7, "denzel boston"): 4.2, (7, "malik washington"): 5.0,
        (7, "cam skattebo"): 18.6, (7, "spencer shrader"): 5.3, (7, "DEF:CLE"): 5.0,
        (5, "bucky irving"): 30.5, (6, "bucky irving"): 5.9, (5, "denzel boston"): 12.1, (6, "denzel boston"): 2.0,
        (5, "malik nabers"): 23.6, (6, "malik nabers"): 22.5, (7, "malik nabers"): 14.6,
        (5, "bhayshul tuten"): 10.6, (6, "bhayshul tuten"): 22.7, (7, "bhayshul tuten"): 0.0}
POS = {"Brock Purdy": "QB", "Jaylen Warren": "RB", "Bucky Irving": "RB", "Puka Nacua": "WR", "Nico Collins": "WR",
       "Sam LaPorta": "TE", "Denzel Boston": "WR", "Malik Washington": "WR", "Cam Skattebo": "RB",
       "Spencer Shrader": "K", "Browns": "DEF"}


def points(week, name, pos=""):
    return REAL.get((week, score.name_key(name, pos)))


def test_lineup_week_compares_played_recommended_and_best():
    played = {"Brock Purdy": "QB", "Jaylen Warren": "RB", "Bucky Irving": "RB", "Puka Nacua": "WR",
              "Nico Collins": "WR", "Sam LaPorta": "TE", "Denzel Boston": "W/R/T", "Spencer Shrader": "K",
              "Browns": "DEF", "Malik Washington": "BN", "Cam Skattebo": "BN"}
    rec = dict(played, **{"Denzel Boston": "BN", "Malik Washington": "W/R/T"})
    decisions = [{"week": 7, "yahoo_now": played, "recomendado": played},           # an earlier run
                 {"week": 7, "tipo": "waivers", "sugerencias": []},                  # not a lineup
                 {"week": 7, "yahoo_now": played, "recomendado": rec}]                # the last one counts
    [w] = score.lineup_weeks(decisions, points, POS)
    assert w.week == 7 and not w.followed
    assert w.played == pytest.approx(88.9) and w.recommended == pytest.approx(89.7)
    assert w.best == pytest.approx(88.9 - 4.2 - 1.6 + 18.6 + 5.0)       # Skattebo in, Washington at WR
    assert w.differences == ["Malik Washington 5.0 en vez de Denzel Boston 4.2"]


def test_trade_and_add_checks_use_points_after_the_move():
    moves = [{"fecha": "2026-10-06", "semana": "5", "team_id": "13", "jugador": "Bucky Irving", "pos": "RB", "accion": "trade"},
             {"fecha": "2026-10-06", "semana": "5", "team_id": "13", "jugador": "Denzel Boston", "pos": "WR", "accion": "trade"},
             {"fecha": "2026-10-06", "semana": "5", "team_id": "7", "jugador": "Malik Nabers", "pos": "WR", "accion": "trade"},
             {"fecha": "2026-10-06", "semana": "5", "team_id": "7", "jugador": "Bhayshul Tuten", "pos": "RB", "accion": "trade"},
             {"fecha": "2026-10-22", "semana": "7", "team_id": "13", "jugador": "Malik Washington", "pos": "WR",
              "accion": "alta", "puja": "3.0"},
             {"fecha": "2026-10-22", "semana": "7", "team_id": "13", "jugador": "Cam Skattebo", "pos": "RB", "accion": "baja"},
             {"fecha": "2026-10-22", "semana": "7", "team_id": "4", "jugador": "Someone Else", "pos": "WR", "accion": "alta"}]
    [t] = score.trade_checks(moves, "13", points, last_week=7)
    assert sorted(t.got) == ["Bucky Irving", "Denzel Boston"] and t.weeks == [5, 6, 7]
    assert t.got_points == pytest.approx(78.2) and t.gave_points == pytest.approx(94.0) and t.net < 0
    [a] = score.add_checks(moves, "13", points, last_week=7)
    assert a.got == ["Malik Washington"] and a.gave == ["Cam Skattebo"] and a.net == pytest.approx(5.0 - 18.6)
    assert a.note == "puja $3"


def test_logged_suggestions_are_checked_later():
    decisions = [{"week": 7, "tipo": "ganar", "time": "2026-10-27T12:00",
                  "sugerencias": [{"add": "Cam Skattebo", "add_pos": "RB", "drop": "Nico Collins", "drop_pos": "WR", "bid": 0}]},
                 {"week": 7, "tipo": "ganar", "time": "2026-10-27T13:00",          # same suggestion again: once
                  "sugerencias": [{"add": "Cam Skattebo", "add_pos": "RB", "drop": "Nico Collins", "drop_pos": "WR", "bid": 0}]},
                 {"week": 9, "tipo": "waivers", "sugerencias": [{"add": "Future Guy", "bid": 3}]}]   # not played yet
    checks = score.suggestion_checks(decisions, points, last_week=7)
    assert len(checks) == 1 and checks[0].net == pytest.approx(18.6 - 1.6)


def test_defenses_match_by_team_name():
    assert score.name_key("Lions") == "DEF:DET" == score.name_key("Detroit", "DEF")
    assert score.name_key("Josh Downs") == "josh downs"


def test_a_cut_cookie_is_explained_instead_of_crashing():
    cut = parse_cookies("A1=abc; A3=d" + "x" * 500 + "…; T=z")
    assert cookie_problems(cut) and "A3" in cookie_problems(cut)[0]
    assert cookie_problems(parse_cookies("A1=abc; T=z; Y=v")) == []
    with pytest.raises(ValueError, match="cortada"):
        YahooWeb("1", "2", "A1=abc; A3=d" + "x" * 500 + "…")


def test_adds_pair_with_their_own_drop_and_compare_the_same_weeks():
    real = {(4, "spencer shrader"): 9.0, (5, "spencer shrader"): 8.0, (4, "cameron dicker"): 6.0, (5, "cameron dicker"): 7.0,
            (4, "tyler loop"): 9.4, (4, "tyjae spears"): 7.7, (5, "tyjae spears"): 20.0,
            (4, "daniel jones"): 7.9, (5, "daniel jones"): 11.1}
    pts = lambda w, n, p="": real.get((w, score.name_key(n, p)))   # noqa: E731
    moves = [{"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Spencer Shrader", "pos": "K", "accion": "alta"},
             {"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Cameron Dicker", "pos": "K", "accion": "baja"},
             {"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Daniel Jones", "pos": "QB", "accion": "alta",
              "puja": "0.0"},
             {"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Tyler Loop", "pos": "K", "accion": "alta"},
             {"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Tyler Loop", "pos": "K", "accion": "alta"},
             {"fecha": "2026-09-30", "semana": "4", "team_id": "13", "jugador": "Tyjae Spears", "pos": "RB", "accion": "baja"}]
    roster = {4: {"spencer shrader", "daniel jones", "tyler loop"}, 5: {"spencer shrader", "daniel jones"}}
    checks = {c.got[0]: c for c in score.add_checks(moves, "13", pts, last_week=5, roster=roster)}
    assert checks["Spencer Shrader"].gave == ["Cameron Dicker"] and checks["Spencer Shrader"].net == pytest.approx(4.0)
    assert checks["Daniel Jones"].gave == [] and checks["Daniel Jones"].note == "puja $0"
    loop = checks["Tyler Loop"]                       # the duplicate row is read once; dropped in week 5
    assert loop.gave == ["Tyjae Spears"] and loop.weeks == [4] and loop.net == pytest.approx(9.4 - 7.7)


def test_a_traded_player_who_never_reached_your_roster_is_not_counted():
    moves = [{"fecha": "2026-10-06", "semana": "5", "team_id": "13", "jugador": "Bucky Irving", "pos": "RB", "accion": "trade"},
             {"fecha": "2026-10-06", "semana": "5", "team_id": "13", "jugador": "Braelon Allen", "pos": "RB", "accion": "trade"},
             {"fecha": "2026-10-06", "semana": "5", "team_id": "7", "jugador": "Malik Nabers", "pos": "WR", "accion": "trade"}]
    roster = {5: {"bucky irving"}, 6: {"bucky irving"}, 7: {"bucky irving"}}
    [t] = score.trade_checks(moves, "13", points, last_week=7, roster=roster)
    assert t.got == ["Bucky Irving"] and "Braelon Allen" in t.note
    assert t.got_points == pytest.approx(30.5 + 5.9 + 23.5) and t.gave_points == pytest.approx(23.6 + 22.5 + 14.6)
