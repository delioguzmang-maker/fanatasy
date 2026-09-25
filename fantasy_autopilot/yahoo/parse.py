"""Pure HTML parsers for Yahoo Fantasy Football (football.fantasysports.yahoo.com).

Yahoo closed its Fantasy API to unapproved apps in July 2026, so the pages a
logged-in manager sees are the data source. Page anchors used here were
verified against live leagues in Aug-Sep 2026 by three open-source projects:

* team page ``/f1/<league>/<team>?week=N&stat1=P&stat2=PW``: ``table#statTable*``
  rows with ``td.pos`` (slot), ``td.player`` (link to /nfl/players/<id>, text
  "Team - POS"), ``td.pts`` (fantasy points; projections with stat1=P).
  On your own team every player row also has ``<select name="<player id>">``
  whose options are the eligible slots (+ BN, IR) and whose selected option is
  the current slot.
* players list ``/f1/<league>/players``: ``#players-table table`` rows with
  ``a[data-ys-playerid]`` and ``.ysf-player-name .D-b span`` ("Team - POS").
* ``/f1/<league>/playernote?pid=<id>``: JSON ``{"content": html}`` with a
  ``.playerinfo`` block and a Week / Fan Pts table ("*" = projection).
* league home ``?matchup_week=N&module=matchups``: ``#matchupweek li`` items.
* ``/f1/<league>/teams``: one row per team with the FAAB balance ("$83").

Everything is parsed defensively: missing pieces come back as None / empty
instead of raising, and callers decide what is essential.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..nfl import NON_STARTING, norm_slot, norm_team, team_from_name
from ..status import norm_status

KNOWN_SLOTS = {"QB", "RB", "WR", "TE", "K", "DEF", "W/R/T", "W/R", "W/T", "R/T", "Q/W/R/T", "BN", "IR",
               "D", "DB", "DL", "LB"}
_TEAM_POS_RE = re.compile(r"\b([A-Za-z]{2,4})\s*-\s*((?:QB|RB|WR|TE|K|DEF)(?:\s*,\s*(?:QB|RB|WR|TE|K|DEF))*)\b")
_STATUS_TOKEN_RE = re.compile(
    r"(?<![\w-])(Q|D|O|IR-R|IR|PUP-R|PUP-P|PUP|NFI-R|NFI-A|NFI|SUSP|NA|COVID-19)(?![\w-])"
)
_GAME_RE = re.compile(
    r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}:\d{2}\s*[ap]m\s*(?:vs|@)\s*[A-Za-z]{2,4}"
    r"|Final\s+[WLT]\s+\d+-\d+\s*(?:vs|@)\s*[A-Za-z]{2,4}"
    r"|Bye)",
    re.I,
)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def _text(el: Optional[Tag]) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def _float(text: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return float(m.group()) if m else None


def is_login_page(url: str, html: str) -> bool:
    """True when Yahoo bounced us to its sign-in flow (expired cookies)."""
    u = (url or "").lower()
    if "login.yahoo.com" in u or "/account/challenge" in u:
        return True
    head = (html or "")[:200000].lower()
    return 'type="password"' in head and "login" in head and "fantasysports" not in u


# --------------------------------------------------------------------------
# team page
# --------------------------------------------------------------------------

@dataclass
class TeamRow:
    pid: str
    name: str
    slot: str
    team: str = ""
    positions: list[str] = field(default_factory=list)
    eligible: list[str] = field(default_factory=list)   # from the <select>, if editable
    ir_eligible: bool = False
    editable: Optional[bool] = None                      # None = page without selects
    proj: Optional[float] = None
    status: str = ""
    status_raw: str = ""
    game_text: str = ""
    on_bye: bool = False


@dataclass
class TeamPage:
    rows: list[TeamRow]
    slot_labels: list[str]            # every slot row in page order, empty ones included
    week: Optional[int] = None
    faab_balance: Optional[int] = None
    has_selects: bool = False
    team_name: str = ""

    @property
    def starting_slots(self) -> list[str]:
        return [s for s in self.slot_labels if s not in NON_STARTING]

    @property
    def bench_size(self) -> int:
        return sum(1 for s in self.slot_labels if s == "BN")

    @property
    def ir_size(self) -> int:
        return sum(1 for s in self.slot_labels if s == "IR")


def _pid_from_link(a: Optional[Tag]) -> str:
    if a is None:
        return ""
    if a.get("data-ys-playerid"):
        return str(a["data-ys-playerid"]).strip()
    m = re.search(r"/players/(\d+)", a.get("href", ""))
    return m.group(1) if m else ""


def _player_link(tr: Tag) -> Optional[Tag]:
    return (
        tr.select_one("a[data-ys-playerid]")
        or tr.select_one("a[href*='/nfl/players/']")
        or tr.select_one("a[href*='/players/']")
        or tr.select_one("a[href*='/nfl/teams/']")
    )


def _status_of(cell: Tag, name: str) -> tuple[str, str]:
    for sel in ("abbr[title]", "[class*=injury]", "[class*=ysf-player-status]", "[class*=F-status]"):
        el = cell.select_one(sel)
        if el is None:
            continue
        for raw in (el.get("title", ""), _text(el)):
            code = norm_status(raw)
            if code:
                return code, raw
    text = _text(cell)
    if name:
        text = text.replace(name, " ")
    m = _TEAM_POS_RE.search(text)
    windows = [text[m.end():], text[: m.start()]] if m else [text]
    for window in windows:
        head = window.strip()[:12]
        tok = _STATUS_TOKEN_RE.match(head)
        if tok:
            return norm_status(tok.group(1)), tok.group(1)
    return "", ""


def _header_index(table: Tag, labels: tuple[str, ...]) -> Optional[int]:
    """Column index (in td/th units) of the first header matching one of `labels`."""
    for tr in table.select("thead tr"):
        idx = 0
        for cell in tr.find_all(["th", "td"]):
            span = int(cell.get("colspan", 1) or 1)
            if _text(cell).lower() in labels:
                return idx
            idx += span
    return None


def parse_team_page(html: str) -> TeamPage:
    soup = soup_of(html)
    tables = soup.select("table[id^=statTable]") or [
        t for t in soup.select("table") if t.select_one("td.pos") or t.select_one("select[name]")
    ]
    rows: list[TeamRow] = []
    slot_labels: list[str] = []
    has_selects = False
    for table in tables:
        pts_idx = _header_index(table, ("fan pts", "proj", "proj pts", "projected"))
        for tr in table.select("tbody tr") or table.select("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            pos_td = tr.select_one("td.pos") or cells[0]
            label = norm_slot(_text(pos_td).split(" ")[0]) if _text(pos_td) else ""
            select = next(
                (s for s in tr.select("select[name]") if re.fullmatch(r"\d+", s.get("name", ""))), None
            )
            if select is not None:
                has_selects = True
            player_td = tr.select_one("td.player") or next(
                (c for c in cells if _player_link(c) is not None), None
            )
            link = _player_link(tr)
            if label in KNOWN_SLOTS:
                slot_labels.append(label)
            if link is None or player_td is None or "(Empty)" in _text(player_td):
                continue

            name = (link.get("title") or _text(link)).strip()
            pid = select["name"] if select is not None else _pid_from_link(link)
            if not pid:
                m = re.search(r"/nfl/teams/([a-z]+)", link.get("href", ""))
                pid = f"def-{m.group(1)}" if m else re.sub(r"\W+", "-", name.lower())

            cell_text = _text(player_td)
            tp = _TEAM_POS_RE.search(cell_text.replace(name, " "))
            team = norm_team(tp.group(1)) if tp else ""
            positions = [x.strip() for x in tp.group(2).split(",")] if tp else []
            if not team and "DEF" in positions:
                team = team_from_name(name)

            eligible: list[str] = []
            ir_ok = False
            editable: Optional[bool] = None
            slot = label
            if select is not None:
                values = [norm_slot(o.get("value") or _text(o)) for o in select.find_all("option")]
                eligible = [v for v in values if v and v not in NON_STARTING]
                ir_ok = "IR" in values
                chosen = select.find("option", selected=True) or (select.find("option") if not label else None)
                if chosen is not None:
                    slot = norm_slot(chosen.get("value") or _text(chosen))
                editable = not select.has_attr("disabled")

            pts_cell = tr.select_one("td.pts")
            if pts_cell is None and pts_idx is not None and pts_idx < len(cells):
                pts_cell = cells[pts_idx]
            proj = _float(_text(pts_cell)) if pts_cell is not None else None

            status, raw = _status_of(player_td, name)
            game = _GAME_RE.search(cell_text)
            game_text = game.group(1) if game else ""
            rows.append(
                TeamRow(
                    pid=str(pid),
                    name=name,
                    slot=slot or "BN",
                    team=team,
                    positions=positions,
                    eligible=eligible,
                    ir_eligible=ir_ok,
                    editable=editable,
                    proj=proj,
                    status=status,
                    status_raw=raw,
                    game_text=game_text,
                    on_bye=bool(re.search(r"\bBye\b", game_text, re.I)),
                )
            )

    text = _text(soup.body or soup)
    week = None
    m = re.search(r"\bWeek\s+(\d{1,2})\b", text)
    if m:
        week = int(m.group(1))
    faab = None
    m = re.search(r"(?:FAAB|FAB)[^$\d]{0,40}\$\s*(\d{1,4})", text, re.I)
    if m:
        faab = int(m.group(1))
    title = _text(soup.select_one("title"))
    return TeamPage(rows=rows, slot_labels=slot_labels, week=week, faab_balance=faab,
                    has_selects=has_selects, team_name=title.split("|")[0].strip())


# --------------------------------------------------------------------------
# lineup form (HTTP replay of the "Save Changes" submit)
# --------------------------------------------------------------------------

class FormError(RuntimeError):
    pass


def build_lineup_submission(html: str, page_url: str, target: dict[str, str]) -> tuple[str, str, list[tuple[str, str]]]:
    """Recreate what the browser sends when you change slot <select>s and save.

    Returns (method, absolute action url, form fields). Raises FormError when the
    form, a player's <select>, or the requested slot option is missing, or when a
    player to move is locked (disabled select).
    """
    soup = soup_of(html)
    selects = {s["name"]: s for s in soup.select("select[name]") if re.fullmatch(r"\d+", s.get("name", ""))}
    if not selects:
        raise FormError("no hay <select> de posiciones en la página del equipo")
    missing = [pid for pid in target if pid not in selects]
    if missing:
        raise FormError(f"jugadores sin <select> (¿bloqueados?): {missing}")
    for pid, slot in target.items():
        sel = selects[pid]
        if sel.has_attr("disabled"):
            raise FormError(f"el jugador {pid} está bloqueado (su partido ya empezó)")
        options = [norm_slot(o.get("value") or _text(o)) for o in sel.find_all("option")]
        if norm_slot(slot) not in options:
            raise FormError(f"el jugador {pid} no puede ir a {slot} (opciones: {options})")
    form = selects[next(iter(target))].find_parent("form")
    if form is None:
        raise FormError("el <select> no está dentro de un <form>")

    fields: list[tuple[str, str]] = []
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "select":
            if name in target:
                wanted = norm_slot(target[name])
                opt = next(o for o in el.find_all("option") if norm_slot(o.get("value") or _text(o)) == wanted)
                fields.append((name, opt.get("value") or _text(opt)))
            else:
                opt = el.find("option", selected=True) or el.find("option")
                if opt is not None:
                    fields.append((name, opt.get("value") or _text(opt)))
        elif el.name == "textarea":
            fields.append((name, el.get_text()))
        else:
            kind = (el.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                continue
            if kind in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    fields.append((name, el.get("value", "on")))
                continue
            fields.append((name, el.get("value", "")))

    submit = None
    for cand in form.select("input[type=submit], button[type=submit], button:not([type])"):
        label = (cand.get("value") or _text(cand)).lower()
        if cand.get("name") == "jsubmit" or "save" in label or "submit" in label:
            submit = cand
            break
    if submit is not None and submit.get("name"):
        fields.append((submit["name"], submit.get("value") or _text(submit)))
    method = (form.get("method") or "get").lower()
    action = urljoin(page_url, form.get("action") or page_url)
    return method, action, fields


# --------------------------------------------------------------------------
# players list, player notes, league pages
# --------------------------------------------------------------------------

@dataclass
class ListedPlayer:
    pid: str
    name: str
    team: str = ""
    positions: list[str] = field(default_factory=list)
    availability: str = ""        # "FA", "W (Wed)", or owner text
    status: str = ""
    percent_owned: Optional[float] = None
    add_url: str = ""
    points: Optional[float] = None


def parse_players_page(html: str, base_url: str = "https://football.fantasysports.yahoo.com") -> list[ListedPlayer]:
    soup = soup_of(html)
    table = soup.select_one("#players-table table")
    if table is None:
        table = next((t for t in soup.select("table") if t.select_one("a[data-ys-playerid]")), None)
    if table is None:
        return []
    pts_idx = _header_index(table, ("fan pts", "proj", "proj pts"))
    out: list[ListedPlayer] = []
    seen: set[str] = set()
    for tr in table.select("tbody tr"):
        pid = ""
        for a in tr.select("a[data-ys-playerid]"):
            pid = str(a["data-ys-playerid"]).strip()
            if pid:
                break
        name_link = tr.select_one(".ysf-player-name a") or tr.select_one("a[href*='/nfl/players/']")
        if not pid:
            pid = _pid_from_link(name_link)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        name = _text(name_link) if name_link is not None else ""
        info = _text(tr.select_one(".ysf-player-name .D-b span")) or _text(tr)
        tp = _TEAM_POS_RE.search(info.replace(name, " ") if name else info)
        row_text = _text(tr)
        avail = ""
        m = re.search(r"\bW\s*\(([^)]{1,20})\)", row_text)
        if m:
            avail = f"W ({m.group(1)})"
        elif re.search(r"(?<![\w-])FA(?![\w-])", row_text):
            avail = "FA"
        pct = re.search(r"(\d{1,3})\s*%", row_text)
        add = tr.select_one("a[href*='addplayer'], a[href*='claim']")
        cells = tr.find_all("td")
        points = None
        if pts_idx is not None and pts_idx < len(cells):
            points = _float(_text(cells[pts_idx]))
        cell = tr.select_one("td.player") or tr
        status, _ = _status_of(cell, name)
        out.append(
            ListedPlayer(
                pid=pid,
                name=name,
                team=norm_team(tp.group(1)) if tp else "",
                positions=[x.strip() for x in tp.group(2).split(",")] if tp else [],
                availability=avail,
                status=status,
                percent_owned=float(pct.group(1)) if pct else None,
                add_url=urljoin(base_url, add["href"]) if add is not None else "",
                points=points,
            )
        )
    return out


@dataclass
class PlayerNote:
    pid: str
    name: str = ""
    positions: list[str] = field(default_factory=list)
    owner: str = ""               # "Free Agent", "Waivers (Wed)", or a team name
    owner_team_id: Optional[str] = None
    status: str = ""
    percent_owned: Optional[float] = None
    weekly: dict[int, float] = field(default_factory=dict)          # projected (or actual) points
    projected_weeks: set[int] = field(default_factory=set)          # weeks that are projections


def parse_playernote(payload: str, pid: str = "") -> PlayerNote:
    """Parse /f1/<league>/playernote?pid=<id> (JSON wrapper around HTML)."""
    try:
        html = json.loads(payload).get("content", "")
    except (ValueError, AttributeError):
        html = payload
    soup = soup_of(html)
    info = soup.select_one(".playerinfo") or soup
    note = PlayerNote(pid=pid)
    note.name = _text(info.select_one(".name"))
    pos = _text(info.select_one("dd.pos")).rstrip(",")
    note.positions = [p.strip() for p in re.split(r"[,/]", pos) if p.strip()]
    owner_dd = info.select_one("dd.owner")
    note.owner = _text(owner_dd).rstrip(",")
    if owner_dd is not None:
        a = owner_dd.select_one("a[href]")
        if a is not None:
            m = re.search(r"/(\d+)/?$", a["href"])
            note.owner_team_id = m.group(1) if m else None
    note.status = norm_status(_text(info.select_one(".status")))
    owned = _float(_text(info.select_one("dd.owned")))
    note.percent_owned = owned
    for table in soup.select("table"):
        headers = [_text(th).lower() for th in table.select("thead th")] or [
            _text(td).lower() for td in (table.select_one("tr") or soup_of("")).find_all(["th", "td"])
        ]
        if "week" not in headers or not any(h in ("fan pts", "pts", "proj") for h in headers):
            continue
        w_idx = headers.index("week")
        p_idx = next(i for i, h in enumerate(headers) if h in ("fan pts", "pts", "proj"))
        for tr in table.select("tbody tr") or table.select("tr")[1:]:
            cells = [_text(td) for td in tr.find_all(["td", "th"])]
            if len(cells) <= max(w_idx, p_idx):
                continue
            wk = re.search(r"\d+", cells[w_idx])
            if not wk:
                continue
            week, raw = int(wk.group()), cells[p_idx].strip()
            if raw in ("-", "", "Bye", "BYE"):
                note.weekly[week] = 0.0
                note.projected_weeks.add(week)
            elif raw.startswith("*"):
                note.weekly[week] = _float(raw) or 0.0
                note.projected_weeks.add(week)
            else:
                val = _float(raw)
                if val is not None:
                    note.weekly[week] = val
        break
    return note


@dataclass
class MatchupSide:
    team_id: str
    name: str
    score: Optional[float]
    projected: Optional[float]


def parse_matchups(html: str) -> list[tuple[MatchupSide, MatchupSide]]:
    soup = soup_of(html)
    out = []
    for li in soup.select("#matchupweek li"):
        ids = re.search(r"mid1=(\d+)&(?:amp;)?mid2=(\d+)", li.get("data-target", ""))
        sides = li.select("div.Grid-u-6-13")
        if not ids or len(sides) != 2:
            continue
        parsed = []
        for team_id, side in zip(ids.groups(), sides):
            link = side.select_one("a.F-link") or side.select_one("a[href]")
            score = _float(_text(side.select_one("div.Fz-lg")))
            proj = _float(_text(side.select_one(".Ta-end .F-shade")))
            parsed.append(MatchupSide(team_id, _text(link), score, proj))
        out.append((parsed[0], parsed[1]))
    return out


def parse_current_week(html: str) -> Optional[int]:
    m = re.search(r"Week (\d+)\s+Matchups", _text(soup_of(html)))
    return int(m.group(1)) if m else None


@dataclass
class LeagueTeam:
    team_id: str
    name: str
    manager: str = ""
    faab: Optional[int] = None


def parse_teams_page(html: str, league_id: str) -> list[LeagueTeam]:
    soup = soup_of(html)
    out: list[LeagueTeam] = []
    seen: set[str] = set()
    pat = re.compile(rf"/f1/{re.escape(str(league_id))}/(\d+)/?$")
    for tr in soup.select("table tbody tr"):
        links = [a for a in tr.select("a[href]") if pat.search(a["href"])]
        if not links:
            continue
        team_id = pat.search(links[0]["href"]).group(1)
        if team_id in seen:
            continue
        seen.add(team_id)
        name = next((_text(a) for a in links if _text(a)), f"Team {team_id}")
        cells = [_text(td) for td in tr.find_all("td")]
        faab = next((int(c.lstrip("$")) for c in cells if re.fullmatch(r"\$\d+", c)), None)
        manager = cells[1] if len(cells) > 1 else ""
        out.append(LeagueTeam(team_id, name, manager, faab))
    return out


@dataclass
class LeagueSettings:
    roster_positions: list[str] = field(default_factory=list)
    reception_points: Optional[float] = None
    uses_faab: Optional[bool] = None
    faab_budget: Optional[int] = None
    playoff_start_week: Optional[int] = None
    num_teams: Optional[int] = None

    @property
    def starting_slots(self) -> list[str]:
        return [s for s in self.roster_positions if s not in NON_STARTING]


def parse_settings_page(html: str) -> LeagueSettings:
    soup = soup_of(html)
    root = soup.select_one("#settings-content") or soup.body or soup
    text = root.get_text("\n", strip=True).replace("\xa0", " ")
    flat = re.sub(r"\s+", " ", text)
    out = LeagueSettings()
    m = re.search(r"Roster Positions:?\s*(.+?)(?=\s+[A-Z][a-z]|$)", flat)
    if m:
        for token in m.group(1).split(","):
            slot = norm_slot(token)
            if slot in KNOWN_SLOTS:
                out.roster_positions.append(slot)
    m = re.search(r"Receptions?\s+(-?\d+(?:\.\d+)?)", flat)
    if m:
        out.reception_points = float(m.group(1))
    m = re.search(r"Waiver Type:?\s*([^:]{0,80})", flat)
    if m:
        out.uses_faab = bool(re.search(r"\bFA?AB\b", m.group(1)))
    m = re.search(r"(?:FAAB|FAB)[^.:]{0,20}(?:Budget|Balance)[^$\d]{0,10}\$?\s*(\d{1,4})", flat, re.I)
    if m:
        out.faab_budget = int(m.group(1))
    m = re.search(r"Playoffs?:?[^.]{0,40}?Week\s+(\d{1,2})", flat)
    if m:
        out.playoff_start_week = int(m.group(1))
    m = re.search(r"Max(?:imum)? Teams:?\s*(\d{1,2})", flat)
    if m:
        out.num_teams = int(m.group(1))
    return out


@dataclass
class FaabTransaction:
    player: str
    amount: int
    text: str = ""


def parse_transactions_page(html: str) -> list[FaabTransaction]:
    """Best-effort: winning FAAB bids ("$11") listed on /f1/<league>/transactions."""
    soup = soup_of(html)
    out: list[FaabTransaction] = []
    for tr in soup.select("tr"):
        row = _text(tr)
        m = re.search(r"\$\s?(\d{1,3})\b", row)
        if not m:
            continue
        link = tr.select_one("a[href*='/nfl/players/'], a[href*='/nfl/teams/']")
        if link is None:
            continue
        out.append(FaabTransaction(_text(link), int(m.group(1)), row[:160]))
    return out
