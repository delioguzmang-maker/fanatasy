"""Settings: one flat object, loadable from a dict, a JSON file or environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

MODES = ("sugerir", "simulacro", "automatico")


@dataclass
class Config:
    # league
    league_id: str = ""
    team_id: str = ""
    week: Optional[int] = None            # None = current Yahoo week
    season: Optional[int] = None          # None = from Sleeper
    # what the autopilot may do: "sugerir" only notifies, "simulacro" rehearses the
    # writes without saving, "automatico" saves lineups (and claims if auto_claims)
    mode: str = "sugerir"
    auto_claims: bool = False
    writer: str = "auto"                  # auto (HTTP, then browser) | http | browser
    # lineup
    min_gain: float = 0.3                 # expected points needed to change a weekly lineup
    gameday_min_gain: float = 0.5
    hedge_reliability: float = 0.85
    hedge_reliability_sugerir: float = 0.5   # in "sugerir" a person makes the game-day swap: count it at half
    uncertain_penalty: float = 0.75
    assume_autoswap: bool = True          # the game-day loop / GitHub Action is running
    p_play_overrides: dict[str, float] = field(default_factory=dict)   # {"Puka Nacua": 0.3}
    status_probs: dict[str, float] = field(default_factory=dict)       # {"Q": 0.7}
    pins_start: list[str] = field(default_factory=list)
    pins_bench: list[str] = field(default_factory=list)
    use_sleeper: bool = True
    use_espn: bool = True
    projections: str = "yahoo"            # yahoo | sleeper | blend
    blend_weight: float = 0.5             # Sleeper share when projections == "blend"
    blend_weight_pos: dict[str, float] = field(default_factory=dict)  # per position (cell 7e); missing = 0
    blend_scale_pos: dict[str, float] = field(default_factory=dict)   # Sleeper points -> league scoring
    # waivers / FAAB
    faab_budget: int = 100
    faab_balance: Optional[int] = None    # None = read from Yahoo's Teams page
    full_budget_ppw: float = 8.0
    max_bid_pct: float = 0.5
    min_bid: int = 0
    max_claims: int = 2
    min_claim_gain: float = 8.0
    max_bid_kdef: int = 2
    never_drop: list[str] = field(default_factory=list)
    last_week: int = 17
    discount: float = 0.92
    candidates_per_position: int = 12    # RB/WR/TE/QB free agents read per position
    candidates_kdef: int = 5             # kickers and defenses (streamable, fewer pages)
    bid_history_cap: bool = True         # cap bids near what this league actually pays
    use_trending: bool = True
    # role alerts (an active starter who lost his job: the Skattebo case)
    role_check: bool = True
    role_penalty: float = 0.5            # projection multiplier with fresh role news
    role_penalty_undated: float = 0.75   # role news without a readable date
    role_penalty_confirmed: float = 0.3  # fresh role news AND his touches already fell last game
    role_news_hours: float = 120.0       # older news is ignored
    role_auto_swap: bool = False         # True: the lineup itself counts the penalty (automatico)
    # alerts
    inactives_posted_minutes: int = 60   # this close to kickoff a Q/D player not ruled out is active
    active_q_factor: float = 0.95        # value of a Q player known to be active (cell 7d measures it)
    resend_minutes: int = 30             # repeat an urgent game-day alert nobody acted on
    max_resends: int = 4
    # win probability, not only points
    win_prob_lineup: bool = True         # suggest riskier/safer swaps when they raise the chance to win
    max_sacrifice: float = 1.5           # expected points you may give up for a better chance to win
    player_sd: dict[str, float] = field(default_factory=dict)  # weekly SD by name or id (cell 7d)
    fa_rescue: bool = True               # starter out and no backup: suggest a free agent
    log_status: bool = True              # keep estado_log.csv to calibrate the Questionable discount
    # notifications
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    email_to: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    # runtime
    yahoo_base_url: str = "https://football.fantasysports.yahoo.com"
    quiet: bool = False                   # print only titles (e.g. public GitHub Actions logs)
    state_dir: str = ".fantasy_autopilot"
    save_evidence: bool = True
    timezone: str = "America/New_York"
    request_delay: float = 1.0
    headless: bool = True
    chromium_path: str = ""

    def __post_init__(self) -> None:
        self.mode = (self.mode or "sugerir").strip().lower().replace("á", "a").replace("ó", "o")
        if self.mode not in MODES:
            raise ValueError(f"mode debe ser uno de {MODES}, no {self.mode!r}")
        self.league_id, self.team_id = str(self.league_id).strip(), str(self.team_id).strip()

    # ------------------------------------------------------------ loading
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"opciones desconocidas: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @classmethod
    def from_env(cls, base: Optional["Config"] = None) -> "Config":
        """FA_CONFIG_JSON (a whole JSON) plus FA_<FIELD> overrides, e.g. FA_LEAGUE_ID=123."""
        data = asdict(base) if base else {}
        if os.environ.get("FA_CONFIG_JSON"):
            data.update(json.loads(os.environ["FA_CONFIG_JSON"]))
        for f in fields(cls):
            raw = os.environ.get(f"FA_{f.name.upper()}")
            if raw is None or raw == "":
                continue
            data[f.name] = _coerce(raw, f.type)
        aliases = {"NTFY_TOPIC": "ntfy_topic", "TELEGRAM_TOKEN": "telegram_token", "TELEGRAM_CHAT_ID": "telegram_chat_id"}
        for env, name in aliases.items():
            if os.environ.get(env) and not data.get(name):
                data[name] = os.environ[env]
        return cls.from_dict(data)

    def to_dict(self, redact: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if redact:
            for key in ("telegram_token", "smtp_password"):
                if data.get(key):
                    data[key] = "***"
        return data

    # ----------------------------------------------------------- derived
    @property
    def writes_enabled(self) -> bool:
        return self.mode == "automatico"

    @property
    def rehearsal(self) -> bool:
        return self.mode == "simulacro"

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


def _coerce(raw: str, typ: Any) -> Any:
    t = str(typ)
    if "bool" in t:
        return raw.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
    if "dict" in t or "list" in t:
        text = raw.strip()
        if text.startswith(("{", "[")):
            return json.loads(text)
        return [x.strip() for x in text.split(",") if x.strip()]
    if t in ("int", "Optional[int]"):
        return int(raw)
    if "float" in t:
        return float(raw)
    return raw
