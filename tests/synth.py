"""Synthetic Sleeper weeks and league adds with a known pattern, for the learning tests."""

import random

KNOWN = [("Brock", "Purdy", "QB", "SF"), ("C.J.", "Stroud", "QB", "HOU"), ("Jaylen", "Warren", "RB", "PIT"),
         ("Kyren", "Williams", "RB", "LAR"), ("Nico", "Collins", "WR", "HOU"), ("Courtland", "Sutton", "WR", "DEN"),
         ("Cam", "Skattebo", "RB", "NYG"), ("Mike", "Evans", "WR", "SF"), ("Daniel", "Jones", "QB", "IND"),
         ("Jacoby", "Brissett", "QB", "ARI"), ("Malik", "Nabers", "WR", "NYG"), ("Bhayshul", "Tuten", "RB", "JAX")]
TEAMS = ["SF", "HOU", "PIT", "LAR", "DEN", "NYG", "IND", "ARI", "JAX", "KC", "BAL", "DET", "CHI", "TB", "ATL", "CAR"]


class SynthSleeper:
    """Sleeper misses some players the same way every week (a learnable pattern) and the games are noisy."""

    def __init__(self, seed=0, extra=50, weeks=None):
        rng = random.Random(seed)
        self.weeks = weeks or {2025: 18, 2026: 3}
        pos_cycle = ["QB", "RB", "RB", "WR", "WR", "WR", "TE"]
        self.players = list(KNOWN) + [("Player", f"N{i}", pos_cycle[i % 7], rng.choice(TEAMS)) for i in range(extra)]
        self.mean = {p: rng.uniform(6, 20) for p in self.players}
        self.miss = {p: (rng.choice([-5.0, 0.0, 0.0, 5.0])) for p in self.players}   # Sleeper's blind spot
        self.data = {}
        for season, n in self.weeks.items():
            for w in range(1, n + 2):
                proj, stats = [], []
                for i, p in enumerate(self.players):
                    first, last, pos, team = p
                    opp = TEAMS[(TEAMS.index(team) + w) % len(TEAMS)]
                    sp = max(1.0, self.mean[p] - self.miss[p])
                    real = max(0.0, self.mean[p] + rng.gauss(0, 3))
                    base = {"player_id": str(1000 + i), "team": team, "opponent": opp,
                            "player": {"first_name": first, "last_name": last, "position": pos}}
                    proj.append({**base, "stats": {"pts_half_ppr": round(sp, 2), "pts_ppr": round(sp + 1, 2),
                                                   "pts_std": round(sp - 1, 2), "rec_tgt": 5, "rush_att": 5}})
                    stats.append({**base, "stats": {"pts_half_ppr": round(real, 2), "pts_ppr": round(real + 1, 2),
                                                    "pts_std": round(real - 1, 2), "gp": 1, "off_snp": 50,
                                                    "tm_off_snp": 60, "rec_tgt": 5, "rush_att": 5}})
                self.data[("projections", season, w)] = proj
                self.data[("stats", season, w)] = stats if w <= n else []

    def rows(self, kind, season, week, final=False):
        return self.data.get((kind, season, week), [])

    # the rest of the Sleeper interface the engine touches
    def state(self):
        return {"week": 3, "season": "2026"}

    def match(self, *a, **k):
        return None

    def players(self):
        return {}

    def trending_adds(self, *a, **k):
        return []

    def projections(self, *a, **k):
        return {}


def league_adds(seed=1, teams=12, weeks=9):
    """Rosters and adds of a league where adds come from busy managers and K/DEF streamers, rarely from byes."""
    rng = random.Random(seed)
    nfl = TEAMS
    byes = {w: set(rng.sample(nfl, 2)) for w in range(1, weeks + 2)}
    ids = [str(i) for i in range(1, teams + 1)]
    activity = {t: rng.choice([0.05, 0.25, 0.6]) for t in ids}
    streamer = {t: rng.random() < 0.4 for t in ids}
    layout = ["QB", "QB", "RB", "RB", "RB", "RB", "WR", "WR", "WR", "WR", "WR", "TE", "TE", "K", "DEF"]
    starters = {0, 2, 3, 6, 7, 11, 13, 14}
    roster, adds = [], []
    for t in ids:
        players = [(pos, rng.choice(nfl)) for pos in layout]
        for w in range(1, weeks + 1):
            for i, (pos, team) in enumerate(players):
                roster.append(dict(team_id=t, week=w, pos=pos, team=team, slot=pos if i in starters else "BN",
                                   proj=10.0, pts=rng.gauss(10, 6), status=""))
    for t in ids:
        for w in range(2, weeks + 1):
            for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
                p = 0.02 + 0.5 * activity[t] * (0.5 if pos in ("QB", "TE") else 1.0)
                if pos in ("K", "DEF") and streamer[t]:
                    p += 0.6
                if rng.random() < min(0.95, p):
                    adds.append(dict(team_id=t, semana=w, pos=pos))
    return roster, adds, byes
