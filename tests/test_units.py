import base64
import json
from datetime import datetime, timezone

import pytest

from fantasy_autopilot.config import Config
from fantasy_autopilot.matchup import win_probability
from fantasy_autopilot.models import Player
from fantasy_autopilot.nfl import eligible_slots, norm_name, norm_team, team_from_name
from fantasy_autopilot.sources.espn import parse_iso, parse_scoreboard
from fantasy_autopilot.status import norm_status, play_probability
from fantasy_autopilot.trade import evaluate_trade
from fantasy_autopilot.waivers import WaiverSettings, drop_costs, plan_claims, replacement_players, suggest_bid
from fantasy_autopilot.yahoo.cookies import cookie_header, describe, parse_cookies, to_playwright
from conftest import SLOTS

# ------------------------------------------------------------------ nfl / status


def test_team_and_slot_helpers():
    assert norm_team("Jax") == "JAX" and norm_team("WSH") == "WAS" and norm_team("la") == "LAR"
    assert team_from_name("Detroit") == "DET" and team_from_name("Lions") == "DET"
    assert team_from_name("New York") == ""  # ambiguous
    assert eligible_slots(["RB"], SLOTS) == ["RB", "W/R/T"]
    assert eligible_slots(["QB"], SLOTS + ["Q/W/R/T"]) == ["QB", "Q/W/R/T"]
    assert norm_name("Michael Pittman Jr.") == "michael pittman"
    assert norm_name("Ja'Marr Chase") == "jamarr chase"


@pytest.mark.parametrize("raw,code", [
    ("Q", "Q"), ("Questionable", "Q"), ("O", "O"), ("Out", "O"), ("IR-R", "IR"),
    ("Injured Reserve", "IR"), ("PUP-P", "PUP"), ("Sus", "SUSP"), ("NA", "NA"), ("", ""), ("Active", ""),
])
def test_norm_status(raw, code):
    assert norm_status(raw) == code


def test_play_probability_uses_practice():
    assert play_probability("Q") == 0.75
    assert play_probability("Q", "DNP") == 0.40
    assert play_probability("Q", "Full Participation") == 0.90
    assert play_probability("O") == 0.0 and play_probability("") == 1.0


# ------------------------------------------------------------------ cookies

def test_cookie_formats_round_trip():
    header = "Cookie: A1=aaa; A3=bbb; T=z=ttt&a=1; Y=v=1"
    cookies = parse_cookies(header)
    assert {c["name"] for c in cookies} == {"A1", "A3", "T", "Y"}
    assert cookie_header(cookies) == "A1=aaa; A3=bbb; T=z=ttt&a=1; Y=v=1"
    exported = json.dumps([{"domain": ".yahoo.com", "name": "T", "value": "x", "path": "/",
                            "expirationDate": 1800000000.5, "httpOnly": True, "secure": True}])
    assert parse_cookies(exported)[0]["expires"] == 1800000000.5
    b64 = base64.b64encode(exported.encode()).decode()
    assert parse_cookies(b64)[0]["name"] == "T"
    state = {"cookies": [{"name": "Y", "value": "v", "domain": ".yahoo.com", "path": "/", "expires": -1}]}
    assert parse_cookies(state)[0]["expires"] is None
    netscape = ".yahoo.com\tTRUE\t/\tTRUE\t1800000000\tA3\tabc\n#HttpOnly_.yahoo.com\tTRUE\t/\tTRUE\t0\tT\tdef\n"
    parsed = {c["name"]: c for c in parse_cookies(netscape)}
    assert parsed["A3"]["value"] == "abc" and parsed["T"]["httpOnly"]
    assert to_playwright(cookies)[0]["sameSite"] == "Lax"
    assert "T" in describe(cookies) and "ttt" not in describe(cookies)


# ------------------------------------------------------------------ espn

def test_espn_scoreboard_parsing():
    data = {"events": [{
        "id": "401", "date": "2026-09-27T17:00Z",
        "status": {"type": {"state": "pre"}},
        "competitions": [{"date": "2026-09-27T17:00Z", "competitors": [
            {"homeAway": "home", "team": {"abbreviation": "WSH"}},
            {"homeAway": "away", "team": {"abbreviation": "NYG"}},
        ]}],
    }]}
    g = parse_scoreboard(data)[0]
    assert (g.home, g.away, g.state) == ("WAS", "NYG", "pre")
    assert g.kickoff == datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
    assert parse_iso("2026-09-28T00:20:00Z").hour == 0


# ------------------------------------------------------------------ waivers

def p(pid, name, pos, weekly, slot="BN", owner=""):
    return Player(pid=pid, name=name, position=pos, eligible=eligible_slots([pos], SLOTS), slot=slot,
                  weekly=weekly, owner=owner)


def flat(v, weeks=range(3, 18)):
    return {w: v for w in weeks}


def small_roster():
    return [
        p("1", "QB1", "QB", flat(20), "QB"),
        p("2", "QB2", "QB", {**flat(15), 8: 15}),          # only useful in QB1's bye
        p("3", "RB1", "RB", flat(14), "RB"), p("4", "RB2", "RB", flat(12), "RB"),
        p("5", "WR1", "WR", flat(13), "WR"), p("6", "WR2", "WR", flat(11), "WR"),
        p("7", "FLEX", "WR", flat(9), "W/R/T"), p("8", "TE", "TE", flat(8), "TE"),
        p("9", "K", "K", flat(8), "K"), p("10", "DEF", "DEF", flat(7), "DEF"),
        p("11", "RB3", "RB", flat(6)), p("12", "WR4", "WR", flat(5)),
    ]


def test_bid_formula_and_caps():
    s = WaiverSettings()
    weeks = list(range(3, 18))
    bid, why = suggest_bid(20.0, weeks, 83, s)
    eff = sum(0.92 ** i for i in range(15))
    assert bid == round(83 * 20 / (8 * eff))
    assert "saldo $83" in why
    capped, why = suggest_bid(500.0, weeks, 83, s)
    assert capped == 41 and "50%" in why
    rival, why = suggest_bid(500.0, weeks, 83, s, rival_max_balance=12)
    assert rival == 13 and "ningún rival" in why
    assert suggest_bid(50.0, weeks, 83, s, on_waivers=False)[0] == 0
    ten_dollars = 10 * 8 * eff / 83                       # the gain that is worth exactly $10
    bid, why = suggest_bid(ten_dollars, weeks, 83, s)
    assert bid == 11 and "redondas" in why


def test_claims_pick_best_upgrade_and_cheapest_drop():
    roster = small_roster()
    qb1 = roster[0]
    qb1.weekly[8] = 0.0                                   # bye in week 8
    fa = [
        p("20", "Stud RB", "RB", flat(16), owner="W (Wed)"),
        p("21", "Meh WR", "WR", flat(4), owner="FA"),
        p("22", "Streamer QB", "QB", flat(16), owner="FA"),
        p("23", "Other QB", "QB", flat(15.5), owner="FA"),
        p("24", "Third QB", "QB", flat(15), owner="FA"),
    ]
    claims = plan_claims(roster, fa, SLOTS, 3, roster_limit=12, balance=83, settings=WaiverSettings(max_claims=1))
    assert len(claims) == 1
    c = claims[0]
    assert c.add.name == "Stud RB" and c.on_waivers and c.bid > 0
    assert c.drop.name in ("RB3", "WR4")                 # bench players who never start cost 0
    assert c.gain > 3.0


def test_backup_qb_is_worth_little_when_free_agents_can_cover_the_bye():
    roster = small_roster()
    roster[0].weekly[8] = 0.0                            # QB1 bye in week 8
    s = WaiverSettings()
    weeks = list(range(3, 18))
    fas = [p(str(40 + i), f"FA QB{i}", "QB", flat(v)) for i, v in enumerate((16, 15.5, 15))]
    with_repl = drop_costs(roster, SLOTS, weeks, s, replacement_players(fas, SLOTS, weeks, s))
    without = drop_costs(roster, SLOTS, weeks, s)
    assert without["2"] == pytest.approx(15 * 0.92 ** 5)            # 15 pts in week 8, discounted
    assert with_repl["2"] == pytest.approx((15 - 0.9 * 15) * 0.92 ** 5)  # only the edge over a streamer


def test_never_drop_is_respected():
    roster = small_roster()
    fa = [p("20", "Stud RB", "RB", flat(16), owner="FA")]
    names = {"QB2", "WR4", "RB3", "FLEX", "TE", "K", "DEF", "WR2", "RB2", "WR1", "RB1", "QB1"}
    claims = plan_claims(roster, fa, SLOTS, 3, 12, 83, WaiverSettings(never_drop=names - {"RB3"}, max_claims=1))
    assert claims[0].drop.name == "RB3" and claims[0].bid == 0   # free agent: no bid


# ------------------------------------------------------------------ trade

def test_trade_evaluator_flags_the_collins_mistake():
    """The skill's worked example: trading a WR2 for a bench-level RB loses points."""
    roster = small_roster()
    wr1 = roster[4]
    dobbins = p("30", "Dobbins", "RB", flat(9))
    rep = evaluate_trade(roster, [wr1], [dobbins], SLOTS, 3, 12, WaiverSettings())
    assert rep.delta < 0 and rep.verdict.startswith("RECHAZAR")
    assert any("NO mejora" in c for c in rep.checks)
    assert any("WR1" in c and "Titulares que se van" in c for c in rep.checks)


def test_trade_evaluator_accepts_a_real_upgrade():
    roster = small_roster()
    stud = p("31", "Stud WR", "WR", flat(20))
    rep = evaluate_trade(roster, [roster[11]], [stud], SLOTS, 3, 12, WaiverSettings())
    assert rep.delta > 5 and rep.verdict.startswith("ACEPTAR")


# ------------------------------------------------------------------ misc

def test_win_probability_symmetry():
    assert win_probability(100, 20, 100, 20) == pytest.approx(0.5)
    assert 0.55 < win_probability(103.41, 21, 94.28, 21) < 0.7


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("FA_LEAGUE_ID", "123456")
    monkeypatch.setenv("FA_TEAM_ID", "7")
    monkeypatch.setenv("FA_MODE", "automatico")
    monkeypatch.setenv("FA_AUTO_CLAIMS", "true")
    monkeypatch.setenv("FA_MAX_BID_PCT", "0.3")
    monkeypatch.setenv("FA_NEVER_DROP", "Alec Pierce, Puka Nacua")
    monkeypatch.setenv("FA_P_PLAY_OVERRIDES", '{"Puka Nacua": 0.3}')
    monkeypatch.setenv("FA_WEEK", "3")
    monkeypatch.setenv("NTFY_TOPIC", "mi-tema-secreto")
    cfg = Config.from_env()
    assert (cfg.league_id, cfg.team_id, cfg.mode, cfg.auto_claims) == ("123456", "7", "automatico", True)
    assert cfg.max_bid_pct == 0.3 and cfg.week == 3
    assert cfg.never_drop == ["Alec Pierce", "Puka Nacua"]
    assert cfg.p_play_overrides == {"Puka Nacua": 0.3}
    assert cfg.ntfy_topic == "mi-tema-secreto"
    with pytest.raises(ValueError):
        Config(mode="loco")


# ------------------------------------------------------------------ fixes from the first real run

def test_kickoff_from_yahoo_text_when_espn_is_blocked():
    from zoneinfo import ZoneInfo
    from fantasy_autopilot.engine import kickoff_from_text
    tz = ZoneInfo("America/New_York")
    friday = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    assert kickoff_from_text("Sun 8:20 pm vs Den", friday, tz) == datetime(2026, 9, 28, 0, 20, tzinfo=timezone.utc)
    assert kickoff_from_text("Sun 1:00 pm @ Cin", friday, tz) == datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
    assert kickoff_from_text("Mon 8:15 pm vs Chi", friday, tz) == datetime(2026, 9, 29, 0, 15, tzinfo=timezone.utc)
    assert kickoff_from_text("Final W 24-17 vs Hou", friday, tz) is None


def test_injured_players_are_discounted_in_future_weeks_too():
    from fantasy_autopilot.engine import apply_injury_outlook
    collins = p("1", "Collins", "WR", flat(12.0))
    collins.status, collins.p_play = "D", 0.10
    apply_injury_outlook(collins, 3)
    assert collins.weekly[3] == pytest.approx(1.2) and collins.weekly[4] == 6.0 and collins.weekly[5] == 12.0
    pierce = p("2", "Pierce", "WR", flat(8.0))
    pierce.status = "IR"
    apply_injury_outlook(pierce, 3)
    assert [pierce.weekly[w] for w in (3, 4, 5, 6, 7)] == [0, 0, 0, 0, 8.0]


def test_no_claim_drops_a_starter_or_overpays_a_kicker_or_adds_a_third_qb():
    """Regression for the first real run: 'Mevis for Worthy $7' and 'Brissett for Bateman'."""
    roster = small_roster()
    roster[0].weekly[8] = 0.0                                  # QB1 bye
    fa = [
        p("40", "Mevis", "K", flat(8.46), owner="W (Wed)"),     # +0.46/week over our K
        p("41", "Brissett", "QB", flat(18.7), owner="FA"),
    ]
    claims = plan_claims(roster, fa, SLOTS, 3, roster_limit=12, balance=100)
    assert claims == []                                         # neither clears the minimum gain
    loose = WaiverSettings(min_gain=0.1)
    claims = plan_claims(roster, fa, SLOTS, 3, 12, 100, loose)
    starters_now = {"QB1", "RB1", "RB2", "WR1", "WR2", "FLEX", "TE", "K", "DEF"}
    for c in claims:
        assert c.drop is None or c.drop.name not in starters_now
        if c.add.name == "Brissett":
            assert c.drop is not None and c.drop.position == "QB"  # swap, never a third QB
        if c.add.position == "K":
            assert c.bid <= 2
