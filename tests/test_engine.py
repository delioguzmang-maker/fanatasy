"""The whole autopilot against the local Yahoo mock, with fake Sleeper/ESPN data."""

from datetime import datetime, timezone

import pytest

from fantasy_autopilot.config import Config
from fantasy_autopilot.engine import Autopilot
from fantasy_autopilot.models import Game
from fantasy_autopilot.notify import Notifier
from fantasy_autopilot.yahoo.web import YahooWeb
from mock_yahoo import LEAGUE, TEAM, MockYahoo

COOKIES = [{"name": "T", "value": "session", "domain": "127.0.0.1", "path": "/"}]
FRIDAY = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
SUNDAY_1145_ET = datetime(2026, 9, 27, 15, 45, tzinfo=timezone.utc)

ONE_PM = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)
FOUR_25 = datetime(2026, 9, 27, 20, 25, tzinfo=timezone.utc)
SNF = datetime(2026, 9, 28, 0, 20, tzinfo=timezone.utc)
GAMES = [
    Game("LAC", "KC", FOUR_25), Game("SF", "ARI", FOUR_25), Game("BAL", "CLE", FOUR_25),
    Game("NYG", "TEN", ONE_PM), Game("CIN", "PIT", ONE_PM), Game("DET", "CHI", ONE_PM),
    Game("JAX", "NE", ONE_PM), Game("IND", "BUF", ONE_PM), Game("HOU", "TB", ONE_PM),
    Game("ATL", "CAR", ONE_PM), Game("SEA", "DAL", FOUR_25), Game("LAR", "DEN", SNF),
]


class FakeSleeper:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def state(self):
        return {"week": 3, "season": "2026"}

    def match(self, yahoo_id="", name="", team="", position=""):
        info = self.statuses.get(yahoo_id)
        if info is None:
            return None
        return {"player_id": f"s{yahoo_id}", "injury_status": info.get("status"),
                "practice_participation": info.get("practice"), "news_updated": info.get("news")}

    def players(self):
        return {}

    def trending_adds(self, *a, **k):
        return []

    def projections(self, *a, **k):
        return {}


def pilot(mock, tmp_path, now=FRIDAY, sleeper=None, **cfg):
    config = Config(league_id=LEAGUE, team_id=TEAM, week=3, season=2026, state_dir=str(tmp_path),
                    use_trending=False, save_evidence=False, **cfg)
    web = YahooWeb(LEAGUE, TEAM, COOKIES, base_url=mock.base_url, delay=0)
    return Autopilot(config, cookies=COOKIES, web=web, sleeper=sleeper or FakeSleeper(),
                     notifier=Notifier(echo=False), games_fetcher=lambda season, week: GAMES,
                     now_fn=lambda: now)


def moves_of(result):
    return {(m.player.name, m.from_slot, m.to_slot) for m in result.data["moves"]}


def test_weekly_lineup_suggest_mode(tmp_path):
    with MockYahoo() as mock:
        res = pilot(mock, tmp_path, mode="sugerir").lineup()
        assert res.changed
        assert moves_of(res) == {
            ("Rashod Bateman", "WR", "BN"),
            ("Malik Nabers", "W/R/T", "WR"),
            ("Bhayshul Tuten", "BN", "W/R/T"),
        }
        assert "103.41 → 104.66" in res.text
        assert not mock.posts                      # suggest mode never writes


def test_weekly_lineup_automatic_mode_saves(tmp_path):
    with MockYahoo() as mock:
        res = pilot(mock, tmp_path, mode="automatico").lineup()
        assert res.writes and res.writes[0].ok, res.text
        assert mock.slots["41100"] == "W/R/T" and mock.slots["31883"] == "BN"


def test_gameday_does_nothing_far_from_kickoff(tmp_path):
    with MockYahoo() as mock:
        res = pilot(mock, tmp_path, mode="automatico").gameday()
        assert not res.notify and "Sin partidos" in res.title
        assert not any(g.startswith(f"/f1/{LEAGUE}/{TEAM}") for g in mock.gets)


def test_gameday_swaps_an_inactive_starter(tmp_path):
    with MockYahoo() as mock:
        mock.set_status("33423", "O")              # Warren ruled out at 11:30
        res = pilot(mock, tmp_path, now=SUNDAY_1145_ET, mode="automatico").gameday()
        assert res.changed and res.writes[0].ok, res.text
        assert mock.slots["33423"] == "BN"         # Warren benched
        assert mock.slots["41100"] == "RB"         # Tuten into his RB slot
        assert "Alineación ajustada" in res.title


def test_sleeper_news_escalates_status(tmp_path):
    fresh = FRIDAY.timestamp() - 3600
    sleeper = FakeSleeper({"33536": {"status": "Out", "news": fresh}, "33423": {"status": "Questionable", "practice": "Limited"}})
    with MockYahoo() as mock:
        res = pilot(mock, tmp_path, sleeper=sleeper).lineup()
        roster = {p.name: p for p in res.data["roster"].players}
        assert roster["Puka Nacua"].status == "O" and roster["Puka Nacua"].prob == 0.0
        assert roster["Jaylen Warren"].practice == "Limited" and roster["Jaylen Warren"].prob == pytest.approx(0.75)


def test_waivers_no_bid_when_nobody_helps(tmp_path):
    with MockYahoo() as mock:
        mock.free_agents["30150"] = ("Michael Pittman Jr.", "WR", "Waivers (Sep 30)", {w: 7.9 for w in range(1, 18)})
        res = pilot(mock, tmp_path).waivers()
        assert res.data["claims"] == []
        assert "No pujes" in res.text
        assert "Saldo FAAB: $83" in res.text and "$100" in res.text


def test_waivers_bid_for_a_real_upgrade(tmp_path):
    with MockYahoo() as mock:
        mock.free_agents["30150"] = ("Michael Pittman Jr.", "WR", "Waivers (Sep 30)", {w: 14.0 for w in range(1, 18)})
        res = pilot(mock, tmp_path).waivers()
        claims = res.data["claims"]
        assert claims and claims[0].add.name == "Michael Pittman Jr."
        assert claims[0].on_waivers and claims[0].drop is None   # there is an empty bench spot
        assert 25 <= claims[0].bid <= 41                           # value-based, capped at 50% of $83
        assert "saldo $83" in claims[0].bid_reason
        assert not mock.claims                                     # suggest mode: nothing submitted


def test_matchup_finds_opponent_mistakes(tmp_path):
    with MockYahoo() as mock:
        res = pilot(mock, tmp_path).matchup()
        audit = res.data["audit"]
        assert any("A.J. Brown (IR) está de titular" in x for x in audit.issues)
        assert any("Patrick Mahomes (23.91) está en la banca" in x for x in audit.issues)
        assert audit.optimal > audit.current
        assert 0.0 < res.data["win_probability"] < 1.0


def test_expired_session_becomes_urgent_notification(tmp_path):
    with MockYahoo() as mock:
        p = pilot(mock, tmp_path)
        p.web.session.cookies.clear()
        res = p.run("lineup")
        assert res.urgent and "Sesión de Yahoo vencida" in res.title
