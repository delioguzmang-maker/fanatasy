"""Who will add (and bid on) a player at each position next week, learned from the league.

The old rule ("a team with a bye hole bids at that position") caught very few real
adds: most adds are kickers and defenses streamed every week, managers who add
something every week whatever their roster, injuries, and positions producing
little. This model puts all of that in one logistic regression per
team-week-position:

* holes from byes next week, injured players, depth at the position,
* how many points the team's starters there scored last week and are projected for,
* the team's add rate (any position and that position, shrunk toward the league),
* the league's rate for the position (K/DEF are streamed) and whether the team
  added at the position the week before.

Exam: every week is predicted with a model trained only on earlier weeks, and
compared with the old rule on the same cases (how many real adds each anticipated,
and how often its "will add" was right).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
OUT_TAGS = ("O", "IR", "IR-R", "PUP", "PUP-R", "NFI", "NFI-R", "SUSP", "D")
FEATURES = ["bye_short", "hurt", "depth", "starter_pts", "starter_proj", "team_rate", "team_pos_rate",
            "league_pos_rate", "added_last_week", "is_K", "is_DEF"]


@dataclass
class Case:
    team_id: str
    week: int          # the week the add is for (waivers before that week's games)
    pos: str
    x: dict[str, float]
    added: Optional[bool] = None   # None = the future


def build_cases(
    roster_rows: Iterable[Mapping],          # one per player and week: team_id, week, pos, team, slot, proj, pts, status
    adds: Iterable[Mapping],                 # team_id, semana (week), pos
    byes: Mapping[int, set[str]],            # week -> NFL teams on bye
    starters: Mapping[str, int],             # pos -> starting slots (flex not counted)
    weeks: Sequence[int],                    # weeks to build cases for (the add week)
    label_through: int,                      # last week whose waivers already ran (later = future)
    shrink: float = 2.0,
) -> list[Case]:
    roster: dict[tuple[str, int], list[Mapping]] = defaultdict(list)
    for r in roster_rows:
        roster[(str(r["team_id"]), int(r["week"]))].append(r)
    added: dict[tuple[str, int, str], int] = defaultdict(int)
    for a in adds:
        try:
            added[(str(a["team_id"]), int(a["semana"]), str(a["pos"]))] += 1
        except (KeyError, TypeError, ValueError):
            continue
    teams = sorted({t for t, _ in roster})
    out = []
    for w in weeks:
        prev = w - 1
        team_weeks = max(1, prev)          # weeks before w in which adds could happen
        league_pos = {p: sum(added[(t, k, p)] > 0 for t in teams for k in range(1, w)) / max(1, len(teams) * team_weeks)
                      for p in POSITIONS}
        # league level of starters' points and projections at each position last week
        lvl_pts, lvl_proj = defaultdict(list), defaultdict(list)
        for t in teams:
            for r in roster.get((t, prev), []):
                if r.get("slot") not in ("BN", "IR") and r.get("pos") in POSITIONS:
                    lvl_pts[r["pos"]].append(float(r.get("pts") or 0))
                    lvl_proj[r["pos"]].append(float(r.get("proj") or 0))
        for t in teams:
            rows = roster.get((t, prev), [])
            if not rows:
                continue
            team_rate = sum(any(added[(t, k, p)] for p in POSITIONS) for k in range(1, w)) / team_weeks
            for pos in POSITIONS:
                at = [r for r in rows if r.get("pos") == pos and r.get("slot") != "IR"]
                healthy = [r for r in at if str(r.get("status") or "") not in OUT_TAGS]
                playing = [r for r in healthy if str(r.get("team") or "") not in byes.get(w, set())]
                need = starters.get(pos, 0)
                st = [r for r in at if r.get("slot") not in ("BN", "IR")]
                pts = np.mean([float(r.get("pts") or 0) for r in st]) if st else 0.0
                proj = np.mean([float(r.get("proj") or 0) for r in st]) if st else 0.0
                n_pos = sum(added[(t, k, pos)] > 0 for k in range(1, w))
                x = {
                    "bye_short": float(max(0, need - len(playing))),
                    "hurt": float(len(at) - len(healthy)),
                    "depth": float(len(at) - need),
                    "starter_pts": float(pts - (np.mean(lvl_pts[pos]) if lvl_pts[pos] else pts)),
                    "starter_proj": float(proj - (np.mean(lvl_proj[pos]) if lvl_proj[pos] else proj)),
                    "team_rate": team_rate,
                    "team_pos_rate": (n_pos + shrink * league_pos[pos]) / (team_weeks + shrink),
                    "league_pos_rate": league_pos[pos],
                    "added_last_week": float(added[(t, prev, pos)] > 0),
                    "is_K": float(pos == "K"),
                    "is_DEF": float(pos == "DEF"),
                }
                out.append(Case(t, w, pos, x, (added[(t, w, pos)] > 0) if w <= label_through else None))
    return out


@dataclass
class Logit:
    mu: np.ndarray
    sd: np.ndarray
    beta: np.ndarray

    def prob(self, c: Case) -> float:
        z = (np.array([c.x[f] for f in FEATURES]) - self.mu) / self.sd
        return float(1.0 / (1.0 + math.exp(-(self.beta[0] + z @ self.beta[1:]))))


def fit(cases: Sequence[Case], l2: float = 20.0, iters: int = 50) -> Optional[Logit]:
    data = [c for c in cases if c.added is not None]
    if len(data) < 30 or sum(c.added for c in data) < 5:
        return None
    X = np.array([[c.x[f] for f in FEATURES] for c in data])
    y = np.array([1.0 if c.added else 0.0 for c in data])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = np.c_[np.ones(len(X)), (X - mu) / sd]
    beta = np.zeros(Z.shape[1])
    pen = l2 * np.eye(Z.shape[1])
    pen[0, 0] = 0.0
    for _ in range(iters):   # Newton steps
        p = 1.0 / (1.0 + np.exp(-(Z @ beta)))
        grad = Z.T @ (p - y) + pen @ beta
        hess = Z.T @ (Z * (p * (1 - p))[:, None]) + pen
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.abs(step).max() < 1e-6:
            break
    return Logit(mu, sd, beta)


def auc(scores: Sequence[float], labels: Sequence[bool]) -> Optional[float]:
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


@dataclass
class AddsExam:
    cases: int = 0
    adds: int = 0
    auc_model: Optional[float] = None
    auc_rule: Optional[float] = None
    rule_recall: float = 0.0          # of the real adds, how many the old rule anticipated
    rule_precision: float = 0.0       # of its "will add", how many happened
    model_recall: float = 0.0         # same, flagging as many cases as the model expects adds
    model_precision: float = 0.0
    expected: float = 0.0             # adds the model expected in the exam weeks
    by_pos: dict[str, tuple[int, int]] = field(default_factory=dict)   # pos -> (anticipated, real)

    @property
    def model_ok(self) -> bool:
        return (self.auc_model or 0) >= 0.65 and self.model_recall > self.rule_recall


def exam(cases: Sequence[Case], first_week: int = 3) -> AddsExam:
    out = AddsExam()
    scores, labels, rule = [], [], []
    hit_m = flag_m = hit_r = flag_r = 0
    by_pos: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for w in sorted({c.week for c in cases if c.added is not None}):
        if w < first_week:
            continue
        model = fit([c for c in cases if c.week < w])
        test = [c for c in cases if c.week == w and c.added is not None]
        if model is None or not test:
            continue
        probs = [model.prob(c) for c in test]
        k = max(1, int(round(sum(probs))))           # flag as many as the model expects
        top = set(sorted(range(len(test)), key=lambda i: -probs[i])[:k])
        for i, c in enumerate(test):
            scores.append(probs[i])
            labels.append(bool(c.added))
            r = c.x["bye_short"] > 0
            rule.append(1.0 if r else 0.0)
            flag_m += i in top
            hit_m += (i in top) and bool(c.added)
            flag_r += r
            hit_r += r and bool(c.added)
            if c.added:
                by_pos[c.pos][1] += 1
                by_pos[c.pos][0] += i in top
        out.expected += sum(probs)
    out.cases, out.adds = len(labels), sum(labels)
    out.auc_model, out.auc_rule = auc(scores, labels), auc(rule, labels)
    out.rule_recall = hit_r / max(1, out.adds)
    out.rule_precision = hit_r / max(1, flag_r)
    out.model_recall = hit_m / max(1, out.adds)
    out.model_precision = hit_m / max(1, flag_m)
    out.by_pos = {p: (v[0], v[1]) for p, v in by_pos.items()}
    return out


def predict(cases: Sequence[Case], week: int) -> dict[str, dict[str, float]]:
    """{team_id: {pos: probability of adding at pos}} for `week`, trained on earlier weeks."""
    model = fit([c for c in cases if c.week < week])
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for c in cases:
        if c.week == week:
            out[c.team_id][c.pos] = model.prob(c) if model else c.x["team_pos_rate"]
    return dict(out)
