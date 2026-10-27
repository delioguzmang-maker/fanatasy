"""Fantasy Autopilot for Yahoo Fantasy Football.

Lineup optimizer with injury-aware hedging, game-day inactive swaps, waiver
and FAAB bid planner, matchup and trade analysis, and optional automatic
writes to Yahoo through your own browser session. Built to run in Google
Colab or on a schedule (GitHub Actions).
"""

import json

from .config import Config
from .engine import Autopilot, RunResult

__all__ = ["Autopilot", "Config", "RunResult", "build_info"]
__version__ = "0.2.1"


def build_info() -> str:
    """Installed version and the git commit it was built from (pip records it for git installs),
    so a notebook can show which code it is really running."""
    try:
        from importlib import metadata

        dist = metadata.distribution("fantasy-autopilot")
        info = json.loads(dist.read_text("direct_url.json") or "{}")
        commit = (info.get("vcs_info") or {}).get("commit_id", "")
        return dist.version + (f" · commit {commit[:7]}" if commit else "")
    except Exception:  # noqa: BLE001 - running from a checkout
        return __version__
