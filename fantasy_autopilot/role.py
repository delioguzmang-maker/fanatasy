"""Role alerts: an *active* starter who lost his job (the Skattebo case, week 6).

Inactive lists only tell you who is out. A player can be active and still get
zero touches (benched for fumbles, demoted, a committee). Yahoo's player note
carries the latest news; this module reads it, keeps only *recent* news (by
the date printed next to it), and looks for role words.

Two kinds of words, on purpose:

* ROLE words (benched, demoted, committee...) cut the projection by
  ``penalty`` because the status model knows nothing about them.
* INJURY words (doubtful, ruled out, game-time decision...) are NOT used here:
  the injury status already discounts those players. Counting them twice was
  the "double discount" bug.

Keyword matching gives false alarms, so by default the alert only notifies;
it changes the lineup by itself only when ``role_auto_swap`` is on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

ROLE_BAD = [
    "downgraded", "downgrade", "demoted", "demotion", "benched", "reduced role", "smaller role",
    "lesser role", "fewer snaps", "fewer touches", "limited snaps", "committee", "split work",
    "split carries", "lose work", "losing work", "lost his job", "lose his job", "fumble",
    "not start", "won't start", "will not start", "pitch count", "backup role", "no longer the starter",
    "out of the rotation", "healthy scratch", "snap share dropped", "phased out",
]
ROLE_GOOD = [
    "expected to start", "will start", "larger role", "bigger role", "lead back", "workhorse",
    "starting role", "every-down", "bell cow", "increased role", "more work", "promoted",
    "named the starter", "take over",
]
# Already counted by the injury status model; listed so they are never penalized twice.
INJURY_WORDS = [
    "doubtful", "questionable", "ruled out", "inactive", "game-time decision", "not expected to play",
    "will not play", "won't play", "injured reserve", "did not practice", "limited practice",
]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_RES = [
    # "3 hours ago", "45 minutes ago", "2 days ago"
    (re.compile(r"\b(\d{1,3})\s*(minute|min|hour|hr|day)s?\s+ago\b", re.I), "ago"),
    (re.compile(r"\b(today|yesterday)\b", re.I), "word"),
    # "Oct 18, 2026", "October 18", "Sun, Oct 18 9:53 AM"
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
                r"(?:,?\s+(\d{4}))?", re.I), "month"),
    # "10/18/2026", "10/18"
    (re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"), "slash"),
]


@dataclass
class NewsSentence:
    text: str
    when: Optional[datetime]   # None = no date found near it


@dataclass
class RoleSignal:
    bad: list[NewsSentence] = field(default_factory=list)
    good: list[NewsSentence] = field(default_factory=list)
    old_ignored: int = 0       # role sentences dropped because their date is too old
    dated: bool = False        # the note had at least one readable date

    @property
    def fresh_bad(self) -> list[NewsSentence]:
        return [s for s in self.bad if s.when is not None]

    @property
    def undated_bad(self) -> list[NewsSentence]:
        return [s for s in self.bad if s.when is None]


@dataclass
class RoleAlert:
    player_pid: str
    name: str
    slot: str
    proj: float
    factor: float              # multiplier applied to the projection
    sentences: list[str]
    undated: bool
    swap_pid: str = ""
    swap_name: str = ""
    swap_proj: float = 0.0
    usage_note: str = ""       # last game's touches against his usual (Sleeper stats)

    @property
    def effective(self) -> float:
        return self.proj * self.factor

    @property
    def gain(self) -> float:
        return self.swap_proj - self.effective if self.swap_pid else 0.0

    def describe(self, with_swap: bool = True) -> str:
        when = "noticia sin fecha, confírmala" if self.undated else "noticia reciente"
        base = (f"{self.name} ({self.slot}) puede haber perdido el puesto ({when}): "
                f"«{' | '.join(self.sentences)[:220]}». "
                f"Proyección {self.proj:.2f} × {self.factor:g} = {self.effective:.2f}.")
        if self.usage_note:
            base += f" {self.usage_note}."
        if not self.sentences:
            base = (f"{self.name} ({self.slot}): {self.usage_note}. Sin noticia de rol: Yahoo ya suele bajar "
                    f"su proyección ({self.proj:.2f}); revisa antes de dejarlo de titular.")
            return base
        if not with_swap:
            return base
        if self.swap_pid:
            base += (f" CAMBIA por {self.swap_name}: {self.swap_proj:.2f} - {self.effective:.2f} = "
                     f"{self.gain:+.2f}.")
        else:
            base += " Ningún suplente le gana aun contándolo así: déjalo."
        return base


def usage_drop(rec: Optional[tuple], min_base: float = 6.0, ratio: float = 0.4) -> str:
    """(last game touches, usual touches) -> a Spanish note when the last game fell below 40%."""
    if not rec:
        return ""
    last, base = rec
    if base is None or base < min_base or last is None or last >= ratio * base:
        return ""
    return f"en su último partido tuvo {last:.0f} toques (acarreos + pases a él) contra {base:.1f} de costumbre"


def note_text(payload: str) -> str:
    """Readable text of a Yahoo player note, without the weekly projections table."""
    try:
        html = json.loads(payload).get("content", "")
    except (ValueError, AttributeError):
        html = payload
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup.select("table"):
        t.decompose()
    return " ".join(soup.get_text(" ").split())


def _find_dates(text: str, now: datetime, tz: ZoneInfo) -> list[tuple[int, datetime]]:
    """(character position, datetime UTC) for every date-like token in the text."""
    local = now.astimezone(tz)
    found = []
    for rx, kind in _DATE_RES:
        for m in rx.finditer(text):
            try:
                if kind == "ago":
                    n, unit = int(m.group(1)), m.group(2).lower()
                    delta = (timedelta(minutes=n) if unit.startswith("min") else
                             timedelta(hours=n) if unit.startswith("h") else timedelta(days=n))
                    when = now - delta
                elif kind == "word":
                    days = 0 if m.group(1).lower() == "today" else 1
                    d = local.date() - timedelta(days=days)
                    when = datetime(d.year, d.month, d.day, 12, tzinfo=tz).astimezone(timezone.utc)
                elif kind == "month":
                    month = _MONTHS[m.group(1).lower()[:3]]
                    day = int(m.group(2))
                    year = int(m.group(3)) if m.group(3) else local.year
                    when = datetime(year, month, day, 12, tzinfo=tz)
                    if not m.group(3) and when > local + timedelta(days=2):
                        when = when.replace(year=year - 1)  # "Dec 30" read in January
                    when = when.astimezone(timezone.utc)
                else:
                    month, day = int(m.group(1)), int(m.group(2))
                    if not (1 <= month <= 12 and 1 <= day <= 31):
                        continue
                    year = int(m.group(3)) if m.group(3) else local.year
                    year = year + 2000 if year < 100 else year
                    when = datetime(year, month, day, 12, tzinfo=tz)
                    if not m.group(3) and when > local + timedelta(days=2):
                        when = when.replace(year=year - 1)
                    when = when.astimezone(timezone.utc)
            except (ValueError, KeyError):
                continue
            found.append((m.start(), when))
    return sorted(found)


def _sentences(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in re.split(r"(?<=[.!?])\s+", text):
        start = text.find(part, pos)
        out.append((max(start, 0), part))
        pos = max(start, 0) + len(part)
    return out


def role_signal(text: str, now: datetime, tz: ZoneInfo, max_age_hours: float = 120.0) -> RoleSignal:
    """Role sentences in a note, each with the nearest date. Old ones are dropped."""
    dates = _find_dates(text, now, tz)
    sig = RoleSignal(dated=bool(dates))
    cutoff = now - timedelta(hours=max_age_hours)
    for start, sentence in _sentences(text):
        low = sentence.lower()
        bad = any(w in low for w in ROLE_BAD)
        good = any(w in low for w in ROLE_GOOD)
        if not (bad or good):
            continue
        when = None
        if dates:
            mid = start + len(sentence) / 2
            when = min(dates, key=lambda d: abs(d[0] - mid))[1]
            if when < cutoff:
                sig.old_ignored += 1
                continue
        item = NewsSentence(sentence.strip(), when)
        (sig.bad if bad else sig.good).append(item)
    return sig


def check_roster(
    players: Sequence,
    fetch_text,
    now: datetime,
    tz: ZoneInfo,
    penalty: float = 0.5,
    penalty_undated: float = 0.75,
    max_age_hours: float = 120.0,
    with_swap: bool = True,
    usage: Optional[dict] = None,
    penalty_confirmed: float = 0.3,
) -> tuple[list[RoleAlert], list[str], list[str]]:
    """Alerts for unlocked starters with fresh role news, each with the best bench swap.

    `fetch_text(pid) -> str` returns the note text (cached by the caller).
    Returns (alerts, good-news lines for bench players, failures)."""
    starters = [p for p in players if p.is_starter and not p.locked]
    bench = [p for p in players if p.slot == "BN" and not p.locked and p.prob > 0 and not p.on_bye]
    alerts, good, failures = [], [], []
    bench_ids = {id(b) for b in bench}
    for p in starters + bench:
        try:
            sig = role_signal(fetch_text(p.pid), now, tz, max_age_hours)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{p.name}: {str(exc)[:60]}")
            continue
        if id(p) in bench_ids:
            if sig.good:
                good.append(f"{p.name} (banca): señal buena «{sig.good[0].text[:160]}»")
            continue
        drop = usage_drop((usage or {}).get(p.pid))
        if not sig.bad:
            if drop:  # usage fell but no news: warn only (Yahoo usually lowers the projection already)
                alerts.append(RoleAlert(p.pid, p.name, p.slot, p.expected, 1.0, [], False, usage_note=drop))
            continue
        undated = not sig.fresh_bad
        factor = penalty_undated if undated else penalty
        if drop and not undated:
            factor = penalty_confirmed  # news AND his touches already fell: the role loss is real
        lines = [s.text for s in (sig.fresh_bad or sig.undated_bad)][:2]
        alert = RoleAlert(p.pid, p.name, p.slot, p.expected, factor, lines, undated, usage_note=drop or "")
        alerts.append(alert)
        if not with_swap:
            continue
        options = [b for b in bench if b.can_fill(p.slot)
                   and (b.kickoff is None or p.kickoff is None or b.kickoff >= p.kickoff)]
        best = max(options, key=lambda b: b.expected, default=None)
        if best is not None and best.expected > alert.effective:
            alert.swap_pid, alert.swap_name, alert.swap_proj = best.pid, best.name, best.expected
    return alerts, good, failures
