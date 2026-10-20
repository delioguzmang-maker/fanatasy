"""Fixes from the October 20 audit: role alerts, resend, Q calibration, win probability, bid cap."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fantasy_autopilot import calibration, role
from fantasy_autopilot.engine import State
from fantasy_autopilot.matchup import win_prob_swap
from fantasy_autopilot.models import Assignment, Player
from fantasy_autopilot.waivers import WaiverSettings, suggest_bid

TZ = ZoneInfo("America/New_York")
SUNDAY = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)  # 11:00 am ET


def pl(pid, name, slot, proj, pos="RB", eligible=("RB", "W/R/T"), **kw):
    return Player(pid=pid, name=name, position=pos, slot=slot, proj=proj, eligible=list(eligible), **kw)


# ------------------------------------------------------------- role alerts
def test_fresh_role_news_triggers_swap_and_old_news_is_ignored():
    text = ("Oct 18, 9:53 AM: Cam Skattebo has been benched for fumbles and Devin Singletary will lead the "
            "backfield. Sep 20: Skattebo fumble in practice drew attention.")
    sig = role.role_signal(text, SUNDAY, TZ, max_age_hours=120)
    assert len(sig.fresh_bad) == 1 and "benched" in sig.fresh_bad[0].text
    assert sig.old_ignored == 1

    starter = pl("1", "Skattebo", "RB", 13.2)
    bench = pl("2", "Worthy", "BN", 9.73, pos="WR", eligible=("WR", "W/R/T"))
    other = pl("3", "Tuten", "BN", 7.0)
    notes = {"1": text, "2": "", "3": ""}
    alerts, good, fails = role.check_roster([starter, bench, other], notes.get, SUNDAY, TZ)
    assert not fails and len(alerts) == 1
    a = alerts[0]
    assert a.factor == 0.5 and abs(a.effective - 6.6) < 1e-9
    assert a.swap_name == "Tuten" and "CAMBIA" in a.describe()  # Worthy cannot play RB


def test_injury_words_are_not_a_second_discount():
    text = "Oct 17: Player X is questionable and listed as a game-time decision with a hamstring injury."
    sig = role.role_signal(text, SUNDAY, TZ)
    assert not sig.bad  # the injury status model already discounts him


def test_undated_news_gets_softer_penalty():
    starter = pl("1", "A", "RB", 10.0)
    alerts, _, _ = role.check_roster([starter], lambda pid: "He was demoted to a backup role.", SUNDAY, TZ)
    assert alerts and alerts[0].undated and alerts[0].factor == 0.75


def test_note_text_drops_projection_table():
    payload = '{"content": "<div>Benched Sunday.</div><table><tr><td>Week</td><td>Oct 3</td></tr></table>"}'
    assert role.note_text(payload) == "Benched Sunday."


# ---------------------------------------------------------- resend alerts
def test_urgent_alert_is_repeated_until_someone_acts(tmp_path: Path):
    st = State(tmp_path / "s.json")
    t0 = 1_000_000.0
    assert st.should_send("gameday", "X", True, t0, 30, 2) == (True, 0)
    assert st.should_send("gameday", "X", True, t0 + 600, 30, 2)[0] is False   # 10 min: wait
    assert st.should_send("gameday", "X", True, t0 + 1900, 30, 2) == (True, 1)  # 31 min: remind
    assert st.should_send("gameday", "X", True, t0 + 3800, 30, 2) == (True, 2)
    assert st.should_send("gameday", "X", True, t0 + 6000, 30, 2)[0] is False   # cap reached
    assert st.should_send("gameday", "X", False, t0 + 9000, 30, 2)[0] is False
    assert st.should_send("gameday", "Y", False, t0 + 9000, 30, 2) == (True, 0)  # new text


# ------------------------------------------------------ Q calibration
def test_questionable_factor_detects_double_discount():
    log, actual = [], {}
    for i in range(60):  # healthy: real = proj
        log.append({"time": "t", "week": 1, "pid": f"h{i}", "status": "", "proj": 10})
        actual[(1, f"h{i}")] = 10.0
    for i in range(60):  # Questionable: they play and score their (already lowered) projection
        log.append({"time": "t", "week": 1, "pid": f"q{i}", "status": "Q", "proj": 8})
        actual[(1, f"q{i}")] = 8.0
    res = calibration.questionable_factor(log, actual)
    assert abs(res["raw"] - 1.0) < 1e-9 and res["factor"] > 0.9
    assert "doble descuento" in res["texto"]


def test_questionable_factor_needs_data():
    res = calibration.questionable_factor([], {})
    assert res["factor"] == 0.75 and res["raw"] is None


def test_status_log_keeps_only_pregame(tmp_path: Path):
    path = tmp_path / "estado_log.csv"
    players = [pl("1", "A", "RB", 10.0, status="Q"), pl("2", "B", "RB", 9.0, locked=True)]
    assert calibration.log_statuses(path, 7, players, SUNDAY) == 1
    rows = calibration.read_log(path)
    assert rows[0]["pid"] == "1" and rows[0]["status"] == "Q"


def test_player_sd_shrinks_toward_position():
    rows = [{"pid": "1", "pos": "WR", "proj": 10, "real": r} for r in (0, 30, 2, 28)]
    sd = calibration.player_sd(rows, {"WR": 6.8})
    assert 6.8 < sd["1"] < 16


# ------------------------------------------------- win probability swap
def test_underdog_prefers_boom_bust_and_favorite_prefers_steady():
    steady = pl("1", "Steady", "W/R/T", 10.0)
    boom = pl("2", "Boom", "BN", 9.5)
    sd = {"1": 3.0, "2": 12.0}
    asg = [Assignment("W/R/T", steady)]
    under = win_prob_swap(asg, [boom], 100.0, 115.0, 20.0, sd_of=lambda p: sd[p.pid])
    assert under is not None and under.in_name == "Boom" and under.wp_after > under.wp_before
    fav = win_prob_swap(asg, [boom], 115.0, 100.0, 20.0, sd_of=lambda p: sd[p.pid])
    assert fav is None


# ---------------------------------------------------------- bid cap
def test_league_history_caps_ordinary_players_but_not_stars():
    s = WaiverSettings()
    weeks = list(range(7, 18))
    league = [1, 2, 3, 3, 5, 6, 7, 10, 11, 12]
    bid, why = suggest_bid(25.0, weeks, 90, s, league_bids=league)  # ~3 pts/week
    assert bid <= 13 and bid % 2 == 1 and "9 de cada 10" in why
    star, why = suggest_bid(60.0, weeks, 90, s, league_bids=league)  # ~7 pts/week
    assert star > 13 and "jugador clave" in why


def test_bids_are_odd():
    s = WaiverSettings()
    for gain in (3, 7, 12, 18, 26):
        bid, _ = suggest_bid(float(gain), list(range(7, 18)), 90, s)
        assert bid < 2 or bid % 2 == 1


# ------------------------------------------------- end to end on the mock
def test_lineup_run_flags_role_loss_and_repeats_alert(tmp_path):
    import json

    from mock_yahoo import MockYahoo
    from test_engine import SUNDAY_1145_ET, pilot

    with MockYahoo() as mock:
        ap = pilot(mock, tmp_path, now=SUNDAY_1145_ET, mode="sugerir")
        roster = ap.load_roster(3)
        ap.enrich(roster.players, 3)
        target = next(p for p in roster.players if p.is_starter and p.position == "RB" and not p.locked)
        original = mock.playernote

        def note(pid):
            if pid != target.pid:
                return original(pid)
            html = json.loads(original(pid))["content"]
            news = "<p>Sep 27, 9:53 AM: He has been benched after two fumbles and will not start.</p>"
            return json.dumps({"content": news + html})

        mock.playernote = note
        res = ap.lineup(gameday=True)
        alerts = res.data["role_alerts"]
        assert [a.name for a in alerts] == [target.name]
        assert "ALERTA DE ROL" in res.text
        if alerts[0].swap_pid:
            assert res.urgent and res.notify and "Alerta de rol" in res.title


def test_starter_out_without_backup_suggests_free_agent(tmp_path):
    from mock_yahoo import MockYahoo
    from test_engine import FRIDAY, pilot

    with MockYahoo() as mock:
        ap = pilot(mock, tmp_path, now=FRIDAY, mode="sugerir",
                   p_play_overrides={"Brock Purdy": 0.0, "Daniel Jones": 0.0})
        res = ap.lineup()
        assert res.urgent and "Puesto vacío" in res.title
        assert "Jacoby Brissett" in res.data["rescue"][0] and "+18.70" in res.data["rescue"][0]
        assert not mock.claims and not mock.posts  # suggestion only
