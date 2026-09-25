import pytest

from fantasy_autopilot.yahoo.parse import (
    FormError,
    build_lineup_submission,
    is_login_page,
    parse_current_week,
    parse_matchups,
    parse_playernote,
    parse_players_page,
    parse_settings_page,
    parse_team_page,
    parse_teams_page,
)
from conftest import FIXTURES

URL = "https://football.fantasysports.yahoo.com/f1/123456/7?week=3&stat1=P&stat2=PW"


def read(name):
    return (FIXTURES / name).read_text()


def test_team_page_rows_slots_and_projections():
    page = parse_team_page(read("team_page.html"))
    assert page.has_selects
    by = {r.name: r for r in page.rows}
    assert len(page.rows) == 14  # 12 offense (1 empty bench row skipped) + K + DEF
    assert page.starting_slots == ["QB", "WR", "WR", "RB", "RB", "TE", "W/R/T", "K", "DEF"]
    assert page.bench_size == 5 and page.ir_size == 1
    assert by["Malik Nabers"].slot == "W/R/T"
    assert by["Malik Nabers"].eligible == ["WR", "W/R/T"]
    assert by["Bhayshul Tuten"].proj == pytest.approx(9.85)
    assert by["Bhayshul Tuten"].team == "JAX"
    assert by["Jaylen Warren"].status == "Q"
    assert by["Nico Collins"].status == "O"
    assert by["Alec Pierce"].slot == "IR" and by["Alec Pierce"].ir_eligible
    assert by["Detroit"].pid == "100008" and by["Detroit"].team == "DET"
    assert by["Puka Nacua"].game_text.startswith("Sun 8:20 pm")
    assert page.faab_balance == 83
    assert page.week == 3


def test_opponent_page_without_selects():
    page = parse_team_page(read("opponent_page.html"))
    assert not page.has_selects
    by = {r.name: r for r in page.rows}
    assert by["A.J. Brown"].slot == "WR" and by["A.J. Brown"].status == "IR"
    assert by["Patrick Mahomes"].slot == "BN" and by["Patrick Mahomes"].proj == pytest.approx(23.91)
    assert by["Patrick Mahomes"].editable is None


def test_lineup_submission_replays_the_form():
    target = {"31883": "BN", "40009": "WR", "41100": "W/R/T"}
    method, action, fields = build_lineup_submission(read("team_page.html"), URL, target)
    data = dict(fields)
    assert method == "post"
    assert action == "https://football.fantasysports.yahoo.com/f1/123456/7/editroster"
    assert data["crumb"] == "abc123" and data["week"] == "3"
    assert data["31883"] == "BN" and data["40009"] == "WR" and data["41100"] == "W/R/T"
    assert data["32671"] == "QB"          # untouched players keep their slot
    assert data["jsubmit"] == "Save Changes"


def test_lineup_submission_rejects_bad_slot():
    with pytest.raises(FormError):
        build_lineup_submission(read("team_page.html"), URL, {"32671": "WR"})


def test_players_page():
    rows = parse_players_page(read("players_page.html"))
    by = {r.name: r for r in rows}
    assert by["Michael Pittman Jr."].availability == "W (Sep 26)"
    assert by["Michael Pittman Jr."].status == "Q"
    assert by["Michael Pittman Jr."].add_url.endswith("addplayer?apid=30150")
    assert by["Braelon Allen"].availability == "FA"
    assert by["Jacoby Brissett"].positions == ["QB"]
    assert by["Jacoby Brissett"].points == pytest.approx(18.70)


def test_playernote_weekly_projections():
    note = parse_playernote(read("playernote.json"), "30150")
    assert note.name == "Michael Pittman Jr."
    assert note.owner.startswith("Waivers")
    assert note.weekly[3] == pytest.approx(7.92) and 3 in note.projected_weeks
    assert note.weekly[6] == 0.0            # bye
    assert 2 not in note.projected_weeks    # already played
    assert note.percent_owned == pytest.approx(41)


def test_matchups_and_week():
    html = read("league_home.html")
    assert parse_current_week(html) == 3
    games = parse_matchups(html)
    me, opp = games[0]
    assert (me.team_id, opp.team_id) == ("7", "4")
    assert me.projected == pytest.approx(103.41) and opp.projected == pytest.approx(94.28)


def test_teams_page_faab():
    teams = parse_teams_page(read("teams_page.html"), "123456")
    faab = {t.team_id: t.faab for t in teams}
    assert faab == {"1": 100, "4": 55, "7": 83}


def test_settings_page():
    s = parse_settings_page(read("settings_page.html"))
    assert s.starting_slots == ["QB", "WR", "WR", "RB", "RB", "TE", "W/R/T", "K", "DEF"]
    assert s.roster_positions.count("BN") == 6
    assert s.reception_points == 1.0
    assert s.uses_faab is True
    assert s.faab_budget == 100
    assert s.playoff_start_week == 15


def test_login_detection():
    assert is_login_page("https://login.yahoo.com/?done=x", "")
    assert not is_login_page(URL, read("team_page.html"))
