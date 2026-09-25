"""Fantasy Autopilot for Yahoo Fantasy Football.

Lineup optimizer with injury-aware hedging, game-day inactive swaps, waiver
and FAAB bid planner, matchup and trade analysis, and optional automatic
writes to Yahoo through your own browser session. Built to run in Google
Colab or on a schedule (GitHub Actions).
"""

from .config import Config
from .engine import Autopilot, RunResult

__all__ = ["Autopilot", "Config", "RunResult"]
__version__ = "0.1.0"
