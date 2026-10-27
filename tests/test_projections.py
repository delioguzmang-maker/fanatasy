"""Own projections, league scoring, matchup-page history and the add model."""

from pathlib import Path

from fantasy_autopilot import adds
from fantasy_autopilot.history import PastRow, matchup_html_to_text, parse_matchup_text
from fantasy_autopilot.learn import chronic_misses, scoring_bonus
from fantasy_autopilot.ownproj import OwnProjections, player_key
from fantasy_autopilot.sources.schedule import implied_totals
from fantasy_autopilot.sources.sleeper import league_points
from synth import SynthSleeper, league_adds

FIXTURES = Path(__file__).parent / "fixtures"


def test_matchup_page_keeps_both_teams_projection_and_points():
    text = matchup_html_to_text((FIXTURES / "matchup_page.html").read_text())
    rows = parse_matchup_text(text, week=2, use_headers=False)
    by = {r.name: r for r in rows}
    assert {r.week for r in rows} == {2}                      # the week menu does not move the week
    assert (by["Brock Purdy"].side, by["Brock Purdy"].proj, by["Brock Purdy"].pts) == ("left", 22.05, 14.02)
    assert (by["C.J. Stroud"].side, by["C.J. Stroud"].team, by["C.J. Stroud"].pts) == ("right", "HOU", 20.52)
    assert by["Jaylen Warren"].status == "Q" and by["Mike Evans"].status == "O"
    assert by["Cam Skattebo"].slot == "BN" and by["Cam Skattebo"].pts == 18.6
    assert not by["Brenton Strange"].usable                   # bye week: not a projection to learn from
    assert len(rows) == 9                                     # the empty bench slot is not a player


def test_league_points_converts_sleeper_to_this_league():
    qb = {"pts_half_ppr": 16.52, "pts_ppr": 16.52, "pass_td": 2, "pass_int": 0}
    assert league_points(qb, 0.5) == 16.52
    assert league_points(qb, 0.5, {"pass_td": 2.0, "pass_int": -1.0}) == 20.52
    assert league_points({"pts_ppr": 12.0}, 1.0) == 12.0 and league_points({}, 1.0) is None


def test_scoring_bonus_learns_six_point_passing_touchdowns():
    games = [({"pts_half_ppr": 14.0, "pass_td": 1, "pass_int": 2, "pass_yd": 288}, 14.0),
             ({"pts_half_ppr": 16.5, "pass_td": 2, "pass_int": 0, "pass_yd": 250}, 20.5),
             ({"pts_half_ppr": 4.6, "pass_td": 0, "pass_int": 1, "pass_yd": 150}, 3.6),
             ({"pts_half_ppr": 17.4, "pass_td": 1, "pass_int": 0, "pass_yd": 230}, 19.4)]
    assert scoring_bonus(games, 0.5) == {"pass_td": 2.0, "pass_int": -1.0}
    assert scoring_bonus([(s, s["pts_half_ppr"]) for s, _ in games], 0.5) == {}   # already the same scoring


def test_implied_totals_from_the_betting_line():
    text = ("season,game_type,week,home_team,away_team,spread_line,total_line\n"
            "2026,REG,8,CIN,TEN,10.5,47.5\n2026,REG,8,LA,LAC,-3,44\n2025,REG,8,KC,DEN,,\n")
    got = implied_totals(text, [2026])
    assert got[(2026, 8, "CIN")] == 29.0 and got[(2026, 8, "TEN")] == 18.5
    assert got[(2026, 8, "LAR")] == 20.5 and (2025, 8, "KC") not in got


def test_own_projections_learn_what_sleeper_misses_and_beat_it_walk_forward():
    sleeper = SynthSleeper()
    op = OwnProjections(sleeper, 2026, 0.5, {}, {}, past_seasons=1)
    league = [PastRow(w, "left", "QB", "Nico Collins", "HOU", "WR", "", None, None) for w in ()]
    ex = op.exam(3, league)
    assert len(ex.weeks) == 3
    assert ex.own < ex.sleeper * 0.9          # the repeated misses are learned
    proj = op.project(4)
    key = player_key("WR", "Nico Collins", "HOU")
    p = next(p for p in sleeper.players if p[1] == "Collins")
    truth, sp = sleeper.mean[p], max(1.0, sleeper.mean[p] - sleeper.miss[p])
    assert abs(proj[key] - truth) <= abs(sp - truth) + 1.0


def test_own_projection_exam_against_yahoo_on_league_rows():
    sleeper = SynthSleeper(seed=3)
    rows = []
    for w in (1, 2, 3):
        for first, last, pos, team in sleeper.players[:8]:
            p = (first, last, pos, team)
            real = next(r for r in sleeper.rows("stats", 2026, w)
                        if r["player"]["last_name"] == last)["stats"]["pts_half_ppr"]
            rows.append(PastRow(w, "left", pos, f"{first} {last}", team, pos, "", round(sleeper.mean[p] - sleeper.miss[p] + 0.5, 2),
                                real, "Final", "7"))
    ex = OwnProjections(sleeper, 2026, 0.5, {}, {}).exam(3, rows)
    assert ex.yahoo is not None and ex.own_league is not None
    assert ex.own_league < ex.yahoo and ex.beats_yahoo
    assert len(ex.rows) == len([r for r in rows if r.usable])


def test_chronic_misses_lists_players_who_keep_missing():
    rows = [PastRow(w, "left", "QB", "Daniel Jones", "IND", "QB", "", 18.0, 11.0, "Final", "7") for w in range(1, 7)]
    rows += [PastRow(w, "left", "RB", "Steady Guy", "PIT", "RB", "", 10.0, 10.0 + (-1) ** w, "Final", "7")
             for w in range(1, 7)]
    out = chronic_misses(rows, lambda r: r.team_id == "7")
    assert out == ["Daniel Jones: 6 de 6 semanas por debajo de Yahoo, promedio -7.0 pts"]


def test_add_model_sees_streamers_and_busy_managers_the_bye_rule_misses():
    roster, add_rows, byes = league_adds(seed=2)
    cases = adds.build_cases(roster, add_rows, byes, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1},
                             range(2, 10), label_through=9)
    ex = adds.exam(cases)
    assert ex.adds > 50
    assert ex.model_recall > ex.rule_recall + 0.2
    assert ex.auc_model > 0.65
    nxt = adds.build_cases(roster, add_rows, byes, {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1},
                           range(2, 11), label_through=9)
    probs = adds.predict(nxt, 10)
    assert set(probs) == {str(i) for i in range(1, 13)}
    assert all(0 <= v <= 1 for d in probs.values() for v in d.values())


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Session:
    def __init__(self, data):
        self.data, self.calls = data, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _Resp(self.data)


SLEEPER_ROWS = [{"player_id": "4034", "team": "SF", "opponent": "ATL",
                 "player": {"first_name": "Brock", "last_name": "Purdy", "position": "QB"},
                 "stats": {"pts_half_ppr": 14.02, "pass_td": 1, "gp": 1, "off_snp": 60, "big_extra": 1}}]


def test_weekly_stats_and_rows_keep_separate_files_and_repair_a_wrong_one(tmp_path):
    import json

    from fantasy_autopilot.sources.sleeper import Sleeper

    # the file left by the previous version: weekly_stats' name holding a list of rows
    (tmp_path / "stats_2026_7.json").write_text(json.dumps([{"player_id": "4034"}]))
    session = _Session(SLEEPER_ROWS)
    sl = Sleeper(cache_dir=tmp_path, session=session)
    stats = sl.weekly_stats(2026, 7)
    assert stats == {"4034": SLEEPER_ROWS[0]["stats"]} and session.calls == 1     # fetched again
    assert isinstance(json.loads((tmp_path / "stats_2026_7.json").read_text()), dict)
    rows = sl.rows("stats", 2026, 7, final=True)
    assert rows[0]["player"]["last_name"] == "Purdy" and "big_extra" not in rows[0]["stats"]
    assert (tmp_path / "rows_stats_2026_7.json").exists()
    assert sl.weekly_stats(2026, 7) == stats and session.calls == 2               # untouched by rows()
    fresh = Sleeper(cache_dir=tmp_path, session=session)
    assert fresh.rows("stats", 2026, 7, final=True) == rows and session.calls == 2  # read from disk
    (tmp_path / "rows_stats_2026_8.json").write_text(json.dumps({"4034": {}}))    # wrong shape: ignored
    assert fresh.rows("stats", 2026, 8, final=True)[0]["player_id"] == "4034" and session.calls == 3


def test_rows_from_cell_7d_history():
    from fantasy_autopilot.history import from_records

    rows = from_records([
        {"semana": 7, "team_id": "13", "pid": "1", "jugador": "Daniel Jones", "pos": "QB", "puesto": "BN",
         "proy": 14.27, "real": 3.62, "nfl": "Ind", "estado": float("nan")},
        {"semana": 7, "team_id": "4", "pid": "2", "jugador": "Saquon Barkley", "pos": "RB", "puesto": "W/R/T",
         "proy": 13.85, "real": float("nan"), "nfl": "PHI", "estado": "Q"},
    ])
    assert (rows[0].week, rows[0].team, rows[0].status, rows[0].team_id, rows[0].usable) == (7, "IND", "", "13", True)
    assert rows[1].slot == "W/R/T" and rows[1].pts is None and not rows[1].usable


def test_questionable_split_accepts_sleeper_rows_as_a_list():
    from fantasy_autopilot import calibration

    as_dict = {7: {"4034": {"off_snp": 50, "rush_att": 10, "rec_tgt": 2}}}
    as_list = {7: [{"player_id": "4034", "stats": {"off_snp": 50, "rush_att": 10, "rec_tgt": 2}}]}
    assert calibration.questionable_split([], {}, as_list) == calibration.questionable_split([], {}, as_dict)


def test_build_info_names_the_version():
    import fantasy_autopilot

    assert fantasy_autopilot.build_info().startswith(fantasy_autopilot.__version__.split(".")[0])
