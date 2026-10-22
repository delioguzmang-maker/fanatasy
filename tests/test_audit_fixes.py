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


def test_blend_weights_per_position(tmp_path):
    from fantasy_autopilot.config import Config
    from fantasy_autopilot.engine import Autopilot

    class S:
        def projections(self, *a, **k):
            return {"s1": 20.0, "s2": 10.0}

    cfg = Config(league_id="1", team_id="1", state_dir=str(tmp_path), projections="blend",
                 blend_weight_pos={"WR": 0.5}, blend_scale_pos={"WR": 0.9}, season=2026)
    ap = Autopilot.__new__(Autopilot)
    ap.cfg, ap.sleeper, ap.warnings = cfg, S(), []
    ap._settings = type("L", (), {"reception_points": 0.5})()
    wr = pl("1", "W", "WR", 10.0, pos="WR", sleeper_id="s1")
    k = pl("2", "K", "K", 8.0, pos="K", sleeper_id="s2")
    ap._sleeper_projections([wr, k], 7)
    assert wr.proj == round(0.5 * 10 + 0.5 * 20 * 0.9, 2) and k.proj == 8.0  # kickers stay Yahoo


def test_second_kicker_is_a_dead_spot_but_bye_cover_is_not():
    from fantasy_autopilot.waivers import WaiverSettings, bench_upgrades

    slots = ["QB", "RB", "WR", "K"]
    wk = lambda v, bye=None: {w: (0.0 if w == bye else v) for w in range(7, 18)}
    roster = [Player("1", "QB1", "QB", eligible=["QB"], weekly=wk(20, bye=8)),
              Player("2", "QB2", "QB", eligible=["QB"], weekly=wk(14)),
              Player("3", "RB1", "RB", eligible=["RB"], weekly=wk(12)),
              Player("4", "WR1", "WR", eligible=["WR"], weekly=wk(11)),
              Player("5", "K1", "K", eligible=["K"], weekly=wk(8, bye=10)),
              Player("6", "K2", "K", eligible=["K"], weekly=wk(7))]
    from fantasy_autopilot.waivers import horizon, replacement_players

    fa = [Player("9", "Depth WR", "WR", eligible=["WR"], weekly=wk(8), owner="FA")]
    fa += [Player(f"k{i}", f"FA K{i}", "K", eligible=["K"], weekly=wk(7.5), owner="FA") for i in range(3)]
    s = WaiverSettings()
    repl = replacement_players(fa, slots, horizon(7, s), s)
    dead, ups = bench_upgrades(roster, fa, slots, 7, s, repl)
    # QB2 covers the week 8 bye: kept. K2 covers K1's week 10 bye, but a kicker is streamed for $0: dead.
    assert [p.name for p, _ in dead] == ["K2"]
    assert ups and ups[0].drop.name == "K2" and ups[0].add.name == "Depth WR" and not ups[0].on_waivers


def test_week_audit_separates_luck_from_mistakes():
    from fantasy_autopilot.audit import week_audit

    slots = ["QB", "RB", "W/R/T"]
    rows = [{"pid": "1", "jugador": "QB", "pos": "QB", "puesto": "QB", "proy": 24.37, "real": 19.36},
            {"pid": "2", "jugador": "RB", "pos": "RB", "puesto": "RB", "proy": 14.4, "real": 8.8},
            {"pid": "3", "jugador": "Skattebo", "pos": "RB", "puesto": "W/R/T", "proy": 10.24, "real": 0.0},
            {"pid": "4", "jugador": "Keenan", "pos": "WR", "puesto": "BN", "proy": 9.48, "real": 12.2}]
    a = week_audit(rows, slots, opponent=103.82)
    assert round(a.scored, 2) == 28.16 and round(a.perfect, 2) == 40.36
    assert a.decision_gap < 1 and a.came_in == (("Keenan", 12.2),) and a.went_out == (("Skattebo", 0.0),)
    assert "ni la alineación perfecta" in a.verdict()


def test_news_plus_usage_drop_confirms_role_loss():
    starter = pl("1", "Skattebo", "RB", 10.24)
    news = "Oct 18, 9:53 AM: Skattebo was benched after his fumbles."
    alerts, _, _ = role.check_roster([starter], lambda pid: news, SUNDAY, TZ, usage={"1": (2.0, 16.0)})
    assert alerts[0].factor == 0.3 and "2 toques" in alerts[0].describe(with_swap=False)
    quiet, _, _ = role.check_roster([starter], lambda pid: "", SUNDAY, TZ, usage={"1": (2.0, 16.0)})
    assert quiet[0].factor == 1.0 and "Sin noticia de rol" in quiet[0].describe()  # warn, no second discount
    none, _, _ = role.check_roster([starter], lambda pid: "", SUNDAY, TZ, usage={"1": (12.0, 16.0)})
    assert none == []


def test_questionable_not_on_inactive_list_counts_as_active(tmp_path):
    from datetime import datetime, timezone

    from mock_yahoo import MockYahoo
    from test_engine import pilot as make

    noon_15 = datetime(2026, 9, 27, 16, 15, tzinfo=timezone.utc)   # 12:15 pm ET, 1 pm games
    with MockYahoo() as mock:
        ap = make(mock, tmp_path, now=noon_15)
        r = ap.load_roster(3)
        ap.enrich(r.players, 3)
        warren = next(p for p in r.players if p.name == "Jaylen Warren")
        nacua = next(p for p in r.players if p.name == "Puka Nacua")      # Q, plays 8:20 pm
        assert warren.status == "Q" and warren.prob == 0.95
        assert nacua.prob == 0.75


def test_weak_bench_back_replaced_by_clearly_better_free_agent():
    from fantasy_autopilot.waivers import WaiverSettings, bench_upgrades

    slots = ["RB", "WR", "W/R/T"]
    wk = lambda v: {w: v for w in range(7, 18)}
    roster = [Player("1", "RB1", "RB", eligible=["RB", "W/R/T"], weekly=wk(14)),
              Player("2", "WR1", "WR", eligible=["WR", "W/R/T"], weekly=wk(13)),
              Player("3", "WR2", "WR", eligible=["WR", "W/R/T"], weekly=wk(9)),
              Player("4", "Gainwell", "RB", eligible=["RB", "W/R/T"], weekly=wk(5.5))]
    fa = [Player("8", "White", "RB", eligible=["RB", "W/R/T"], weekly=wk(7.0), owner="W (Wed)"),
          Player("9", "Meh", "RB", eligible=["RB", "W/R/T"], weekly=wk(6.0), owner="FA")]
    dead, ups = bench_upgrades(roster, fa, slots, 7, WaiverSettings())
    assert dead == [] and [(u.drop.name, u.add.name, u.on_waivers) for u in ups] == [("Gainwell", "White", True)]


def test_questionable_split_separates_not_playing_from_limited(tmp_path):
    log, actual, stats = [], {}, {6: {}, 5: {}, 4: {}}
    for i in range(40):
        sid, pid = f"s{i}", f"p{i}"
        log.append({"time": "t", "week": 6, "pid": pid, "pos": "RB", "status": "Q", "proj": 10, "sleeper_id": sid})
        for w in (4, 5):
            stats[w][sid] = {"off_snp": 40, "rush_att": 15, "rec_tgt": 5}
        if i % 4 == 0:                      # 1 of 4 did not play
            actual[(6, pid)] = 0.0
        else:                               # the rest played with half the work
            stats[6][sid] = {"off_snp": 20, "rush_att": 7, "rec_tgt": 3}
            actual[(6, pid)] = 6.0
    for i in range(40):
        sid, pid = f"h{i}", f"hp{i}"
        log.append({"time": "t", "week": 6, "pid": pid, "pos": "WR", "status": "", "proj": 10, "sleeper_id": sid})
        stats[6][sid] = {"off_snp": 50, "rec_tgt": 7}
        actual[(6, pid)] = 10.0
    res = calibration.questionable_split(log, actual, stats)
    assert res["n"] == 40 and res["played"] == 30 and abs(res["play_rate"] - 0.75) < 1e-9
    assert abs(res["workload"] - 0.5) < 1e-9 and abs(res["active_factor"] - 0.6) < 1e-9


def test_status_log_upgrades_old_header(tmp_path):
    path = tmp_path / "estado_log.csv"
    path.write_text("time,week,pid,name,pos,team,status,practice,proj,p_play,slot\nt,6,9,A,RB,X,Q,,10,0.75,RB\n")
    calibration.log_statuses(path, 7, [pl("1", "B", "RB", 8.0, sleeper_id="s1")], SUNDAY)
    rows = calibration.read_log(path)
    assert [r["pid"] for r in rows] == ["9", "1"] and rows[1]["sleeper_id"] == "s1" and rows[0]["sleeper_id"] == ""


def test_yahoo_cache_never_freezes_team_page_and_retries_999(monkeypatch):
    import types

    from fantasy_autopilot.yahoo import web as W

    calls = []
    codes = {"/f1/1/playernote": [999, 200], "/f1/1/7": [200, 200]}

    def get(url, params=None, timeout=40):
        path = url.split("yahoo.com")[1]
        calls.append(path)
        code = codes[path].pop(0) if codes[path] else 200
        return types.SimpleNamespace(url=url, text=f"<html>{len(calls)}</html>", status_code=code)

    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    y = W.YahooWeb("1", "7", [{"name": "T", "value": "x"}], delay=0, session=types.SimpleNamespace(
        get=get, cookies=types.SimpleNamespace(set=lambda *a, **k: None)))
    a = y.get("/playernote", {"pid": "5"})
    b = y.get("/playernote", {"pid": "5"})
    assert a is b and calls.count("/f1/1/playernote") == 2      # one 999, one retry, then cached
    y.get("/7"); y.get("/7")
    assert calls.count("/f1/1/7") == 2                          # team page: always fresh


def test_lineup_shows_deadline_next_check_and_logs_decision(tmp_path):
    import json as _json

    from mock_yahoo import MockYahoo
    from test_engine import FRIDAY, pilot as make

    with MockYahoo() as mock:
        res = make(mock, tmp_path, now=FRIDAY, mode="sugerir").lineup()
        assert res.changed and "PLAZO: haz estos cambios en Yahoo antes de" in res.text
        assert "PRÓXIMO CONTROL:" in res.text
        rec = [_json.loads(x) for x in (tmp_path / "decisiones_log.jsonl").read_text().splitlines()][-1]
        assert rec["decidido"] and rec["cambios"] and rec["yahoo_now"]["Rashod Bateman"] == "WR"


def test_resolved_urgent_alert_sends_one_confirmation(tmp_path):
    from fantasy_autopilot.engine import RunResult

    from mock_yahoo import MockYahoo
    from test_engine import FRIDAY, pilot as make

    sent = []
    with MockYahoo() as mock:
        ap = make(mock, tmp_path, now=FRIDAY)
        ap.notifier.ntfy_topic = "x"
        ap.notifier.send = lambda title, body, urgent=False, click_url=None: sent.append(title) or []
        ap.deliver(RunResult("gameday", "⚠ Cambia tu alineación YA", "A", changed=True, urgent=True))
        ap.deliver(RunResult("gameday", "Alineación: sin cambios", "B", notify=False))
        ap.deliver(RunResult("gameday", "Alineación: sin cambios", "B", notify=False))
    assert sent == ["⚠ Cambia tu alineación YA", "✓ Listo: Yahoo ya tiene la alineación recomendada"]


def test_no_second_discount_when_yahoo_already_lowered(tmp_path):
    import json

    from mock_yahoo import MockYahoo
    from test_engine import SUNDAY_1145_ET, pilot as make

    with MockYahoo() as mock:
        ap = make(mock, tmp_path, now=SUNDAY_1145_ET)
        r = ap.load_roster(3)
        ap.enrich(r.players, 3)
        sk = next(p for p in r.players if p.name == "Cam Skattebo")
        orig = mock.playernote

        def note(pid):
            if pid != sk.pid:
                return orig(pid)
            rows = "".join(f"<tr><td>{w}</td><td>vs X</td><td>*{v}</td></tr>" for w, v in ((3, 6.0), (4, 13.0), (5, 13.0)))
            return json.dumps({"content": "<p>Sep 27, 9:00 AM: Skattebo was benched after fumbles.</p>"
                               f"<table><thead><tr><th>Week</th><th>Opp</th><th>Fan Pts</th></tr></thead><tbody>{rows}</tbody></table>"})
        mock.playernote = note
        sk.proj = 6.0
        alerts, _ = ap.role_alerts(r)
        a = next(x for x in alerts if x.name == "Cam Skattebo")
        assert a.factor == 1.0 and "Yahoo ya bajó su proyección" in a.usage_note
