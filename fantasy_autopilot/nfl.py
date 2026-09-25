"""NFL reference data: team abbreviations, roster slots and position eligibility."""

from __future__ import annotations

import re
import unicodedata

# Canonical abbreviations (Sleeper / Yahoo style, upper case).
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona", "Cardinals"),
    "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"),
    "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"),
    "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"),
    "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"),
    "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"),
    "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"),
    "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars"),
    "KC": ("Kansas City", "Chiefs"),
    "LAC": ("Los Angeles", "Chargers"),
    "LAR": ("Los Angeles", "Rams"),
    "LV": ("Las Vegas", "Raiders"),
    "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"),
    "NE": ("New England", "Patriots"),
    "NO": ("New Orleans", "Saints"),
    "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"),
    "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"),
    "SEA": ("Seattle", "Seahawks"),
    "SF": ("San Francisco", "49ers"),
    "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"),
    "WAS": ("Washington", "Commanders"),
}

_ALIASES = {
    "WSH": "WAS",
    "JAC": "JAX",
    "LA": "LAR",
    "STL": "LAR",
    "OAK": "LV",
    "LVR": "LV",
    "SD": "LAC",
    "KAN": "KC",
    "GNB": "GB",
    "NWE": "NE",
    "NOR": "NO",
    "SFO": "SF",
    "TAM": "TB",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

# Lineup slots as Yahoo labels them. Flex slots accept several positions.
FLEX_SLOTS: dict[str, frozenset[str]] = {
    "W/R/T": frozenset({"WR", "RB", "TE"}),
    "W/R": frozenset({"WR", "RB"}),
    "W/T": frozenset({"WR", "TE"}),
    "R/T": frozenset({"RB", "TE"}),
    "Q/W/R/T": frozenset({"QB", "WR", "RB", "TE"}),
}
BASE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
NON_STARTING = frozenset({"BN", "IR"})
_SLOT_ALIASES = {
    "FLEX": "W/R/T",
    "WRT": "W/R/T",
    "SUPERFLEX": "Q/W/R/T",
    "SFLEX": "Q/W/R/T",
    "OP": "Q/W/R/T",
    "D/ST": "DEF",
    "DST": "DEF",
    "BENCH": "BN",
    "IL": "IR",
    "IR+": "IR",
}


def norm_team(abbr: str | None) -> str:
    """Normalize any team abbreviation (Yahoo 'Jax', ESPN 'WSH', ...) to canonical form."""
    if not abbr:
        return ""
    key = abbr.strip().upper()
    return _ALIASES.get(key, key)


def team_from_name(text: str | None) -> str:
    """Resolve 'Detroit', 'Lions', 'Detroit Lions' or 'DET' to 'DET' ('' if unknown)."""
    if not text:
        return ""
    t = text.strip()
    if norm_team(t) in TEAMS:
        return norm_team(t)
    low = t.lower()
    for abbr, (city, nick) in TEAMS.items():
        if low == nick.lower() or low == f"{city} {nick}".lower():
            return abbr
    # City alone is ambiguous for LA / NY teams; accept only unique cities.
    matches = [abbr for abbr, (city, _) in TEAMS.items() if low == city.lower()]
    return matches[0] if len(matches) == 1 else ""


def norm_slot(slot: str | None) -> str:
    if not slot:
        return ""
    s = re.sub(r"\s+", "", slot.strip().upper())
    return _SLOT_ALIASES.get(s, s)


def is_starting_slot(slot: str) -> bool:
    return norm_slot(slot) not in NON_STARTING


def slot_accepts(slot: str, position: str) -> bool:
    slot = norm_slot(slot)
    position = position.upper()
    if slot in FLEX_SLOTS:
        return position in FLEX_SLOTS[slot]
    return slot == position


def eligible_slots(positions: list[str] | tuple[str, ...], league_slots: list[str]) -> list[str]:
    """Starting slots of this league that a player with `positions` can fill."""
    out: list[str] = []
    for slot in league_slots:
        slot = norm_slot(slot)
        if slot in NON_STARTING or slot in out:
            continue
        if any(slot_accepts(slot, p) for p in positions):
            out.append(slot)
    return out


def is_flex(slot: str) -> bool:
    return norm_slot(slot) in FLEX_SLOTS


def norm_name(name: str | None) -> str:
    """Lower-case ASCII name without suffixes/punctuation, for fuzzy matching."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()
