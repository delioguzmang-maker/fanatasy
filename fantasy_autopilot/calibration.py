"""Learn the Questionable discount from this league instead of guessing it.

The problem: a Questionable starter is counted as ``proj × 0.75``. If Yahoo
already lowers the projection of Questionable players, that is a *double*
discount. Yahoo's past-week pages show today's injury tag, not the tag the
player had before kickoff, so the past cannot answer it.

The fix: every lineup run writes a pre-game snapshot (status, projection) to
``estado_log.csv``. After the games, compare with the real points:

    factor_Q = (real ÷ proj for Questionable) ÷ (real ÷ proj for healthy players)

That single number already contains both "did he play?" and "did Yahoo
already discount him?". It is pulled toward 0.75 until there is enough data.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

FIELDS = ["time", "week", "pid", "name", "pos", "team", "status", "practice", "proj", "p_play", "slot", "sleeper_id"]


def log_statuses(path: Path, week: int, players: Sequence, now: datetime) -> int:
    """Append one row per player whose game has not started. Returns rows written."""
    rows = [p for p in players if not p.locked and not p.on_bye and p.slot != "IR" and p.proj > 0]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:  # an older file without the newer columns: rewrite it once with the full header
        with path.open(newline="") as fh:
            header = next(csv.reader(fh), [])
        if header != FIELDS:
            old = read_log(path)
            with path.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for p in rows:
            w.writerow({"time": now.isoformat(timespec="minutes"), "week": week, "pid": p.pid, "name": p.name,
                        "pos": p.position, "team": p.team, "status": p.status, "practice": p.practice,
                        "proj": round(p.proj, 2), "p_play": round(p.prob, 3), "slot": p.slot,
                        "sleeper_id": p.sleeper_id or ""})
    return len(rows)


def last_snapshots(log_rows: Iterable[Mapping[str, str]]) -> dict[tuple[int, str], dict]:
    """The latest pre-game row for each (week, player)."""
    out: dict[tuple[int, str], dict] = {}
    for r in log_rows:
        try:
            key = (int(r["week"]), str(r["pid"]))
        except (KeyError, ValueError):
            continue
        if key not in out or str(r["time"]) >= str(out[key]["time"]):
            out[key] = dict(r)
    return out


def questionable_factor(
    log_rows: Iterable[Mapping[str, str]],
    actual: Mapping[tuple[int, str], float],
    prior: float = 0.75,
    strength: float = 30.0,
    status: str = "Q",
) -> dict:
    """Measured discount for `status` players, relative to healthy players.

    `actual` maps (week, pid) -> real fantasy points. Returns a dict with
    n, played (share with points != 0), raw factor, shrunk factor, and a
    Spanish explanation."""
    snaps = last_snapshots(log_rows)
    sums = {"q": [0.0, 0.0, 0, 0], "h": [0.0, 0.0, 0, 0]}  # real, proj, n, played
    for key, r in snaps.items():
        if key not in actual:
            continue
        try:
            proj = float(r["proj"])
        except (KeyError, ValueError):
            continue
        if proj <= 0:
            continue
        grp = "q" if r.get("status", "") == status else "h" if r.get("status", "") == "" else None
        if grp is None:
            continue
        real = float(actual[key])
        s = sums[grp]
        s[0] += real
        s[1] += proj
        s[2] += 1
        s[3] += real != 0
    qr, qp, n, played = sums["q"]
    hr, hp, nh, _ = sums["h"]
    if n == 0 or hp <= 0 or qp <= 0 or nh < 20:
        return {"n": n, "played": None, "raw": None, "factor": prior,
                "texto": f"Todavía no hay datos suficientes ({n} casos {status}); se usa {prior:.2f}."}
    raw = (qr / qp) / (hr / hp)
    factor = (n * raw + strength * prior) / (n + strength)
    factor = max(0.3, min(1.0, factor))
    texto = (f"{n} casos {status}: jugaron {played}/{n}. Real ÷ proyección: {qr / qp:.2f} contra "
             f"{hr / hp:.2f} de los sanos → factor medido {raw:.2f}; con freno hacia {prior:.2f}: {factor:.2f}.")
    if raw > 0.95:
        texto += " Yahoo ya descuenta a estos jugadores: multiplicar otra vez sería doble descuento."
    return {"n": n, "played": played / n, "raw": raw, "factor": factor, "texto": texto}


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def player_sd(rows: Iterable[Mapping[str, object]], by_pos: Mapping[str, float], strength: float = 6.0,
              min_weeks: int = 3) -> dict[str, float]:
    """Weekly scoring SD per player (real - proj), pulled toward the position SD.

    rows need pid, pos, proj, real. Used by the win-probability lineup."""
    acc: dict[str, list] = {}
    for r in rows:
        try:
            pid, pos = str(r["pid"]), str(r["pos"])
            resid = float(r["real"]) - float(r["proj"])
        except (KeyError, ValueError, TypeError):
            continue
        acc.setdefault(pid, [pos, []])[1].append(resid)
    out = {}
    for pid, (pos, res) in acc.items():
        if len(res) < min_weeks or pos not in by_pos:
            continue
        mean = sum(res) / len(res)
        var = sum((x - mean) ** 2 for x in res) / (len(res) - 1)
        prior = by_pos[pos] ** 2
        shrunk = ((len(res) - 1) * var + strength * prior) / (len(res) - 1 + strength)
        out[pid] = round(shrunk ** 0.5, 2)
    return out


def questionable_split(
    log_rows: Iterable[Mapping[str, str]],
    actual: Mapping[tuple[int, str], float],
    stats: Mapping[int, Mapping[str, Mapping]],
    status: str = "Q",
    min_cases: int = 30,
) -> dict:
    """Separate the two effects inside the Questionable discount.

    Uses the real participation of each week (Sleeper stats: snaps, carries, targets)
    for running backs, receivers and tight ends:

    * play rate: how many Questionable players took the field (snaps or touches > 0);
    * when active: touches against his usual touches in the other weeks, and
      points ÷ projection against the same ratio for healthy players.

    Points alone mix both effects (a 0 can be "did not play" or "played and did
    nothing"); with snaps and touches they can be told apart."""
    def touches(st):
        return float(st.get("rush_att", 0) or 0) + float(st.get("rec_tgt", 0) or 0)

    def played(st):
        return bool(st) and bool(st.get("off_snp") or st.get("gp") or touches(st) > 0)

    def by_player(rows):   # Sleeper's raw list of rows also works: {player_id: stats}
        if isinstance(rows, Mapping):
            return rows
        return {str(r.get("player_id")): r.get("stats") or {} for r in rows or () if isinstance(r, Mapping)}

    stats = {w: by_player(rows) for w, rows in stats.items()}
    usual: dict[str, list[float]] = {}
    for w, by_id in stats.items():
        for sid, st in by_id.items():
            if played(st):
                usual.setdefault(str(sid), []).append((w, touches(st)))
    snaps = last_snapshots(log_rows)
    q_n = q_played = 0
    q_load, q_pts, q_proj, h_pts, h_proj = [], 0.0, 0.0, 0.0, 0.0
    for (w, pid), r in snaps.items():
        sid = str(r.get("sleeper_id") or "")
        if r.get("pos") not in ("RB", "WR", "TE") or not sid or w not in stats or (w, pid) not in actual:
            continue
        st = stats[w].get(sid, {})
        proj = float(r.get("proj") or 0)
        if proj <= 0:
            continue
        if r.get("status", "") == status:
            q_n += 1
            if played(st):
                q_played += 1
                q_pts += float(actual[(w, pid)])
                q_proj += proj
                other = [t for ww, t in usual.get(sid, []) if ww != w]
                if other and sum(other) > 0:
                    q_load.append(touches(st) / (sum(other) / len(other)))
        elif r.get("status", "") == "" and played(st):
            h_pts += float(actual[(w, pid)])
            h_proj += proj
    out = {"n": q_n, "played": q_played, "play_rate": None, "workload": None, "active_factor": None}
    if q_n:
        out["play_rate"] = q_played / q_n
    if q_load:
        out["workload"] = sum(q_load) / len(q_load)
    if q_played and q_proj > 0 and h_proj > 0:
        out["active_factor"] = (q_pts / q_proj) / (h_pts / h_proj)
    if q_n < min_cases:
        out["texto"] = (f"{q_n} casos {status} con participación conocida (faltan {min_cases - q_n} para separar "
                        "'no jugó' de 'jugó limitado').")
    else:
        out["texto"] = (f"{q_n} casos {status}: jugaron {q_played} ({out['play_rate']:.0%}). Cuando jugaron: toques "
                        f"{(out['workload'] or 0):.0%} de lo normal y puntos ÷ proyección "
                        f"{(out['active_factor'] or 0):.2f} contra los sanos.")
    return out
