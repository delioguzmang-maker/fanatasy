"""A tiny stand-in for football.fantasysports.yahoo.com used by the write tests.

It serves the team-page fixture with the current slots, accepts the roster
form POST (checking the crumb), and implements a classic add/drop + FAAB
claim form. Requests without the session cookie are sent to a login page.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"
LEAGUE, TEAM = "123456", "7"


class MockYahoo:
    def __init__(self) -> None:
        self.template = (FIXTURES / "team_page.html").read_text()
        soup = BeautifulSoup(self.template, "lxml")
        self.slots = {}
        for sel in soup.select("select[name]"):
            opt = sel.find("option", selected=True)
            self.slots[sel["name"]] = opt["value"]
        self.options = {s["name"]: [o["value"] for o in s.find_all("option")] for s in soup.select("select[name]")}
        self.claims: list[dict] = []
        self.posts: list[dict] = []
        self.gets: list[str] = []
        # weekly projections served by /playernote: roster players get a flat line
        # equal to their team-page projection; free agents are defined here.
        self.proj = {}
        for tr in soup.select("tr"):
            sel = tr.select_one("select[name]")
            pts = tr.select_one("td.pts")
            if sel is not None and pts is not None:
                try:
                    self.proj[sel["name"]] = float(pts.get_text(strip=True))
                except ValueError:
                    self.proj[sel["name"]] = 0.0
        self.free_agents = {
            "30150": ("Michael Pittman Jr.", "WR", "Waivers (Sep 30)", {w: 9.5 for w in range(1, 18)}),
            "40600": ("Braelon Allen", "RB", "Free Agent", {w: 5.27 for w in range(1, 18)}),
            "28000": ("Jacoby Brissett", "QB", "Free Agent", {w: 18.7 for w in range(1, 18)}),
        }
        self.pages = {
            f"/f1/{LEAGUE}": (FIXTURES / "league_home.html").read_text(),
            f"/f1/{LEAGUE}/teams": (FIXTURES / "teams_page.html").read_text(),
            f"/f1/{LEAGUE}/settings": (FIXTURES / "settings_page.html").read_text(),
            f"/f1/{LEAGUE}/players": (FIXTURES / "players_page.html").read_text(),
            f"/f1/{LEAGUE}/4": (FIXTURES / "opponent_page.html").read_text(),
            f"/f1/{LEAGUE}/transactions": "<html><body><table>"
            "<tr><td><a href='https://sports.yahoo.com/nfl/players/1'>Josh Downs</a></td><td>Waiver $11</td></tr>"
            "<tr><td><a href='https://sports.yahoo.com/nfl/teams/kc'>Kansas City</a></td><td>Waiver $12</td></tr>"
            "</table></body></html>",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MockYahoo":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()

    def team_html(self) -> str:
        soup = BeautifulSoup(self.template, "lxml")
        for sel in soup.select("select[name]"):
            for opt in sel.find_all("option"):
                if opt["value"] == self.slots[sel["name"]]:
                    opt["selected"] = ""
                elif opt.has_attr("selected"):
                    del opt["selected"]
            label = sel.find_parent("tr").select_one("td.pos span")
            label.string = self.slots[sel["name"]]
        return str(soup)

    def set_status(self, pid: str, status: str) -> None:
        """Change the injury tag a player shows on the team page (e.g. game-day Out)."""
        soup = BeautifulSoup(self.template, "lxml")
        cell = soup.select_one(f'select[name="{pid}"]').find_parent("tr").select_one("td.player")
        tag = cell.select_one("abbr")
        if tag is None:
            tag = soup.new_tag("abbr", attrs={"class": "F-injury"})
            cell.select_one(".ysf-player-name").append(tag)
        tag.string = status
        tag["title"] = status
        self.template = str(soup)

    def playernote(self, pid: str) -> str:
        import json

        if pid in self.free_agents:
            name, pos, owner, weekly = self.free_agents[pid]
        elif pid in self.proj:
            name, pos, owner, weekly = f"Player {pid}", "WR", "Mi Equipo", {w: self.proj[pid] for w in range(1, 18)}
        else:
            name, pos, owner, weekly = f"Player {pid}", "WR", "Other Team", {w: 10.0 for w in range(1, 18)}
        owner_html = (f'<a href="/f1/{LEAGUE}/{TEAM if pid in self.proj else 4}">{owner}</a>'
                      if owner not in ("Free Agent",) and not owner.startswith("Waivers") else owner)
        rows = "".join(
            f"<tr><td>{w}</td><td>vs X</td><td>{'*' if w >= 3 else ''}{v:.2f}</td></tr>" for w, v in sorted(weekly.items())
        )
        html = (f'<div class="playerinfo"><div class="name">{name}</div><dl><dd class="pos">{pos},</dd>'
                f'<dd class="owner">{owner_html},</dd><dd class="owned">10% owned</dd></dl></div>'
                f"<table><thead><tr><th>Week</th><th>Opp</th><th>Fan Pts</th></tr></thead><tbody>{rows}</tbody></table>")
        return json.dumps({"content": html})

    def add_html(self, apid: str) -> str:
        rows = "".join(
            f'<tr><td><input type="radio" name="dpid" value="{pid}"/></td>'
            f'<td><a href="https://sports.yahoo.com/nfl/players/{pid}">Player {pid}</a></td></tr>'
            for pid in self.slots
        )
        return f"""<html><body><h2>Add Player</h2>
<nav><a href="/f1/{LEAGUE}/players">Add Player</a></nav>
<form method="post" action="/f1/{LEAGUE}/addplayer">
<input type="hidden" name="apid" value="{apid}"/><input type="hidden" name="crumb" value="abc123"/>
<p>Your roster is full. Select a player to drop:</p><table>{rows}</table>
<p>FAAB balance: $83. Your bid: $<input type="text" name="faab" value=""/></p>
<button type="button">Cancel</button><button type="submit">Submit Claim</button>
</form></body></html>"""

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, code: int, body: str = "", location: str = "") -> None:
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def _authed(self) -> bool:
                return "T=session" in (self.headers.get("Cookie") or "")

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                if url.path == "/login":
                    return self._send(200, '<html><body>Sign in to login <input type="password"/></body></html>')
                if not self._authed():
                    return self._send(302, location="/login")
                mock.gets.append(self.path)
                if url.path == f"/f1/{LEAGUE}/{TEAM}":
                    return self._send(200, mock.team_html())
                if url.path == f"/f1/{LEAGUE}/playernote":
                    return self._send(200, mock.playernote(parse_qs(url.query).get("pid", [""])[0]))
                if url.path in mock.pages:
                    return self._send(200, mock.pages[url.path])
                if url.path == f"/f1/{LEAGUE}/addplayer":
                    return self._send(200, mock.add_html(parse_qs(url.query).get("apid", [""])[0]))
                return self._send(404, "not found")

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return self._send(302, location="/login")
                length = int(self.headers.get("Content-Length", 0))
                data = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                mock.posts.append({"path": self.path, "data": data})
                if data.get("crumb") != "abc123":
                    return self._send(400, "bad crumb")
                if self.path == f"/f1/{LEAGUE}/{TEAM}/editroster":
                    new = {pid: data.get(pid, slot) for pid, slot in mock.slots.items()}
                    if any(slot not in mock.options[pid] for pid, slot in new.items()):
                        return self._send(400, "invalid slot")
                    mock.slots.update(new)
                    return self._send(302, location=f"/f1/{LEAGUE}/{TEAM}")
                if self.path == f"/f1/{LEAGUE}/addplayer":
                    if not data.get("dpid") or not data.get("faab", "").isdigit():
                        return self._send(200, "<html><body>There was a problem with your claim.</body></html>")
                    mock.claims.append(data)
                    return self._send(200, "<html><body>Your waiver claim has been submitted.</body></html>")
                return self._send(404, "not found")

        return Handler
