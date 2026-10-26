"""Own projections, built to beat Yahoo's and checked against games already played.

Start: Sleeper's projection converted to the league's scoring (for example 6-point
passing touchdowns and -2 per interception; see ``sleeper.league_points``). On top
of it, a ridge regression learns what that projection misses. It is trained on
every NFL player-week since last season (about 9,000 games), using only what was
known before each kickoff:

* how the player has been doing against his projections (recent weeks weigh more),
* his snap share, targets and carries compared with what the projection assumes,
* how many points his opponent has been allowing to his position,
* the betting line: points the market expects his team and the opponent to score.

Exam (``exam``): every week of this season is predicted with a model trained only
on earlier weeks, then compared with Sleeper alone (every NFL player) and with
Yahoo (the players of your league, from the matchup pages: ``history.py``).

What it cannot do: one game is mostly luck. Even knowing every player's real
season average (impossible before the season ends) misses by about 5 points per
player per game; the best projections are a few tenths of a point better than the
rest, not several points.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from .nfl import norm_name, norm_team
from .sources.sleeper import league_points, row_key

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
HALF_LIFE = 2.0           # weeks: last week counts double the one three weeks ago... roughly
RIDGE = 50.0              # how hard the corrections are pulled toward zero
AVG_TEAM_POINTS = 22.5    # league-average implied team total
MIN_PROJ = 0.5            # rows with a smaller projection are ignored (not real candidates)


@dataclass
class Obs:
    season: int
    week: int
    pos: str
    key: str
    team: str
    opp: str
    sp: float                       # Sleeper projection, league scoring
    sp_tgt: float = 0.0
    sp_att: float = 0.0
    actual: Optional[float] = None  # league points (None = not played yet)
    played: bool = False
    snp: float = 0.0
    tm_snp: float = 0.0
    tgt: float = 0.0
    att: float = 0.0
    imp: Optional[float] = None     # implied points of his team (betting line)
    imp_opp: Optional[float] = None
    f: dict[str, float] = field(default_factory=dict)


def player_key(pos: str, name: str, team: str = "") -> tuple[str, str]:
    """Key shared by Yahoo players and Sleeper rows: (position, normalized name); defenses by team."""
    return (pos, norm_team(team)) if pos == "DEF" else (pos, norm_name(name))


def observations(
    proj_rows: Iterable[dict[str, Any]],
    stat_rows: Optional[Iterable[dict[str, Any]]],
    season: int,
    week: int,
    reception_points: float = 0.5,
    bonus: Optional[dict[str, float]] = None,
    implied: Optional[dict[tuple[int, int, str], float]] = None,
    final_teams: Optional[set[str]] = None,
) -> list[Obs]:
    """One week of player rows. `stat_rows` None = not played yet. `final_teams` limits the
    real points to teams whose game is over (a week still in progress)."""
    stats = {row_key(r): r for r in stat_rows or ()}
    implied = implied or {}
    out = []
    for r in proj_rows:
        pos, key = row_key(r)
        if pos not in POSITIONS or not key:
            continue
        ps = r.get("stats") or {}
        sp = league_points(ps, reception_points, bonus)
        if sp is None or sp < MIN_PROJ:
            continue
        team, opp = norm_team(r.get("team") or ""), norm_team(r.get("opponent") or "")
        o = Obs(season, week, pos, key, team, opp, sp,
                sp_tgt=float(ps.get("rec_tgt") or 0), sp_att=float(ps.get("rush_att") or 0),
                imp=implied.get((season, week, team)), imp_opp=implied.get((season, week, opp)))
        s = stats.get((pos, key))
        if stat_rows is not None and (final_teams is None or team in final_teams):
            st = (s or {}).get("stats") or {}
            o.played = bool(s) and bool(st.get("gp") or st.get("gms_active") or st.get("off_snp")
                                        or pos in ("K", "DEF"))
            o.actual = (league_points(st, reception_points, bonus) or 0.0) if s else 0.0
            o.snp, o.tm_snp = float(st.get("off_snp") or 0), float(st.get("tm_off_snp") or 0)
            o.tgt, o.att = float(st.get("rec_tgt") or 0), float(st.get("rush_att") or 0)
        out.append(o)
    return out


def _ewm(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    w = [0.5 ** ((len(values) - 1 - i) / HALF_LIFE) for i in range(len(values))]
    return float(np.dot(w, values) / sum(w))


def add_features(obs: list[Obs]) -> None:
    """Features from earlier weeks of the same season only (roles change between seasons)."""
    obs.sort(key=lambda o: (o.season, o.week))
    hist: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    opp_sum: dict[tuple, float] = defaultdict(float)     # (season, week, opp, pos) -> misses allowed
    team_sum: dict[tuple, float] = defaultdict(float)    # (season, week, team) -> misses of the offense
    for o in obs:
        if o.played and o.actual is not None:
            opp_sum[(o.season, o.week, o.opp, o.pos)] += o.actual - o.sp
            team_sum[(o.season, o.week, o.team)] += o.actual - o.sp
    opp_weeks: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
    for (s, w, opp, pos), v in opp_sum.items():
        opp_weeks[(s, opp, pos)].append((w, v))
    team_weeks: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
    for (s, w, team), v in team_sum.items():
        team_weeks[(s, team)].append((w, v))

    def before(series: list[tuple[int, float]], week: int) -> Optional[float]:
        vals = [v for w, v in series if w < week]
        return float(np.mean(vals)) if vals else None

    by_week: dict[tuple[int, int], list[Obs]] = defaultdict(list)
    for o in obs:
        by_week[(o.season, o.week)].append(o)
    for sw in sorted(by_week):
        for o in by_week[sw]:   # features first (only earlier weeks are in `hist`) ...
            h = hist[(o.season, o.pos, o.key)]
            f: dict[str, Optional[float]] = {}
            for name in ("resid", "act", "share", "tgt", "att"):
                vals = h[name]
                f[f"{name}_mean"] = float(np.mean(vals)) if vals else None
                f[f"{name}_last"] = vals[-1] if vals else None
                f[f"{name}_ewm"] = _ewm(vals)
            f["n_prev"] = float(len(h["resid"]))
            f["tgt_vs_proj"] = None if f["tgt_ewm"] is None else f["tgt_ewm"] - o.sp_tgt
            f["att_vs_proj"] = None if f["att_ewm"] is None else f["att_ewm"] - o.sp_att
            f["opp_allow"] = before(opp_weeks.get((o.season, o.opp, o.pos), []), o.week)
            f["team_form"] = before(team_weeks.get((o.season, o.team), []), o.week)
            f["imp_c"] = None if o.imp is None else o.imp - AVG_TEAM_POINTS
            f["imp_opp_c"] = None if o.imp_opp is None else o.imp_opp - AVG_TEAM_POINTS
            o.f = {k: (0.0 if v is None else float(v)) for k, v in f.items()}
        for o in by_week[sw]:   # ... then this week joins the history
            if o.played and o.actual is not None:
                h = hist[(o.season, o.pos, o.key)]
                h["resid"].append(o.actual - o.sp)
                h["act"].append(o.actual)
                if o.tm_snp > 0:
                    h["share"].append(o.snp / o.tm_snp)
                h["tgt"].append(o.tgt)
                h["att"].append(o.att)


BASE = ["resid_mean", "resid_last", "resid_ewm", "act_mean", "act_ewm", "share_mean", "share_last", "share_ewm",
        "tgt_vs_proj", "att_vs_proj", "n_prev", "opp_allow", "team_form", "imp_c", "imp_opp_c"]


def design(o: Obs) -> list[float]:
    f = o.f
    row = [o.sp] + [f.get(k, 0.0) for k in BASE]
    for p in POSITIONS:
        on = 1.0 if o.pos == p else 0.0
        row += [on, on * f.get("resid_ewm", 0.0), on * o.sp, on * f.get("opp_allow", 0.0),
                on * f.get("imp_c", 0.0), on * f.get("imp_c", 0.0) * o.sp / 10]
    return row


@dataclass
class Ridge:
    mu: np.ndarray
    sd: np.ndarray
    beta: np.ndarray

    def correction(self, o: Obs) -> float:
        z = (np.array(design(o)) - self.mu) / self.sd
        return float(self.beta[0] + z @ self.beta[1:])

    def predict(self, o: Obs) -> float:
        return max(0.0, o.sp + self.correction(o))


def fit(train: Sequence[Obs], ridge: float = RIDGE) -> Optional[Ridge]:
    rows = [o for o in train if o.played and o.actual is not None]
    if len(rows) < 200:
        return None
    X = np.array([design(o) for o in rows])
    y = np.array([o.actual - o.sp for o in rows])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = np.c_[np.ones(len(X)), (X - mu) / sd]
    pen = ridge * np.eye(Z.shape[1])
    pen[0, 0] = 0.0
    beta = np.linalg.solve(Z.T @ Z + pen, Z.T @ y)
    return Ridge(mu, sd, beta)


@dataclass
class WeekExam:
    week: int
    n_nfl: int
    mae_sleeper: float
    mae_own: float
    n_league: int = 0
    mae_yahoo: Optional[float] = None
    mae_own_league: Optional[float] = None


@dataclass
class ExamResult:
    weeks: list[WeekExam]
    pairs_yahoo: Optional[float] = None    # share of same-position pairs ordered right (start/sit)
    pairs_own: Optional[float] = None
    pairs_n: int = 0
    rows: list[tuple[Any, float]] = field(default_factory=list)   # (league row, own projection)

    def _mean(self, attr: str) -> Optional[float]:
        vals = [getattr(w, attr) for w in self.weeks if getattr(w, attr) is not None]
        return float(np.mean(vals)) if vals else None

    @property
    def yahoo(self) -> Optional[float]:
        return self._mean("mae_yahoo")

    @property
    def own_league(self) -> Optional[float]:
        return self._mean("mae_own_league")

    @property
    def sleeper(self) -> Optional[float]:
        return self._mean("mae_sleeper")

    @property
    def own(self) -> Optional[float]:
        return self._mean("mae_own")

    @property
    def beats_yahoo(self) -> Optional[bool]:
        if self.yahoo is None or self.own_league is None:
            return None
        return self.own_league < self.yahoo


def pair_accuracy(groups: Iterable[Sequence[tuple[float, float]]]) -> tuple[float, int]:
    """Share of pairs (same position, same week) where the higher projection scored more."""
    ok = n = 0
    for g in groups:
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g[i][1] == g[j][1]:
                    continue
                n += 1
                ok += (g[i][0] - g[j][0]) * (g[i][1] - g[j][1]) > 0
    return (ok / n if n else 0.0), n


class OwnProjections:
    """Loads Sleeper's weeks, builds features, trains, predicts and takes the exam."""

    def __init__(self, sleeper: Any, season: int, reception_points: float = 0.5,
                 bonus: Optional[dict[str, float]] = None,
                 implied: Optional[dict[tuple[int, int, str], float]] = None,
                 past_seasons: int = 1, weeks_per_season: int = 18,
                 positions: Sequence[str] = ("QB", "RB", "WR", "TE")):
        self.sleeper = sleeper
        self.season = int(season)
        self.rec = reception_points
        self.bonus = bonus or {}
        self.implied = implied or {}
        self.past_seasons = past_seasons
        self.weeks_per_season = weeks_per_season
        self.positions = tuple(positions)   # the rest keep Yahoo's projection (exam included)
        self.obs: list[Obs] = []
        self.loaded_through: Optional[int] = None

    def load(self, target_week: int, final_teams: Optional[dict[int, set[str]]] = None) -> list[Obs]:
        """Earlier seasons + this season up to `target_week` (whose games have not been played)."""
        obs: list[Obs] = []
        final_teams = final_teams or {}
        for s in range(self.season - self.past_seasons, self.season):
            for w in range(1, self.weeks_per_season + 1):
                try:
                    proj = self.sleeper.rows("projections", s, w, final=True)
                    stats = self.sleeper.rows("stats", s, w, final=True)
                except Exception:  # noqa: BLE001 - an older season is a bonus, not a requirement
                    break
                if not proj:
                    break
                obs += observations(proj, stats, s, w, self.rec, self.bonus, self.implied)
        for w in range(1, target_week + 1):
            done = w < target_week - 1 or (w == target_week - 1 and w not in final_teams)
            proj = self.sleeper.rows("projections", self.season, w, final=done)
            stats = self.sleeper.rows("stats", self.season, w, final=done) if w < target_week else None
            obs += observations(proj, stats, self.season, w, self.rec, self.bonus, self.implied,
                                final_teams=final_teams.get(w))
        add_features(obs)
        self.obs, self.loaded_through = obs, target_week
        return obs

    def _split(self, week: int) -> tuple[list[Obs], list[Obs]]:
        train = [o for o in self.obs if (o.season, o.week) < (self.season, week)]
        test = [o for o in self.obs if o.season == self.season and o.week == week]
        return train, test

    def project(self, week: int) -> dict[tuple[str, str], float]:
        """{(pos, key): own projection} for every player Sleeper projects in `week`."""
        if self.loaded_through is None or self.loaded_through < week:
            self.load(week)
        train, test = self._split(week)
        model = fit(train)
        if model is None:
            return {(o.pos, o.key): o.sp for o in test}
        return {(o.pos, o.key): round(model.predict(o), 2) for o in test}

    def exam(self, through_week: int, league_rows: Sequence[Any] = ()) -> ExamResult:
        """Walk-forward over this season's finished weeks (1..through_week). `league_rows` are
        history.PastRow with Yahoo's projection and the real points in your league."""
        if self.loaded_through is None or self.loaded_through < through_week:
            self.load(through_week + 1)
        by_week: dict[int, list[Any]] = defaultdict(list)
        for r in league_rows:
            if r.usable:
                by_week[r.week].append(r)
        weeks, pairs_y, pairs_o, rows = [], defaultdict(list), defaultdict(list), []
        for w in range(1, through_week + 1):
            train, test = self._split(w)
            test = [o for o in test if o.played and o.actual is not None]
            model = fit(train)
            if not test or model is None:
                continue
            pred = {(o.pos, o.key): model.predict(o) for o in test}
            we = WeekExam(w, len(test), float(np.mean([abs(o.sp - o.actual) for o in test])),
                          float(np.mean([abs(pred[(o.pos, o.key)] - o.actual) for o in test])))
            league = [(r, pred.get(player_key(r.pos, r.name, r.team)) if r.pos in self.positions else r.proj)
                      for r in by_week.get(w, [])]
            league = [(r, p if p is not None else r.proj) for r, p in league]
            if league:
                we.n_league = len(league)
                we.mae_yahoo = float(np.mean([abs(r.proj - r.pts) for r, _ in league]))
                we.mae_own_league = float(np.mean([abs(p - r.pts) for r, p in league]))
                for r, p in league:
                    rows.append((r, p))
                    pairs_y[(w, r.pos)].append((r.proj, r.pts))
                    pairs_o[(w, r.pos)].append((p, r.pts))
            weeks.append(we)
        out = ExamResult(weeks, rows=rows)
        if pairs_y:
            out.pairs_yahoo, out.pairs_n = pair_accuracy(pairs_y.values())
            out.pairs_own, _ = pair_accuracy(pairs_o.values())
        return out
