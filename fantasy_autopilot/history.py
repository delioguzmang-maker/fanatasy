"""Past weeks from Yahoo's matchup page: each player's original projection and real points.

Yahoo keeps the *original* projection of every player on the matchup page of a
finished week (columns Proj / Fan Pts on both sides). That is the training data
for learning where Yahoo misses in this league. The parser reads the page's
visible text (what you get by selecting the page and pasting it), so it works
both on text pasted by hand and on the page HTML flattened to lines.

Row layout on that page (left team | slot | right team):

    Brock Purdy / Brock PurdyVideo Forecast... / SF - QB / Final L 20-29 @ Atl
    22.05  (left proj)   14.02 (left pts)   QB   20.52 (right pts)   22.80 (right proj)
    C.J. Stroud / C.J. StroudVideo Forecast... / Hou - QB / Final ...
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from bs4 import BeautifulSoup

from .nfl import norm_slot, norm_team

SLOT_LINES = {"QB", "RB", "WR", "TE", "W/R/T", "W/R", "W/T", "Q/W/R/T", "K", "DEF", "BN", "IR"}
TEAM_POS_RE = re.compile(r"^([A-Za-z]{2,3}) - (QB|RB|WR|TE|K|DEF)(?:,(?:QB|RB|WR|TE|K|DEF))*$")
WEEK_RE = re.compile(r"^Week (\d{1,2})\b")
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
STATUS_RE = re.compile(r"(IR-R|IR|PUP-R|PUP-P|PUP|NFI-R|NFI|SUSP|NA|Q|D|O)$")
NOTE_WORDS = ("Video Forecast", "New Player Note", "No new player Notes", "Player Note")


@dataclass
class PastRow:
    week: int
    side: str            # "left" (the team whose page it is) or "right" (its opponent)
    slot: str
    name: str
    team: str
    pos: str
    status: str
    proj: Optional[float]
    pts: Optional[float]
    game: str = ""
    team_id: str = ""     # Yahoo fantasy team (filled when the page is read from Yahoo)

    @property
    def usable(self) -> bool:
        """A real projection and a real result (bye weeks and unplayed games are left out)."""
        return self.proj is not None and self.proj > 0 and self.pts is not None and "Bye" not in self.game


def _num(s: str) -> Optional[float]:
    s = s.strip()
    return float(s) if NUM_RE.match(s) else None


def _status(name: str, label_line: str) -> str:
    rest = label_line
    for word in NOTE_WORDS:
        rest = rest.replace(word, "")
    m = STATUS_RE.search(rest)
    if not m:
        return ""
    before = rest[: m.start()].strip()
    last = name.split()[-1] if name.split() else name
    return m.group(1) if before and (before == name or before.endswith(last)) else ""


_STATUS_ONLY = re.compile(r"^(IR-R|IR|PUP-R|PUP-P|PUP|NFI-R|NFI|SUSP|NA|Q|D|O)$")


def _player(lines: list[str], tp: int) -> tuple[str, str, str, str, str]:
    """(name, team, pos, status, game) for the team-pos line at index tp.

    Above the team-pos line there are, in copy/paste text, "Name" and
    "NameQVideo ForecastNew Player Note"; in HTML flattened to lines the same
    pieces may come one per line ("Name", "Q", "Video Forecast", "New Player Note").
    """
    m = TEAM_POS_RE.match(lines[tp])
    team, pos = norm_team(m.group(1)), m.group(2)
    status, name, label = "", "", ""
    for k in range(tp - 1, max(tp - 7, -1), -1):
        line = lines[k]
        if line in NOTE_WORDS or line in ("Video", "Forecast"):
            continue
        if _STATUS_ONLY.match(line):
            status = status or line
            continue
        if any(w in line for w in NOTE_WORDS):
            label = line           # "NameQVideo ForecastNew Player Note": holds the status
            continue
        name = line
        break
    if label:
        status = status or _status(name, label)
        if not name:
            name = label
            for w in NOTE_WORDS:
                name = name.replace(w, "")
    game = lines[tp + 1] if tp + 1 < len(lines) else ""
    if game.startswith("Final") and tp + 3 < len(lines) and not _num(lines[tp + 2] or "x") and len(game) < 8:
        game = " ".join(lines[tp + 1: tp + 4])  # "Final" / "L 20-29" / "@ Atl" split in pieces
    return name, team, pos, status, game


def parse_matchup_text(text: str, week: Optional[int] = None, use_headers: bool = True) -> list[PastRow]:
    """Every player row of one or more matchup pages (weeks taken from 'Week N' headers, unless
    `use_headers` is False: a page read from Yahoo also lists every week in its week menu)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    rows: list[PastRow] = []
    current = week
    for i, line in enumerate(lines):
        w = WEEK_RE.match(line) if use_headers else None
        if w:
            current = int(w.group(1))
            continue
        if line not in SLOT_LINES or i < 2 or i + 2 >= len(lines) or current is None:
            continue
        lp, lpts, rpts, rp = lines[i - 2], lines[i - 1], lines[i + 1], lines[i + 2]
        if not all(_num(x) is not None or x in ("–", "-") for x in (lp, lpts, rpts, rp)):
            continue  # not a player row
        slot = norm_slot(line)
        # left player: its team-pos line sits just above its game line, above the two numbers
        tp = next((k for k in range(i - 3, max(i - 10, -1), -1) if TEAM_POS_RE.match(lines[k])), None)
        if tp is not None:
            name, team, pos, status, game = _player(lines, tp)
            rows.append(PastRow(current, "left", slot, name, team, pos, status, _num(lp), _num(lpts), game))
        # right player follows the numbers, unless the slot is empty
        if i + 3 < len(lines) and lines[i + 3] != "--empty--":
            tp = next((k for k in range(i + 3, min(i + 12, len(lines))) if TEAM_POS_RE.match(lines[k])), None)
            if tp is not None:
                name, team, pos, status, game = _player(lines, tp)
                rows.append(PastRow(current, "right", slot, name, team, pos, status, _num(rp), _num(rpts), game))
    return rows


def matchup_html_to_text(html: str) -> str:
    """Flatten a matchup page to one visible string per line, like a copy/paste of the page."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def dedupe(rows: Iterable[PastRow]) -> list[PastRow]:
    seen, out = set(), []
    for r in rows:
        key = (r.week, r.name, r.team, r.pos)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _text(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def _float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def from_records(records: Iterable[Mapping[str, Any]]) -> list[PastRow]:
    """Rows saved by notebook cell 7d (historial_proyecciones.csv: Yahoo's projection and the real
    points of every player of every team, read from the team pages of finished weeks)."""
    out = []
    for r in records:
        week, proj, pts = _float(r.get("semana")), _float(r.get("proy")), _float(r.get("real"))
        pos = _text(r.get("pos"))
        if week is None or not pos:
            continue
        out.append(PastRow(int(week), "", norm_slot(_text(r.get("puesto"))) or "BN", _text(r.get("jugador")),
                           norm_team(_text(r.get("nfl"))), pos, _text(r.get("estado")), proj, pts,
                           team_id=_text(r.get("team_id"))))
    return out


def read_7d_csv(path: Path) -> list[PastRow]:
    try:
        with open(path, newline="") as fh:
            return from_records(csv.DictReader(fh))
    except OSError:
        return []
