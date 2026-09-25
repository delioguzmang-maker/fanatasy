"""Read your Yahoo league with your browser session, and save lineups over HTTP.

Everything goes through the same pages you see at
football.fantasysports.yahoo.com, one request at a time with a pause in
between (``delay``), like a person clicking around.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Union

import requests

from ..models import WriteResult
from ..nfl import norm_slot
from ..sources.http import BROWSER_UA, make_session
from . import parse
from .cookies import CookieSource, parse_cookies

BASE_URL = "https://football.fantasysports.yahoo.com"


class YahooSessionExpired(RuntimeError):
    """Yahoo sent us to the login page: the saved cookies no longer work."""


class YahooPageError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    html: str


class YahooWeb:
    def __init__(
        self,
        league_id: Union[str, int],
        team_id: Union[str, int],
        cookies: CookieSource,
        base_url: str = BASE_URL,
        delay: float = 1.0,
        session: Optional[requests.Session] = None,
        user_agent: str = BROWSER_UA,
    ):
        self.league_id = str(league_id)
        self.team_id = str(team_id)
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.cookies = parse_cookies(cookies) if cookies else []
        if not self.cookies:
            raise ValueError("No se encontraron cookies de Yahoo (revisa YAHOO_COOKIES).")
        self.session = session or make_session(user_agent)
        # Cookies go in the jar (not a raw "Cookie" header): requests drops a
        # hand-set header when it follows a redirect, e.g. after saving a lineup.
        for c in self.cookies:
            self.session.cookies.set(
                c["name"], c["value"], domain=c.get("domain") or ".yahoo.com", path=c.get("path") or "/"
            )
        self._last = 0.0

    # ------------------------------------------------------------------ http
    def url(self, path: str = "") -> str:
        return f"{self.base_url}/f1/{self.league_id}{path}"

    def _pause(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str = "", params: Optional[dict] = None) -> Page:
        self._pause()
        resp = self.session.get(self.url(path), params=params, timeout=40)
        if parse.is_login_page(resp.url, resp.text):
            raise YahooSessionExpired(
                "Yahoo pidió iniciar sesión: las cookies vencieron. Copia de nuevo el valor "
                "de 'Cookie' desde tu navegador (ver README, paso 2) y actualiza YAHOO_COOKIES."
            )
        if resp.status_code >= 400:
            raise YahooPageError(f"GET {resp.url} -> HTTP {resp.status_code}")
        return Page(resp.url, resp.text)

    # ----------------------------------------------------------------- reads
    def team_page(self, team_id: Optional[str] = None, week: Optional[int] = None) -> tuple[Page, parse.TeamPage]:
        params = {"stat1": "P", "stat2": "PW"}
        if week:
            params["week"] = str(week)
        page = self.get(f"/{team_id or self.team_id}", params)
        return page, parse.parse_team_page(page.html)

    def league_home(self, week: Optional[int] = None) -> Page:
        params = {"matchup_week": str(week), "module": "matchups"} if week else None
        return self.get("", params)

    def current_week(self) -> Optional[int]:
        return parse.parse_current_week(self.league_home().html)

    def matchups(self, week: int) -> list[tuple[parse.MatchupSide, parse.MatchupSide]]:
        return parse.parse_matchups(self.league_home(week).html)

    def teams(self) -> list[parse.LeagueTeam]:
        return parse.parse_teams_page(self.get("/teams").html, self.league_id)

    def settings(self) -> parse.LeagueSettings:
        return parse.parse_settings_page(self.get("/settings").html)

    def players(self, position: str, status: str = "A", sort: str = "PR_S", start: int = 0) -> list[parse.ListedPlayer]:
        params = {"status": status, "pos": position, "sort": sort, "stat1": "K_K", "count": str(start)}
        return parse.parse_players_page(self.get("/players", params).html, self.base_url)

    def playernote(self, pid: str) -> parse.PlayerNote:
        page = self.get("/playernote", {"pid": pid})
        return parse.parse_playernote(page.html, pid)

    def transactions(self) -> list[parse.FaabTransaction]:
        return parse.parse_transactions_page(self.get("/transactions").html)

    # ---------------------------------------------------------------- writes
    def set_lineup(self, target: dict[str, str], week: Optional[int] = None, dry_run: bool = True) -> WriteResult:
        """Change slots by replaying Yahoo's roster form, then re-read to verify.

        `target` maps Yahoo player id -> new slot ("BN", "WR", "W/R/T", ...).
        """
        target = {pid: norm_slot(slot) for pid, slot in target.items()}
        if not target:
            return WriteResult(ok=True, action="lineup", detail="sin cambios", dry_run=dry_run)
        page, _ = self.team_page(week=week)
        try:
            method, action, fields = parse.build_lineup_submission(page.html, page.url, target)
        except parse.FormError as exc:
            return WriteResult(ok=False, action="lineup", detail=f"formulario de alineación: {exc}", dry_run=dry_run)
        planned = [f"{pid}->{slot}" for pid, slot in target.items()]
        if dry_run:
            return WriteResult(ok=True, action="lineup", detail="simulacro (no se envió nada)",
                               applied=planned, dry_run=True)
        self._pause()
        headers = {"Referer": page.url, "Origin": self.base_url}
        if method == "post":
            resp = self.session.post(action, data=fields, headers=headers, timeout=40)
        else:
            resp = self.session.get(action, params=fields, headers=headers, timeout=40)
        if parse.is_login_page(resp.url, resp.text):
            raise YahooSessionExpired("Yahoo pidió iniciar sesión al guardar la alineación.")
        return self.verify_lineup(target, week)

    def verify_lineup(self, target: dict[str, str], week: Optional[int] = None) -> WriteResult:
        _, fresh = self.team_page(week=week)
        now = {r.pid: r.slot for r in fresh.rows}
        applied, failed = [], []
        for pid, slot in target.items():
            (applied if now.get(pid) == slot else failed).append(f"{pid}->{slot} (ahora {now.get(pid, '?')})")
        ok = not failed
        detail = "alineación guardada y verificada" if ok else "Yahoo no aplicó todos los cambios"
        return WriteResult(ok=ok, action="lineup", detail=detail, applied=applied, failed=failed)
