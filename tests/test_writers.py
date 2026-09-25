"""End-to-end write tests against a local mock of Yahoo's classic pages."""

import os

import pytest

from fantasy_autopilot.yahoo.web import YahooSessionExpired, YahooWeb
from mock_yahoo import LEAGUE, TEAM, MockYahoo

COOKIES = [{"name": "T", "value": "session", "domain": "127.0.0.1", "path": "/"},
           {"name": "Y", "value": "abc", "domain": "127.0.0.1", "path": "/"}]
TARGET = {"31883": "BN", "40009": "WR", "41100": "W/R/T"}  # Bateman out, Nabers WR, Tuten FLEX


def web(mock):
    return YahooWeb(LEAGUE, TEAM, COOKIES, base_url=mock.base_url, delay=0)


def test_http_lineup_dry_run_changes_nothing():
    with MockYahoo() as mock:
        result = web(mock).set_lineup(TARGET, week=3, dry_run=True)
        assert result.ok and result.dry_run
        assert mock.slots["41100"] == "BN" and not mock.posts


def test_http_lineup_save_and_verify():
    with MockYahoo() as mock:
        result = web(mock).set_lineup(TARGET, week=3, dry_run=False)
        assert result.ok, result
        assert mock.slots["41100"] == "W/R/T"
        assert mock.slots["40009"] == "WR"
        assert mock.slots["31883"] == "BN"
        assert mock.slots["32671"] == "QB"  # untouched


def test_http_lineup_rejects_locked_or_invalid_moves():
    with MockYahoo() as mock:
        result = web(mock).set_lineup({"32671": "WR"}, week=3, dry_run=False)
        assert not result.ok
        assert not mock.posts


def test_expired_cookies_are_detected():
    with MockYahoo() as mock:
        client = YahooWeb(LEAGUE, TEAM, COOKIES[1:], base_url=mock.base_url, delay=0)
        with pytest.raises(YahooSessionExpired):
            client.team_page(week=3)


# ------------------------------------------------------------------ browser
def _browser(mock, tmp_path):
    pytest.importorskip("playwright")
    from fantasy_autopilot.yahoo.browser import YahooBrowser

    cookies = [{"name": "T", "value": "session", "domain": "127.0.0.1", "path": "/"}]
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or (
        "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
    )
    return YahooBrowser(LEAGUE, TEAM, cookies, base_url=mock.base_url, evidence_dir=tmp_path,
                        timeout_ms=15000, executable_path=exe)


def _skip_without_chromium(exc: Exception):
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
        pytest.skip("Chromium for Playwright is not installed")
    raise exc


def test_browser_lineup_save_and_verify(tmp_path):
    with MockYahoo() as mock:
        try:
            result = _browser(mock, tmp_path).set_lineup(TARGET, week=3, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            _skip_without_chromium(exc)
        assert result.ok, result
        assert mock.slots["41100"] == "W/R/T" and mock.slots["31883"] == "BN"
        assert any(p.endswith(".png") for p in result.evidence)


def test_browser_claim_dry_run_then_live(tmp_path):
    with MockYahoo() as mock:
        try:
            browser = _browser(mock, tmp_path)
            dry = browser.claim("30150", drop_pid="30121", bid=7, dry_run=True)
        except Exception as exc:  # noqa: BLE001
            _skip_without_chromium(exc)
        assert dry.ok and dry.dry_run, dry
        assert "Submit Claim" in dry.detail
        assert not mock.claims

        live = browser.claim("30150", drop_pid="30121", bid=7, dry_run=False)
        assert live.ok, live
        assert mock.claims == [{"apid": "30150", "crumb": "abc123", "dpid": "30121", "faab": "7"}]


def test_browser_claim_reports_missing_drop_option(tmp_path):
    with MockYahoo() as mock:
        try:
            result = _browser(mock, tmp_path).claim("30150", drop_pid="99999", bid=5, dry_run=True)
        except Exception as exc:  # noqa: BLE001
            _skip_without_chromium(exc)
        assert not result.ok
        assert "soltar" in result.detail
