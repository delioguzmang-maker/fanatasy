"""Injury status normalization and the probability that a player is active."""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from .models import Player
from .nfl import norm_name

# Order matters: more severe first when several sources disagree.
SEVERITY = ["COVID", "SUSP", "NFI", "PUP", "IR", "NA", "O", "D", "Q", ""]

_STATUS_WORDS = {
    "Q": "Q",
    "QUESTIONABLE": "Q",
    "P": "",  # old "probable" tag
    "PROBABLE": "",
    "D": "D",
    "DOUBTFUL": "D",
    "O": "O",
    "OUT": "O",
    "INACTIVE": "NA",
    "NA": "NA",
    "NOTACTIVE": "NA",
    "IR": "IR",
    "IRR": "IR",
    "IRNFI": "IR",
    "INJUREDRESERVE": "IR",
    "IL": "IR",
    "PUP": "PUP",
    "PUPR": "PUP",
    "PUPP": "PUP",
    "PHYSICALLYUNABLETOPERFORM": "PUP",
    "NFI": "NFI",
    "NFIR": "NFI",
    "NFIA": "NFI",
    "NONFOOTBALLINJURY": "NFI",
    "SUSP": "SUSP",
    "SUS": "SUSP",
    "SUSPENDED": "SUSP",
    "COV": "COVID",
    "COVID": "COVID",
    "COVID19": "COVID",
    "DTD": "Q",
    "GTD": "Q",
    "ACTIVE": "",
    "HEALTHY": "",
}

# Defaults based on public base rates since 2016 (no "probable" tag); all configurable.
DEFAULT_PROBS: dict[str, float] = {
    "": 1.0,
    "Q": 0.75,
    "D": 0.10,
    "O": 0.0,
    "NA": 0.0,
    "IR": 0.0,
    "PUP": 0.0,
    "NFI": 0.0,
    "SUSP": 0.0,
    "COVID": 0.0,
}
DEFAULT_PRACTICE_PROBS: dict[str, dict[str, float]] = {
    "Q": {"FULL": 0.90, "LIMITED": 0.75, "DNP": 0.40},
    "D": {"FULL": 0.25, "LIMITED": 0.15, "DNP": 0.05},
}


def norm_status(raw: Optional[str]) -> str:
    """Map Yahoo / Sleeper / ESPN wording ('Questionable', 'IR-R', 'Sus', 'O') to a code."""
    if not raw:
        return ""
    key = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if key in _STATUS_WORDS:
        return _STATUS_WORDS[key]
    for word, code in _STATUS_WORDS.items():
        if len(word) > 3 and key.startswith(word):
            return code
    return ""


def norm_practice(raw: Optional[str]) -> str:
    if not raw:
        return ""
    k = re.sub(r"[^A-Z]", "", raw.upper())
    if k.startswith("FULL") or k == "FP":
        return "FULL"
    if k.startswith("LIM") or k == "LP":
        return "LIMITED"
    if k.startswith("DNP") or k.startswith("DIDNOT") or k == "OUT":
        return "DNP"
    return ""


def most_severe(statuses: Iterable[str]) -> str:
    present = {s for s in statuses if s is not None}
    for code in SEVERITY:
        if code in present:
            return code
    return ""


def is_out(status: str) -> bool:
    return DEFAULT_PROBS.get(status, 1.0) == 0.0


def play_probability(
    status: str,
    practice: str = "",
    probs: Optional[Mapping[str, float]] = None,
    practice_probs: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> float:
    probs = probs or DEFAULT_PROBS
    practice_probs = practice_probs or DEFAULT_PRACTICE_PROBS
    base = probs.get(status, DEFAULT_PROBS.get(status, 1.0))
    by_practice = practice_probs.get(status, {})
    return float(by_practice.get(norm_practice(practice), base))


def apply_status_model(
    players: Iterable[Player],
    overrides: Optional[Mapping[str, float]] = None,
    probs: Optional[Mapping[str, float]] = None,
    practice_probs: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> None:
    """Fill `p_play` for every player. Overrides are keyed by name or Yahoo id."""
    over = {norm_name(k): float(v) for k, v in (overrides or {}).items()}
    over.update({k: float(v) for k, v in (overrides or {}).items() if k.isdigit()})
    for p in players:
        if p.on_bye:
            p.p_play = 0.0
            continue
        manual = over.get(p.pid, over.get(norm_name(p.name)))
        if manual is not None:
            p.p_play = max(0.0, min(1.0, manual))
            p.notes.append(f"probabilidad manual {p.p_play:.0%}")
            continue
        p.p_play = play_probability(p.status, p.practice, probs, practice_probs)
